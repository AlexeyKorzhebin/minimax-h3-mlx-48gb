# Measured results

Everything below was run on a MacBook Pro M4 Pro, 48 GB unified memory. All runs used the one
schedule the baked AdaLN table covers — `num_inference_steps=31` (31 grid points, 30 forwards),
sigma shifts 12.0/3.0 — since any other value fails at the `h3_48gb.adaln` layer before a single
forward runs. Memory was watched with `memwatch.sh` (RSS via `ps`, wired/compressed via `vm_stat`,
swap via `sysctl vm.swapusage`) at a **10-second** sample interval for the duration of each run,
driven by `night_queue.sh`, which passes that interval explicitly.

`iogpu.wired_limit_mb` was raised to 45056 (44 GB) for these runs. That is **methodology, not a
prerequisite**: it was set to leave headroom for the heaviest configuration in the planned series
(15 s at native resolution, ~42 GB projected), which was never actually run. Nothing that *was* run
came close — the largest MLX allocation observed at any point is the 28.22 GB text encoder, and the
diffusion phase holds about 12 GB — so reproducing the runs in the table does not require raising
the limit. It resets on reboot in any case.

## A correction: how these numbers were re-derived

The figures in this file were published once from a corrupted instrument, and the corrected values
below come from re-parsing the archived CSVs rather than from re-running anything.

`memwatch.sh` used bash `printf '%.2f'`, which honours `LC_NUMERIC`. Under the author's
`LANG=ru_RU.UTF-8` every `%.2f` emitted a comma decimal, so each row of a nominally 6-column CSV
arrived with **11 comma-separated fields** — and bash's `printf` additionally *rejected* `bc`'s
dot-decimal output as malformed, writing `0,00` for `wired_gb` and `compressed_gb` in all 2,490
samples of all three runs. `night_queue.sh` then read column 2 as "peak RSS" (the integer part of
the RSS) and column 5 as "swap" (the decimal part of the always-zero wired figure), which is why
every run reported a suspiciously round RSS and exactly `0.0` swap.

The fix is `export LC_ALL=C` at the top of `memwatch.sh` (and of `night_queue.sh`, whose summary
`awk`s format numbers too); the CSV is now 6 fields under both `C` and `ru_RU.UTF-8`, verified in
both. Two consequences worth naming:

* **`wired_gb` and `compressed_gb` recorded nothing at all** for the archived runs. Those two
  columns are unrecoverable — there is no partial value to salvage, only zeros — so nothing in this
  file is based on them.
* Every RSS and swap figure below was recovered by splitting the 11 fields back into 6.

## Per-run results

| Resolution | Requested | Frames (at 24 fps) | Per step | Total | Peak RSS | Swap |
|---|---|---|---|---|---|---|
| 512x512 | 2.4 s | 73 (3.04 s) | 46.1 s | 24.5 min | 11.54 GB | none consumed; 12.01 → 9.36 GB |
| 1344x768 (native) | 5 s | 124 (5.17 s) | 585.8 s | 299.3 min | 10.14 GB | none consumed; 10.94 → 8.98 GB |
| 1344x768 (native) | 10 s | 243 (10.13 s) | 1881 s (2 steps measured) | ~15.7 h (extrapolated) | not measured | not measured |

"Requested" is the `--duration` passed on the command line. The port snaps it to the latent grid,
so the clip that comes out is longer than the one asked for — 2.4 s of request became 73 frames,
which is 3.04 s at 24 fps.

The 10-second run has no memory figures because its monitor attached to the wrong process:
`h3-mem-native10.csv` records an RSS of 0.04 GB from t=220 s onward while the run's own log shows
it still working through steps of 1865 s and 1881 s. Nothing can be recovered from that trace, and
it is not reported. (The cause — `night_queue.sh` slept 10 s and then `pgrep`'d for a command line
— is fixed; the script now attaches to `$!` immediately.)

The 10-second run at native resolution was measured for 2 steps and then **abandoned
deliberately** — at 1881 s/step the full 30-forward run projects to roughly 15.7 hours, which was
not worth running to completion just to confirm a number the first 2 steps already established.
It was a stop, not a crash: no error, no sign the process would not have completed if left running.

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

### Three numbers, two instruments

The honest shape of this fork's memory behaviour is not one peak but three, and they were not all
measured the same way. Stating that plainly matters more than a tidy headline:

