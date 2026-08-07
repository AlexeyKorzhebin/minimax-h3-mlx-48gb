# Measured results

Everything below was run on a MacBook Pro M4 Pro, 48 GB unified memory, with `iogpu.wired_limit_mb`
raised to 44 GB. All runs used the one schedule the baked AdaLN table covers —
`num_inference_steps=31` (31 grid points, 30 forwards), sigma shifts 12.0/3.0 — since any other
value fails at the `h3_48gb.adaln` layer before a single forward runs. Memory was watched with
`memwatch.sh` (RSS via `ps`, wired/compressed via `vm_stat`, swap via `sysctl vm.swapusage`) at a
5-second sample interval for the duration of each run.

## Per-run results

| Resolution | Clip length | Per step | Total | Peak RSS | Swap used |
|---|---|---|---|---|---|
| 512x512 | 2.4 s | 46 s | 24 min | 11.0 GB | 0 |
| 1344x768 (native) | 5 s | 586 s | 299 min | 10.0 GB | 0 |
| 1344x768 (native) | 10 s | 1881 s (2 steps measured) | ~15.7 h (extrapolated) | not measured to completion | not measured to completion |

The 10-second run at native resolution was measured for 2 steps and then **abandoned
deliberately** — at 1881 s/step the full 30-forward run projects to roughly 15.7 hours, which was
not worth running to completion just to confirm a number the first 2 steps already established.
It was a stop, not a crash: no error, no swap, no sign the process would not have completed if left
running.

## Scaling is worse than linear

Per-step cost across the three geometries: **46 s -> 586 s -> 1881 s**. Going from 512x512/2.4s to
1344x768/5s is roughly a 3.2x increase in per-step cost for a ~7x increase in pixel count (native
resolution is 1344x768 = 1,032,192 px vs 512x512 = 262,144 px) and a ~2.1x increase in frame count
at 24 fps; going from the 5s to the 10s native run is a 3.2x increase in per-step cost for a 2x
increase in frame count alone (geometry unchanged). That is not the profile of a compute cost that
scales with token count, i.e. linearly, or even a naive quadratic-in-sequence-length curve applied
cleanly — it is worse, because:

- **Attention is dense.** The whole packed sequence (text + keyframe conditions + audio rows +
  video rows) attends over itself with plain full self-attention — no cross-attention, no sparsity,
  no per-modality block weights (see `upstream/README.md`'s architecture table). MiniMax has not
  released a sparse-attention implementation for H3, so there is no cheaper path available to this
  port even in principle.
- **The bottleneck is attention FLOPs, not memory.** This is why quantization — which this fork
  already applies to the text encoder, and which helps residency — does not help throughput here.
  Quantizing the DiT's own weights would shrink what is resident, not the O(sequence_length^2)
  attention cost that dominates wall time at native resolution and longer clips.

Anyone tuning clip length or resolution against a time budget should expect the cost curve to bend
upward faster than the pixel or frame count alone would suggest, for exactly this reason.

## Memory profile

Two components dominate residency, and only one of them stays resident for the whole run:

| Phase | Resident | Notes |
|---|---|---|
| Text encoding | text encoder, Q8, 28.2 GB | Runs once, at the very start; unloaded immediately after (`h3_48gb/pipeline.py`) |
| Diffusion + decode | DiT + both VAEs + AdaLN cache + activations | Text encoder is gone by this point |

The unload is why the *process* peak (11.0 GB / 10.0 GB, from the table above) is so much lower
than either phase's component weight alone would suggest — the 28.2 GB encoder and the DiT/VAE
working set are never resident at the same time. Confirming the unload actually reclaims memory
(rather than merely dropping a Python name) took two steps, both in `h3_48gb/pipeline.py` and
`h3_48gb/memory.py`:

- **`mx.eval(prompt_embeds)` before anything else.** MLX is lazy; a computation graph that still
  references the encoder's parameters keeps all 28.2 GB alive no matter what else is deleted, until
  something forces evaluation.
- **`gc.collect()` before `mx.clear_cache()`.** Module trees and MLX graphs form reference cycles,
  so plain refcounting does not reclaim them — measured directly: without the `gc.collect()` call,
  the encoder stayed resident straight through the diffusion loop. Only after the cyclic collector
  runs does clearing MLX's allocator cache actually give memory back, since it can only release
  buffers nothing still refers to.

**Zero swap** across every run in the table above — the whole point of loading per phase instead of
all at once. mere.run's own admission control demands 32 GB free before it will start a job at all;
this fork's peak footprint fits inside that headroom.

## Previews

`h3_48gb/preview.py` decodes one frame from the current, partially-denoised latent every N steps,
so a run measured in hours is watchable rather than opaque. Two constraints, both measured rather
than assumed:

- **One preview costs 49.3 s and 8.46 GB peak** at native (1344x768) resolution. The video VAE is
  loaded on demand for the decode and unloaded again immediately after — it is not kept resident for
  the whole diffusion loop, which would stack its ~5.2 GB on top of the DiT's own peak activation
  moment.
- **The VAE cannot decode fewer than 7 latent frames.** `VideoVAE.decode` is a causal chunked
  decoder whose temporal padding assumes frame 0 of whatever it is given is the true start of the
  clip; it refuses fewer than `2 * chunk_tokens - token_drop` latent frames outright. For the
  released config that floor is 7 latent frames, about 22 pixel frames (~1 s at 24 fps) — there is
  no cheaper *correct* decode than that, and a preview always decodes from the clip's actual start,
  not from whichever moment is currently most interesting.

## Honest limits

- **Only `num_inference_steps=31` works.** The AdaLN table `h3_48gb/adaln.py` serves is precomputed
  for exactly one 31-point grid (30 forwards) at sigma shifts 12.0/3.0. Any other value raises
  `ScheduleMismatch` before the first forward, by design — there is no fallback to a nearest-value
  lookup.
- **A 10-second clip at native resolution is a ~15.7-hour commitment**, extrapolated from 2
  measured steps at 1881 s each. Nothing about that run failed; it simply was not run to completion.
  `h3_48gb/checkpoint.py` makes this tractable in practice — a crash or an intentional stop costs
  only the steps since the last checkpoint, not the whole run — but the wall-clock cost itself is
  real and this fork does nothing to reduce it.
- **No sparse attention.** MiniMax has not released one for H3, and full dense attention over the
  packed sequence is what makes the scaling above worse than linear. Quantization, which this fork
  already uses for the text encoder, addresses memory residency, not this cost, and would not help
  even if applied to the DiT.
- **Test suite: 71 tests**, none of which run a real generation (a single native step costs 586 s).
  Reproduce with:

  ```bash
  ./.venv/bin/python -m pytest tests/ test_preview.py -q
  ```
