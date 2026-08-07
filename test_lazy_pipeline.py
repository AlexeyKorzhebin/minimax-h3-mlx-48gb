#!/usr/bin/env python3
"""Prove phase-scoped residency: configs are free, weights are not loaded early, and the text
encoder's memory is really released.

    ./.venv/bin/python test_lazy_pipeline.py

Two things are easy to get wrong and neither raises:

* a proxy that loads on *any* touch defeats the point — the pipeline reads
  ``dit.config.patch_size`` and ``video_vae.config.spatial_compression_ratio`` long before the
  first weight is needed, so ``.config`` must not trigger a load;
* dropping a reference to the encoder frees nothing while a lazy MLX graph still reads its
  parameters. The unload is only real if the output is materialized first.

The second one is measured against MLX's allocator on arrays big enough for the difference to be
unambiguous, with a deliberately-lazy control that shows the measurement has teeth.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from h3_48gb import _upstream  # noqa: E402,F401

import mlx.core as mx  # noqa: E402

from h3_48gb.memory import PhaseTracker, release, snapshot  # noqa: E402
from h3_48gb.pipeline import LazyComponent, LazyTextEncoder  # noqa: E402

#: Big enough that allocator noise cannot explain the drop: 256 MB per fake "encoder".
WEIGHT_ELEMENTS = 64 * 1024 * 1024
WEIGHT_GB = WEIGHT_ELEMENTS * 4 / 1e9


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} FAILED {detail}")
    print(f"  ok  {name}")


class FakeConfig:
    def __init__(self, value):
        self.patch_size = value


class FakeModel:
    """Holds a real MLX array so its residency is visible to the allocator."""

    def __init__(self, config, elements=WEIGHT_ELEMENTS):
        self.config = config
        self.weight = mx.zeros((elements,), dtype=mx.float32)
        mx.eval(self.weight)
        self.calls = 0

    def __call__(self, x):
        self.calls += 1
        return x * 2

    def encode(self, prompt, images=None):
        # Deliberately lazy: the returned graph still reads `self.weight`.
        return self.weight[:4] + len(prompt), ["tag"]


def test_config_is_free() -> None:
    loads = []
    proxy = LazyComponent("toy", FakeConfig((1, 2, 2)),
                          lambda: (loads.append(1), FakeModel(FakeConfig((9, 9, 9))))[1],
                          verbose=False)
    check("not loaded on construction", not proxy.loaded)
    check("config resolves without loading", proxy.config.patch_size == (1, 2, 2))
    check("still not loaded after reading the config", not proxy.loaded and not loads)


def test_load_on_first_real_use() -> None:
    loads = []
    proxy = LazyComponent("toy", FakeConfig((1, 2, 2)),
                          lambda: (loads.append(1), FakeModel(FakeConfig((1, 2, 2))))[1],
                          verbose=False)
    check("calling the proxy loads it", int(proxy(mx.array([2.0]))[0]) == 4 and len(loads) == 1)
    check("it is marked loaded", proxy.loaded)
    check("a second call does not reload", proxy(mx.array([1.0])) is not None and len(loads) == 1)
    check("attribute access forwards", proxy.calls == 2, f"got {proxy.calls}")
    check("config now comes from the loaded object", proxy.config.patch_size == (1, 2, 2))


def test_private_attributes_forward() -> None:
    """`_encode_keyframes` reaches into ``video_vae._encode_clip``; a blanket underscore guard
    would break keyframe conditioning with an AttributeError."""

    class WithPrivate(FakeModel):
        def _encode_clip(self, x):
            return x + 1

    proxy = LazyComponent("vae", FakeConfig(None),
                          lambda: WithPrivate(FakeConfig(None), elements=16), verbose=False)
    check("single-underscore methods forward", int(proxy._encode_clip(mx.array([1.0]))[0]) == 2)

    # Dunder probes (copy, pickle, repr protocols) must never trigger a load, whether Python finds
    # them on `object` or not.
    fresh = LazyComponent("vae", FakeConfig(None), lambda: None, verbose=False)
    for dunder in ("__deepcopy__", "__getstate__", "__setstate__", "__iter__"):
        try:
            getattr(fresh, dunder)
        except AttributeError:
            pass
        check(f"{dunder} does not trigger a load", not fresh.loaded)
    try:
        fresh.__deepcopy__
        raise AssertionError("an absent dunder should not be forwarded")
    except AttributeError:
        check("an absent dunder raises instead of forwarding to the loader", True)


def test_unload_releases_memory() -> None:
    mx.clear_cache()
    baseline = mx.get_active_memory()
    proxy = LazyComponent("weights", FakeConfig(None), lambda: FakeModel(FakeConfig(None)),
                          verbose=False)
    proxy.load()
    loaded = mx.get_active_memory()
    grew = (loaded - baseline) / 1e9
    check(f"loading is visible to the allocator (+{grew:.2f} GB)", grew > WEIGHT_GB * 0.8)

    proxy.unload()
    after = mx.get_active_memory()
    freed = (loaded - after) / 1e9
    check(f"unloading releases it (-{freed:.2f} GB)", freed > WEIGHT_GB * 0.8)
    check("and the proxy reports itself unloaded", not proxy.loaded)


class FakeEncoderWithUnload(FakeModel):
    """Like the real `QuantizedTextEncoder`, which drops its own sub-modules on unload."""

    def unload(self) -> int:
        released = self.weight.nbytes
        self.weight = None
        return released


def test_encoder_unloads_after_encode() -> None:
    """The whole point of task 4: 28.2 GB must not survive into the diffusion loop.

    Run twice — once on a component that only gets dropped by reference (which is what catches a
    stray local in the proxy) and once on one that also releases its own sub-modules.
    """
    for factory, label in ((FakeModel, "by reference alone"),
                           (FakeEncoderWithUnload, "with an unload() hook")):
        mx.clear_cache()
        baseline = mx.get_active_memory()
        proxy = LazyTextEncoder("text encoder", None,
                                lambda f=factory: f(FakeConfig(None)), verbose=False)
        embeds, tags = proxy.encode("hello")
        after = mx.get_active_memory()

        check(f"the encoder is gone once encode returns ({label})", not proxy.loaded)
        check(f"its memory went with it, {label} ({(after - baseline) / 1e9:+.2f} GB)",
              (after - baseline) / 1e9 < WEIGHT_GB * 0.2)
        check(f"the embeddings survive and are usable ({label})",
              embeds.shape == (4,) and float(embeds[0]) == 5.0, f"got {embeds}")
        check(f"the tags come back too ({label})", tags == ["tag"])


def test_unload_without_eval_would_not_free() -> None:
    """The control that gives the previous test its meaning.

    Dropping every reference to the model while a lazy graph still reads its weight frees nothing.
    This is what the pipeline would do without the `mx.eval` before the unload.
    """
    # Both halves use the same release primitive, so the only variable under test is the `mx.eval`.
    release()
    baseline = mx.get_active_memory()
    model = FakeModel(FakeConfig(None))
    lazy, _ = model.encode("hello")   # graph over model.weight, never evaluated
    loaded = mx.get_active_memory()
    del model
    release()
    still = mx.get_active_memory()
    check(f"an unevaluated graph pins the weights ({(still - baseline) / 1e9:.2f} GB still held)",
          (still - baseline) > 0.8 * (loaded - baseline),
          f"held {(still - baseline) / 1e9:.2f} of {(loaded - baseline) / 1e9:.2f} GB")

    mx.eval(lazy)
    del lazy
    release()
    check(f"and materializing then releasing does free them "
          f"({(mx.get_active_memory() - baseline) / 1e9:+.2f} GB)",
          (mx.get_active_memory() - baseline) / 1e9 < WEIGHT_GB * 0.2)


def test_reload_after_unload() -> None:
    calls = []
    proxy = LazyTextEncoder("text encoder", None,
                            lambda: (calls.append(1), FakeModel(FakeConfig(None), 16))[1],
                            verbose=False)
    proxy.encode("a")
    proxy.encode("b")
    check("a second request reloads rather than failing", len(calls) == 2, f"got {len(calls)}")

    kept = LazyTextEncoder("text encoder", None,
                           lambda: FakeModel(FakeConfig(None), 16),
                           verbose=False, unload_after_encode=False)
    kept.encode("a")
    check("unload_after_encode=False keeps it resident", kept.loaded)


def test_phase_tracker() -> None:
    tracker = PhaseTracker(verbose=False)
    tracker.mark("start")
    big = mx.zeros((WEIGHT_ELEMENTS,), dtype=mx.float32)
    mx.eval(big)
    tracker.mark("allocated")
    del big
    mx.clear_cache()
    tracker.mark("freed")
    check("three phases recorded", len(tracker.phases) == 3)
    check("the allocation shows up as a peak", tracker.phases[1][1]["mlx_peak_gb"] > WEIGHT_GB * 0.8,
          f"got {tracker.phases[1][1]['mlx_peak_gb']:.2f} GB")
    check("snapshot reports rss too", snapshot()["rss_gb"] > 0.0)


def main() -> int:
    tests = [
        test_config_is_free,
        test_load_on_first_real_use,
        test_private_attributes_forward,
        test_unload_releases_memory,
        test_encoder_unloads_after_encode,
        test_unload_without_eval_would_not_free,
        test_reload_after_unload,
        test_phase_tracker,
    ]
    for test in tests:
        print(f"{test.__name__}:")
        test()
    print(f"\n{len(tests)} test groups passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