| Phase | Figure | Where it comes from |
|---|---|---|
| All four components resident | **~55 GB** | 45.9 GB of weights + 9.3 GB of activations (below) |
| Text-encoding phase, ~10 s long | **28.2 GB** | MLX's `get_active_memory`, printed by the run itself |
| Diffusion phase, the other 99% of the wall clock | **11.54 / 10.14 GB** | process RSS via `ps`, sampled every 10 s |

**The 45.9 GB** is derived from this fork's own run log (`h3-gen-native5.log`), which prints each
component's cost as MLX's allocator sees it at load: text encoder 28.22 + transformer 11.34 + video
VAE 5.21 + audio VAE 0.61 = 45.38 GB, plus the 0.56 GB AdaLN cache. **The 9.3 GB** is not this
fork's measurement — it is upstream's own, from its README's sequence-length table for
1344x768 / 5 s on an M3 Ultra. Their sum, 55.2 GB, is where the "55.5 GB" this project has quoted
since its planning notes comes from; the weights half of it is now re-derived here, the activation
half is still upstream's number and is labelled as such.

**The 28.2 GB is not an RSS measurement, and the RSS trace never saw that phase.** MLX prints
`loaded text encoder: +28.22 GB in 8.3s`, and that is what the figure is: the allocator's own
accounting for the encoder's weights. Meanwhile `night_queue.sh` slept 10 s before attaching
`memwatch.sh` — and the encoder finished loading in 8.3 s and was unloaded immediately after — so
the first RSS sample of every archived run was taken after the encoding phase was already over.
There is no measurement of process RSS during text encoding in any of the three runs. The
instrumentation gap is now closed (the monitor attaches to `$!` immediately), but closing it does
not retroactively produce a sample, and the runs were not repeated: a native step costs 586 s.

So: 28.2 GB and 11.5 GB come from different tools and were never observed together. What can be
said with both in hand is that the encoder's 28.22 GB is *allocated and released* before the
diffusion loop starts, and that during the loop the process's RSS is flat at 9.04 GB for 1,751
consecutive samples with a 10.14 GB peak. What cannot be said is that any instrument watched the
transition.

### Why the diffusion peak is what it is

| Phase | Resident | Notes |
|---|---|---|
| Text encoding | text encoder, Q8, 28.2 GB | Runs once, at the very start; unloaded immediately after (`h3_48gb/pipeline.py`) |
| Diffusion + decode | DiT + both VAEs + AdaLN cache + activations | Text encoder is gone by this point |

The unload is why the diffusion phase costs 10–11.5 GB rather than that plus the encoder's 28.2 GB.
It does **not** explain the 10.14 GB figure on its own: the DiT alone loads at 11.34 GB, more than
the peak RSS ever reached during the native run. Two things account for that, and neither is the
unload:

- **RSS is not the allocator's view.** MLX's `+11.34 GB` counts the buffers it allocated; `ps`
  reports pages this process has resident. Weights read from a memory-mapped safetensors file are
  file-backed and clean, so the kernel can evict them from the resident set under pressure and
  fault them back on use without any swap traffic at all. RSS legitimately sits below allocated
  weight for a memory-mapped, mostly-read-only working set.
- **Which is why RSS is the sanity check and not the measurement**, exactly as
  `h3_48gb/memory.py` says in its module docstring. The claim RSS supports is "this process is not
  thrashing and is not driving the machine into swap", which the traces do support. The claim it
  does not support is "the model's working set is 10.14 GB".

Confirming the unload actually reclaims memory (rather than merely dropping a Python name) took two
steps, both in `h3_48gb/pipeline.py` and `h3_48gb/memory.py`:

- **`mx.eval(prompt_embeds)` before anything else.** MLX is lazy; a computation graph that still
  references the encoder's parameters keeps all 28.2 GB alive no matter what else is deleted, until
  something forces evaluation.
- **`gc.collect()` before `mx.clear_cache()`.** Module trees and MLX graphs form reference cycles,
  so plain refcounting does not reclaim them — measured directly: without the `gc.collect()` call,
  the encoder stayed resident straight through the diffusion loop. Only after the cyclic collector
  runs does clearing MLX's allocator cache actually give memory back, since it can only release
  buffers nothing still refers to.

### Swap

This project previously claimed **zero swap**. That was false, and an artifact of the locale bug
above: the column being read as swap was the always-zero decimal half of the `wired_gb` field.

What the re-parsed CSVs actually show is better than the claim it replaces. The machine had
**8.98–12.01 GB of swap in use throughout**, from everything else running on it — `sysctl
vm.swapusage` is machine-wide and has no per-process view, so none of it is attributable to these
runs either way. The signal is the direction, and in both measured runs it points down:

