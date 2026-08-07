"""Phase-scoped component residency for a 48 GB machine.

`MiniMaxH3Pipeline.from_pretrained` loads the encoder, the DiT and both VAEs up front and holds all
four for the whole run — 45.9 GB of weights, and ~55 GB once the diffusion phase's own activations
are added, against 48 GB of physical memory. But the components are used in
strict phases: the text encoder is read exactly once (`pipeline.py` line 238), the video VAE
encodes keyframes and then is idle until the final decode, and the DiT only matters in between.

Most of this reimplements nothing. Two seams are enough for residency:

* Every component is replaced by a **proxy** that already knows its config — which is what the
  early lines of `__call__` actually need (``dit.config.patch_size``,
  ``video_vae.config.spatial_compression_ratio``, ``audio_vae.config.latent_channels``) — and loads
  its weights on the first access to anything else.
* The text-encoder proxy owns the unload. ``self.text_encoder.encode(...)`` is the encoder's only
  call site, so the proxy runs it, **materializes the result**, and then drops the weights. The
  `mx.eval` is not optional: MLX is lazy, and a returned graph still referencing the encoder's
  parameters pins all 28.2 GB no matter what is deleted.

`_ensure_cache` is overridden as well, because this build's AdaLN table is precomputed rather than
computable — see :mod:`h3_48gb.adaln`.

`__call__` stays a thin wrapper too, for the same reason :mod:`h3_48gb.checkpoint` gives for its own
seams: wrapping `self.dit` sees everything a per-step hook needs (the full packed rows entering
every forward) without duplicating the denoising loop itself. `preview_every` / `preview_stem`
install :class:`h3_48gb.preview.PreviewInterceptor` the same way `checkpoint_dir` installs
`h3_48gb.checkpoint.StepInterceptor` — nested around it if both are active, so a step the checkpoint
fast-forwards is invisible to the preview too. See :mod:`h3_48gb.preview` for the hook itself and
for why a preview can only ever decode the VAE's minimum chunk, not an arbitrary frame.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import _upstream  # noqa: F401

import mlx.core as mx

from minimax_h3_mlx.config import DiTConfig, PipelineConfig
from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

from . import memory
from .adaln import AdaLNCacheFile, ScheduleMismatch
from .checkpoint import CheckpointingPipeline, pop_checkpoint_kwargs, weights_fingerprint
from .dit import load_dit_cached
from .preview import PreviewInterceptor, pop_preview_kwargs
from .text_encoder import QuantizedTextEncoder


# -- configs read without touching the weights -------------------------------------------------

def video_vae_config(model_dir: Path):
    """`VideoVAEConfig` from a converted ``video_vae/`` directory, mirroring `load_video_vae`."""
    from minimax_h3_mlx.video_vae import VideoVAEConfig

    with open(model_dir / "config.json") as fh:
        wrapper = json.load(fh)
    with open(model_dir / "source" / "config.json") as fh:
        source = json.load(fh)
    ch = source["ch"]
    return VideoVAEConfig(
        in_channels=source["in_channels"],
        out_channels=source["out_ch"],
        latent_channels=source["z_channels"],
        block_out_channels=tuple(ch * m for m in source["ch_mult"]),
        layers_per_block=source["num_res_blocks"],
        spatial_downsample_factors=tuple(source["space_down"]),
        temporal_downsample_factors=tuple(source["time_down"]),
        decoder_num_layers=source["vit_decoder_kwargs"]["num_layers"],
        decoder_num_attention_heads=source["vit_decoder_kwargs"]["heads"],
        decoder_attention_head_dim=source["vit_decoder_kwargs"]["dim_head"],
        decoder_rope_theta=source["vit_decoder_kwargs"]["rope_theta"],
        decoder_rope_dim_ratio=source["vit_decoder_kwargs"]["rope_dim_ratio"],
        clip_length=wrapper.get("vae_clip_length", 17),
        token_drop=wrapper.get("vae_token_drop", 3),
        latents_mean=tuple(wrapper.get("latents_mean", ())),
        latents_std=tuple(wrapper.get("latents_std", ())),
    )


def audio_vae_config(model_dir: Path):
    """`AudioVAEConfig` from a converted ``audio_vae/`` directory, mirroring `load_audio_vae`."""
    from minimax_h3_mlx.audio_vae import AudioVAEConfig

    with open(model_dir / "metadata.json") as fh:
        kwargs = json.load(fh)["metadata"]["kwargs"]
    with open(model_dir / "config.json") as fh:
        wrapper = json.load(fh)
    return AudioVAEConfig(
        encoder_dim=kwargs["encoder_dim"],
        encoder_rates=tuple(kwargs["encoder_rates"]),
        latent_dim=kwargs["latent_dim"],
        latent_channels=kwargs["vae_latent_channels"],
        decoder_dim=kwargs["decoder_dim"],
        decoder_rates=tuple(kwargs["decoder_rates"]),
        sampling_rate=kwargs["sample_rate"],
        latents_mean=tuple(wrapper.get("latents_mean", ())),
        latents_std=tuple(wrapper.get("latents_std", ())),
    )


# -- lazy components ---------------------------------------------------------------------------

class LazyComponent:
    """A component that is its config until something needs its weights.

    ``config`` resolves without loading. Any other attribute, or calling the proxy, triggers the
    load. That is exactly the distinction the pipeline's ``__call__`` draws: geometry is computed
    from configs long before the first weight is read.
    """

    def __init__(self, name: str, config, loader, verbose: bool = True):
        self.__dict__["_name"] = name
        self.__dict__["_config"] = config
        self.__dict__["_loader"] = loader
        self.__dict__["_obj"] = None
        self.__dict__["_verbose"] = verbose

    # -- state

    @property
    def loaded(self) -> bool:
        return self._obj is not None

    @property
    def config(self):
        return self._config if self._obj is None else self._obj.config

    def load(self):
        if self._obj is None:
            started = time.perf_counter()
            before = mx.get_active_memory()
            self.__dict__["_obj"] = self._loader()
            if self._verbose:
                grew = (mx.get_active_memory() - before) / 1e9
                print(f"  loaded {self._name}: {grew:+.2f} GB in "
                      f"{time.perf_counter() - started:.1f}s", flush=True)
        return self._obj

    def unload(self) -> None:
        if self._obj is None:
            return
        before = mx.get_active_memory()
        obj = self._obj
        self.__dict__["_obj"] = None
        unload = getattr(obj, "unload", None)
        if callable(unload):
            unload()
        del obj
        memory.release()
        if self._verbose:
            freed = (before - mx.get_active_memory()) / 1e9
            print(f"  unloaded {self._name}: {freed:.2f} GB released", flush=True)

    # -- transparency

    #: The proxy's own state. Everything else forwards — including single-underscore names, because
    #: `_encode_keyframes` reaches straight into ``video_vae._encode_clip``.
    _OWN = frozenset({"_name", "_config", "_loader", "_obj", "_verbose", "_unload_after_encode"})

    def __getattr__(self, item):
        # Only reached for attributes not found normally. Dunders and the proxy's own fields must
        # not trigger a load, or `__init__` and any copy/pickle probe would recurse.
        if item in LazyComponent._OWN or item.startswith("__"):
            raise AttributeError(item)
        return getattr(self.load(), item)

    def __call__(self, *args, **kwargs):
        return self.load()(*args, **kwargs)


class LazyTextEncoder(LazyComponent):
    """The text encoder, which unloads itself the moment its one job is done.

    28.2 GB resident for a single call at the top of the run. `encode` is the only entry point the
    pipeline uses, so wrapping it is enough to bound the encoder's lifetime — and it is the only
    place where the `mx.eval` that makes the release real can be placed correctly.
    """

    def __init__(self, *args, unload_after_encode: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__["_unload_after_encode"] = unload_after_encode

    def encode(self, prompt, images=None):
        hidden_states, token_tags = self.load().encode(prompt, images)
        # Materialize before releasing anything: a lazy graph over the encoder's parameters keeps
        # every one of them alive, and the unload below would free nothing at all.
        mx.eval(hidden_states)
        # Note the absence of a local holding the encoder. Binding `encoder = self.load()` above
        # and unloading here would free nothing until this frame returned, because that local is
        # another live reference — measured, not assumed (see test_lazy_pipeline.py).
        if self._unload_after_encode:
            self.unload()
        return hidden_states, token_tags


# -- the pipeline ------------------------------------------------------------------------------

class LazyMiniMaxH3Pipeline(CheckpointingPipeline, MiniMaxH3Pipeline):
    """`MiniMaxH3Pipeline` with phase-scoped residency, a precomputed AdaLN table and resumable runs."""

    def __init__(self, dit, text_encoder, video_vae, audio_vae, config, adaln_cache_path=None,
                 verbose: bool = True, weights_id: dict | None = None):
        super().__init__(dit, text_encoder, video_vae, audio_vae, config)
        self._adaln_cache_path = Path(adaln_cache_path) if adaln_cache_path else None
        self._schedules = None
        self._supported_steps: int | None = None
        self._weights_id = weights_id or {}
        self._tracker = memory.PhaseTracker(verbose=verbose)

    @classmethod
    def from_pretrained(
        cls,
        checkpoint_dir: str | Path,
        transformer_dir: str | Path | None = None,
        dtype: mx.Dtype = mx.bfloat16,
        load_vision: bool = True,
        unload_after_encode: bool = True,
        verbose: bool = True,
    ) -> "LazyMiniMaxH3Pipeline":
        """Read every config; read no weights.

        ``load_vision`` defaults to True here, unlike upstream: keyframe conditioning is the point
        of the fl2va partition, and the tower is only 1.1 GB — and it is released with the rest of
        the encoder after the single `encode` call.
        """
        root = Path(checkpoint_dir)
        dit_path = Path(transformer_dir) if transformer_dir else root / "transformer"
        config = PipelineConfig.from_model_index(root / "model_index.json")

        dit_config = DiTConfig.from_json(dit_path / "config.json")
        cache_path = dit_path / "adaln_cache.safetensors"

        if verbose:
            print(f"loading MiniMax-H3 configs from {root} (weights are deferred)")

        components = {
            "dit": LazyComponent(
                "transformer", dit_config,
                lambda: load_dit_cached(dit_path, verbose=verbose), verbose),
            "text_encoder": LazyTextEncoder(
                "text encoder", None,
                lambda: QuantizedTextEncoder(root / "text_encoder", dtype=dtype,
                                             load_vision=load_vision, verbose=verbose),
                verbose=verbose, unload_after_encode=unload_after_encode),
            "video_vae": LazyComponent(
                "video vae", video_vae_config(root / "video_vae"),
                lambda: _load_video_vae(root / "video_vae"), verbose),
            "audio_vae": LazyComponent(
                "audio vae", audio_vae_config(root / "audio_vae"),
                lambda: _load_audio_vae(root / "audio_vae"), verbose),
        }
        return cls(components["dit"], components["text_encoder"], components["video_vae"],
                   components["audio_vae"], config,
                   adaln_cache_path=cache_path if cache_path.exists() else None,
                   verbose=verbose,
                   # Which weights produced a checkpoint is part of its identity: resuming a run
                   # onto a reconverted transformer would denoise into a different model.
                   weights_id=weights_fingerprint(dit_path, root / "text_encoder",
                                                  root / "video_vae", root / "audio_vae"))

    # -- keyframe conditioning ------------------------------------------------------------------

    def _encode_keyframes(self, images, height, width):
        """Materialize the video VAE *before* upstream seeds the keyframe generator.

        Upstream constructs every component in `from_pretrained`, long before
        `_encode_keyframes` calls `mx.random.seed(42)` and draws the posterior sample. Here the
        VAE is a proxy, so the first touch of `video_vae._encode_clip` builds it — which happens
        *after* that seed, and building it draws from MLX's global RNG for some 560 parameter
        tensors. The sample therefore comes off a different point in the stream, and the
        conditioning latents depend on whether the VAE happened to be resident already: a cold
        and a warm run of the same request differed by 0.87.

        Loading here rather than in `__call__` keeps the peak where it was — by this point the
        28.2 GB text encoder has been released.
        """
        self.video_vae.load()
        return super()._encode_keyframes(images, height, width)

    # -- schedule / modulation ------------------------------------------------------------------

    def _build_schedules(self, num_inference_steps: int):
        """Keep the schedulers: `_ensure_cache` needs them to locate rows in the cached table."""
        schedules = super()._build_schedules(num_inference_steps)
        self._schedules = schedules
        return schedules

    def _ensure_cache(self, timesteps: mx.array, drop_adaln: bool, verbose: bool):
        """Serve the modulation table from mere.run's cache instead of computing it.

        `ModulationCache.build` reads ``dit.time_embedder`` and every block's ``adaln_proj``; this
        build ships neither. ``drop_adaln`` is moot — the projections were never allocated.
        """
        if self._adaln_cache_path is None:
            return super()._ensure_cache(timesteps, drop_adaln, verbose)
        if self._schedules is None:
            raise RuntimeError("`_build_schedules` must run before the modulation table is built.")

        key = tuple(round(float(v), 9) for v in timesteps.tolist())
        if self._cache is not None and self._cache_timesteps == key:
            return

        started = time.perf_counter()
        video_sched, audio_sched = self._schedules
        cache_file = AdaLNCacheFile(self._adaln_cache_path)
        cache = cache_file.build(timesteps, video_sched, audio_sched, dtype=mx.bfloat16,
                                 verbose=False)

        # The DiT's output layer has no `adaln_proj` either; hand it the cached shift/scale.
        final_layer = self.dit.final_layer
        if not hasattr(final_layer, "set_modulation"):
            raise RuntimeError(
                "The DiT was not loaded by h3_48gb.dit.load_dit_cached, so its output layer still "
                "expects `final_layer.adaln_proj` — which this checkpoint does not carry."
            )
        final_layer.set_modulation(cache.final_shift, cache.final_scale)

        self._cache = cache
        self._cache_timesteps = key
        if verbose:
            print(f"  adaln cache: {len(key)} timesteps, {cache.nbytes() / 1e6:.0f} MB "
                  f"from {self._adaln_cache_path.name} in {time.perf_counter() - started:.1f}s")

    # -- guard rails ----------------------------------------------------------------------------

    def supported_num_inference_steps(self) -> int | None:
        """The one step count this build's precomputed table can serve, if it has one.

        The port's scheduler spends one grid point on the terminal sigma, so ``N`` grid points drive
        ``N - 1`` forwards — the cache's ``video_sigmas`` length *is* the argument to pass.
        """
        if self._adaln_cache_path is None:
            return None
        if self._supported_steps is None:
            raw = mx.load(str(self._adaln_cache_path))
            self._supported_steps = int(raw["video_sigmas"].shape[0])
        return self._supported_steps

    def checkpoint_identity_extra(self) -> dict:
        """The weight files and the sigma shifts — everything outside the request that steers the run."""
        return {
            "weights": self._weights_id,
            "sigma_shift_video": self.config.sigma_shift_video,
            "sigma_shift_audio": self.config.sigma_shift_audio,
            "adaln_cache": self._adaln_cache_path.name if self._adaln_cache_path else None,
        }

    def __call__(self, *args, **kwargs):
        """Validate the schedule, install the checkpoint/preview seams, then run upstream's loop.

        A wrong step count would otherwise be caught only once the encoder had been loaded and run
        — 28.2 GB and several minutes into the request.
        """
        # `pop`/re-add rather than `**kwargs` passthrough: the bind below is against *upstream's*
        # signature, which knows nothing about the checkpoint/preview arguments and would reject
        # them.
        checkpoint_kwargs = pop_checkpoint_kwargs(kwargs)
        preview_options = pop_preview_kwargs(kwargs)
        preview_every = int(preview_options.get("preview_every", 0) or 0)
        preview_stem = preview_options.get("preview_stem")
        supported = self.supported_num_inference_steps()

        import inspect

        bound = inspect.signature(MiniMaxH3Pipeline.__call__).bind(self, *args, **kwargs)
        bound.apply_defaults()
        if supported is not None:
            requested = int(bound.arguments["num_inference_steps"])
            if requested != supported:
                raise ScheduleMismatch(
                    f"This build's AdaLN table is precomputed for num_inference_steps={supported} "
                    f"({supported - 1} forwards) at sigma shifts 12.0/3.0, but the request asks "
                    f"for {requested}. mere.run dropped the modulation projections, so no other "
                    "schedule can be evaluated — pass "
                    f"num_inference_steps={supported}."
                )

        if preview_every < 0:
            raise ValueError(f"`preview_every` must be >= 0 (0 disables previews), got {preview_every}.")

        # Hand both consumers the same picture. Upstream passes the raw `images` to the text
        # encoder (its line 238) and only later prepares them for the VAE (its line 192), so
        # Qwen3-VL describes the original while the conditioning latent is made from the canvas
        # version. The reference prepares once, before either consumer
        # (`reference/diffusers/modular/before_encoder.py:190`). Mismatched, nothing raises: the
        # number of vision tokens changes, and `packing` derives every media row's rotary clock
        # from it. Preparing here is safe to repeat — `prepare_keyframe_image` returns an image
        # that already is the canvas untouched, so upstream's own call becomes a no-op.
        images = bound.arguments.get("images")
        if images:
            from minimax_h3_mlx.packing import prepare_keyframe_image, resolve_canvas_size

            height, width = bound.arguments["height"], bound.arguments["width"]
            if height is None or width is None:
                height, width = resolve_canvas_size(*bound.arguments["aspect"])
            bound.arguments["images"] = [
                prepare_keyframe_image(image, height, width, stretch=index == 0)
                for index, image in enumerate(images)
            ]
            args, kwargs = bound.args[1:], bound.kwargs

        original_dit = self.dit
        if preview_every:
            self.dit = self._install_preview(original_dit, preview_every, preview_stem, bound.arguments)
        try:
            return super().__call__(*args, **kwargs, **checkpoint_kwargs)
        finally:
            self.dit = original_dit

    def _install_preview(self, dit, every: int, stem, arguments: dict) -> PreviewInterceptor:
        """Size a `PreviewInterceptor` from the same public geometry helpers upstream uses.

        Deliberately re-derives ``latent_height``/``latent_width``/``num_latent_frames``/
        ``n_cond_v`` from the request arguments rather than reaching into upstream's `__call__` for
        them — there is nowhere to reach into without duplicating the loop (see the module
        docstring). These are pure functions of the request plus the VAE/DiT configs, called here
        exactly as `MiniMaxH3Pipeline.__call__` calls them, so this can drift only if upstream's
        canvas/frame-count math changes shape entirely — which `test_preview.py` would catch via the
        shape check inside `preview.render_preview_frame`.
        """
        from minimax_h3_mlx.packing import FPS, align_num_frames, resolve_canvas_size, video_latent_num_frames

        if stem is None:
            raise ValueError(
                "`preview_every` > 0 requires `preview_stem`: previews are written to "
                "`<preview_stem>-preview-stepNN.jpg`."
            )

        height, width = arguments["height"], arguments["width"]
        if height is None or width is None:
            height, width = resolve_canvas_size(*arguments["aspect"])
        num_frames = align_num_frames(int(round(arguments["duration_seconds"] * FPS)))
        num_latent_frames = video_latent_num_frames(num_frames)
        ratio = self.video_vae.config.spatial_compression_ratio
        latent_height, latent_width = height // ratio, width // ratio
        patch_size = self.dit.config.patch_size
        rows_per_frame = (latent_height // patch_size[1]) * (latent_width // patch_size[2])
        n_cond_v = len(arguments["keyframe_anchors"]) * rows_per_frame

        return PreviewInterceptor(
            dit, self, every, stem, n_cond_v, num_latent_frames, latent_height, latent_width,
            patch_size, verbose=bool(arguments.get("verbose", True)),
        )


def _load_video_vae(model_dir: Path):
    from minimax_h3_mlx.load import load_video_vae

    return load_video_vae(model_dir)


def _load_audio_vae(model_dir: Path):
    from minimax_h3_mlx.load import load_audio_vae

    return load_audio_vae(model_dir)