| Run | Swap at first sample | At the end of the diffusion phase | Change |
|---|---|---|---|
| smoke, 512x512, 24 min | 12.01 GB (t=0) | 9.36 GB (t=1454 s) | **-2.65 GB** |
| native5, 1344x768, 5 h | 10.94 GB (t=0) | 8.98 GB (t=17,570 s) | **-1.96 GB** |

Swap in use fell **monotonically** across both diffusion phases — checked sample by sample, not
just endpoint to endpoint: of the smoke run's 147 samples, not one is higher than the sample before
it until the process has already exited; of the native5 run's 1,791 samples, the first increase of
any kind is at t=17,581 s. A run that needed more memory than the machine had would push this
number up; instead the machine **reclaimed** swap while generating. That is the claim the data
supports: *these runs did not consume swap, and the machine gave some back while they ran.*

The caveat, so the table is not read as more than it is. Swap does rise at the very end of the
native5 trace, from 8.98 GB to 22.75 GB across its last 20 samples. That rise begins at t=17,581 s
— and 30 steps at 585.8 s each is 17,574 s, so it starts at the moment the final diffusion step
completes and the video VAE decode, the raw `.npz` write and the ffmpeg encode begin. The process's
own RSS has dropped to ~0.9 GB by then. It describes the decode-and-teardown tail (and whatever
else the overnight queue was starting), not the phase the table is about. Swap during the diffusion
phase itself never exceeded its opening 10.94 GB.

`sysctl vm.swapusage` is machine-wide, so none of these movements can be attributed to the run with
certainty in either direction — which is exactly why the claim above is about direction and bound,
not attribution.

mere.run's own admission control demands 32 GB free before it will start a job at all; this fork's
peak footprint fits inside that headroom.

## Previews

`h3_48gb/preview.py` decodes one frame from the current, partially-denoised latent every N steps,
so a run measured in hours is watchable rather than opaque. Enable it with
`h3 generate --preview-every N`; it is off by default. Two constraints, both measured rather
than assumed:

- **One preview costs 49.3 s and 8.46 GB peak** at native (1344x768) resolution. **Measured with
  correctly-shaped random weights, not with the real checkpoint** — see `h3_48gb/preview.py`, which
  makes the same disclosure. The argument for why that is still a fair measurement is that VAE
  decode cost is a function of shapes and dtypes, not of weight values: the same 28 tiles walk the
  same 36-layer decoder either way. The argument against is that it was never confirmed against the
  real weights, so treat it as an estimate with a sound basis rather than as a measurement of the
  shipped path. The video VAE is
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
  lookup. This is a limitation of *this build*, not of H3:
  [`FEASIBILITY-turbo-tae.md`](FEASIBILITY-turbo-tae.md) locates the full modulation path in
  8-dimensional curve form inside ComfyUI's pruned checkpoint (87 MB, reproducing this fork's baked
  table to 2.2e-3 rel-L2). Unimplemented, and derived from safetensors headers rather than from a
  run — but it is a mapped way out rather than a permanent ceiling.
- **A 10-second clip at native resolution is a ~15.7-hour commitment**, extrapolated from 2
  measured steps at 1881 s each. Nothing about that run failed; it simply was not run to completion.
  `h3_48gb/checkpoint.py` makes this tractable in practice — a crash or an intentional stop costs
  only the steps since the last checkpoint, not the whole run — but the wall-clock cost itself is
  real and this fork does nothing to reduce it.
- **No sparse attention.** MiniMax has not released one for H3, and full dense attention over the
  packed sequence is what makes the scaling above worse than linear. Quantization, which this fork
  already uses for the text encoder, addresses memory residency, not this cost, and would not help
  even if applied to the DiT.
- **Test suite: 115 tests**, none of which run a real generation (a single native step costs 586 s).
  Reproduce with:

  ```bash
  ./.venv/bin/python -m pytest -q
  ```

  This file previously documented `pytest tests/ test_preview.py -q` and claimed 71 tests. That
  command collected 76, and hid the four root-level files — `test_adaln_indexing.py`,
  `test_lazy_pipeline.py`, `test_qkv_permutation.py`, `test_text_encoder_quant.py` — which are 27
  more tests and which cover three of the four core patches. A bare `pytest` used to fail instead,
  with five collection errors from `upstream/tests/*` importing torch; `norecursedirs` in
  `pyproject.toml` excludes the vendored clone, so the plain command is now the whole suite.
