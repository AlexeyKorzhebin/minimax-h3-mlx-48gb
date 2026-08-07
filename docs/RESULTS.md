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
diffusion phase holds about 11.5 GB — so reproducing the runs in the table does not require raising
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
`h3-mem-native10.csv` records an RSS of 0.09 GB at t=221 s, dropping to 0.04 GB at t=321 s, while the run's own log shows
it still working through steps of 1865 s and 1881 s. Nothing can be recovered from that trace, and
it is not reported. (The cause — `night_queue.sh` slept 10 s and then `pgrep`'d for a command line
— is fixed; the script now attaches to `$!` immediately.)

The 10-second run at native resolution was measured for 2 steps and then **abandoned
deliberately** — at 1881 s/step the full 30-forward run projects to roughly 15.7 hours, which was
not worth running to completion just to confirm a number the first 2 steps already established.
It was a stop, not a crash: no error, no sign the process would not have completed if left running.

## Scaling bends upward, but not everywhere

Per-step cost across the three geometries: **46 s -> 586 s -> 1881 s**.

| Step up | Tokens | Per-step cost | Exponent |
|---|---|---|---|
| 512x512/2.4s -> 1344x768/5s | x6.7 (3.9x pixels, 1.7x frames) | **x12.7** | tokens^1.34 |
| 1344x768/5s -> 1344x768/10s | x2.0 (frames only) | **x3.2** | tokens^1.68 |

Two corrections to what this section used to say. It reported the first step up as "roughly 3.2x",
which is wrong — 586/46 is 12.7, and the 3.2 belongs to the second row only. And it called the
curve "worse than a naive quadratic": both step ups are in fact *sub*-quadratic, at exponents 1.34
and 1.68 over token count. Growth is still faster than linear, and the second step up — where only
the sequence gets longer, with geometry fixed — is the steeper of the two, which is what the
mechanism predicts:

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
upward faster than the pixel or frame count alone would suggest, for exactly this reason: doubling
a clip's length roughly triples the cost of every step.

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
instrumentation gap is now narrowed (the monitor attaches to `$!` immediately), but since `night_queue.sh`
still uses a 10-second sample interval and the encoder finishes in 8.3 s, catching that phase remains
probabilistic. Closing it would require either a shorter sampling interval or attaching the monitor before the script launches the process; retroactively, it does not produce a sample anyway, and the runs were not repeated: a native step costs 586 s.

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
native5 trace, from 8.98 GB to 22.75 GB across its last 38 samples. That rise begins at t=17,581 s
— and 30 steps at 585.8 s each is 17,574 s, so it starts at the moment the final diffusion step
completes and the video VAE decode, the raw `.npz` write and the ffmpeg encode begin. The process's
own RSS is 1.28 GB at that moment, dropping to ~0.9 GB as the decode phase progresses. It describes the decode-and-teardown tail (and whatever
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
  more tests and which cover three of the four core patches (76 + 27 = 103). The remaining 12 tests
  were added in this precision-sweep commit to verify measured values against the actual data. A bare `pytest` used to fail instead,
  with five collection errors from `upstream/tests/*` importing torch; `norecursedirs` in
  `pyproject.toml` excludes the vendored clone, so the plain command is now the whole suite.

## Keyframe conditioning: verified, and it works

`--image`/`--end-image` (added on `feat/image-to-video`) parse, validate, and bind into checkpoint
identity — that much is covered by unit tests. Whether a keyframe actually **conditions** the clip,
as opposed to being silently accepted and ignored, has never been checked against the real
checkpoint, because every existing test replaces the pipeline with a fake
(`_default_pipeline_factory` is monkeypatched in `tests/test_cli.py` and friends). `scripts/verify_i2v.py`
was written to close that gap: generate the same prompt and seed twice at 512x512 — once with a
keyframe, once without — and compare each clip's first frame against the image by PSNR and
correlation, with the unconditioned control as the baseline that makes the comparison meaningful.
It has now run, twice, and the answer is yes — see **The measurement** below. Getting there took
three blockers, each hidden behind the one before it, and each costing a 25-minute run to surface.
All three are recorded here so the history is legible instead of overwritten.

**Blocker 1 — `torchvision`, fixed.** `h3_48gb/image_processor.py`'s `TorchFreeProcessor` (landed on
this branch, verified against real `transformers` output in `tests/fixtures/processor/`) now stands
in for the composite `AutoProcessor`, so `upstream/minimax_h3_mlx/text_encoder.py`'s
`build_request()` no longer touches `transformers.AutoProcessor.from_pretrained` for an image
request. Confirmed directly: a conditioned run now gets past `build_request()` — token ids, tags,
`pixel_values` and `image_grid_thw` all come back — and reaches `self.vision(...)`, the Qwen3-VL
vision tower's own forward pass.

**Blocker 2 — the vision tower's conv weight layout, fixed.** The next failure was inside the vision
tower itself, in `mx.conv3d`:

```
ValueError: [conv] Expect the input channels in the input and weight array to match but got
shapes - input: (4032,2,16,16,3) and weight: (1152,3,2,16,16)
```

Confirmed by inspecting the checkpoint directly: `~/models/h3-converted/text_encoder/model-00014-of-00014.safetensors`
stores `model.visual.patch_embed.proj.weight` as `(1152, 3, 2, 16, 16)` —
`(out_channels, in_channels, kD, kH, kW)`, PyTorch/HF's conv layout — while `mlx.nn.Conv3d` expects
channels-last, `(out_channels, kD, kH, kW, in_channels)`, exactly as its own constructor builds
`self.weight` (`mlx/nn/layers/convolution.py`). `upstream/minimax_h3_mlx/load.py`'s VideoVAE loader
already applies this exact transpose to every 5-D conv weight it loads
(`tensor.transpose(0, 2, 3, 4, 1)`, with a comment explaining why); `upstream/minimax_h3_mlx/text_encoder.py`'s
`_load_weights()` never got the same treatment for the vision tower's one convolution (everything
else in `VisionModel` is attention and linear layers, layout-agnostic). No test or run before this
task ever asked the real checkpoint to encode an image, so this was never exercised.

Fixed in this fork's own loader — `h3_48gb/text_encoder.py`'s `QuantizedTextEncoder._load_weights()`
now applies `to_mlx_conv3d_layout()` to `patch_embed.proj.weight` specifically, at load time, before
the module tree is used. `upstream/` is unmodified. Two things were added, deliberately, because a
wrong permutation here would not raise — it would silently scramble the patch embedding and show up
only as a worse clip, hours later, with the real checkpoint's kernel shape `(D, H, W) = (2, 16, 16)`
giving a wrong H/W-swapping permutation the exact same output *shape* as the right one:

- `test_vision_patch_embed_layout.py` pins values, not just shape — an independently-computed
  per-index reference (plain nested loops, not another call to `.transpose()`), plus a case with
  `H == W` matching the real checkpoint's collision, and a case that confirms a plausible wrong
  permutation is actually distinguishable from the right one (otherwise the value check would prove
  nothing). 7 tests, all passing.
- Verified against the real checkpoint directly (not via the CLI, to avoid paying for a full
  diffusion run to check one loader): `QuantizedTextEncoder(...).encode(prompt, [image])` now runs
  the real image through `patch_embed` without the shape error — strictly further than blocker 2
  allowed.

**Blocker 3 — fixed, as a patch against `upstream/`.** Past the fixed conv layer, `encode()` failed
one call later, still inside `upstream/minimax_h3_mlx/text_encoder.py`:

```
ValueError: [broadcast_shapes] Shapes (1,1026,1) and (1,1008,5120) cannot be broadcast.
```

at

```python
inputs_embeds = mx.where(image_mask[..., None], hidden.astype(inputs_embeds.dtype)[None], inputs_embeds)
```

`image_mask` has one entry per token in the *full* prompt (label + `<|vision_start|>` + one
`<|image_pad|>` per merged patch + `<|vision_end|>` + the text prompt itself — 1026 tokens for this
run); `hidden`, the vision tower's output, has one row per merged image patch only (1008 for this
run's 1344x768 keyframe). `mx.where` requires elementwise-broadcastable shapes, so this only works
when the whole prompt is image tokens with nothing else — never true in practice, since there is
always at least a `<Picture i>:` label and the prompt text. Confirmed this is a real bug rather than
a misuse: `mlx_vlm`'s own `qwen3_vl.py` (`.venv/lib/python3.12/site-packages/mlx_vlm/models/qwen3_vl/qwen3_vl.py`)
implements the equivalent operation correctly, as `masked_scatter()` — flatten, index the True
positions, assign, reshape — specifically because a boolean-masked `mx.where` can't do this.
`upstream/minimax_h3_mlx/text_encoder.py`'s hand-rolled version does not use it. The processor's own
`pixel_values`/`image_grid_thw` were checked directly and are internally consistent (4032 raw
patches, 1008 after the checkpoint's `merge_size=2`) — the mismatch is between the image-token count
and the full sequence length, not a processor bug.

This one is a control-flow bug in the middle of `encode()`, not a weight layout fixable before the
call, so no loader-side workaround exists. Copying `encode()` into a subclass to change one line
would have meant carrying 40 lines of upstream logic that then drift apart silently. It is carried
as `patches/0001-keyframe-masked-scatter.patch` instead — `upstream/` is gitignored, so an edit made
there would exist on one machine and nowhere else. `README.md`'s setup applies it; `--image` without
it is refused up front with `upstream_patch_missing`, before any weight loads, rather than crashing
28.2 GB into a run. Text-only runs never reach the line.

### Five silent divergences, found by reading against upstream's own reference

With the run finally completing, the remaining defects were the dangerous kind: none of them raise.
They were found by comparing this fork against `upstream/reference/diffusers/`, which ships in the
vendored checkout, and each is now fixed and pinned by a test.

| What was wrong | How it showed | Where |
|---|---|---|
| Seed 42 taken once per request, not once per keyframe | Two byte-identical keyframes encoded 0.83 apart; the reference makes them identical | `h3_48gb/pipeline.py` |
| Our lazy VAE construction consumed the RNG stream between `seed(42)` and the draw | A cold and a warm run of the same request differed by 0.87 | `h3_48gb/pipeline.py` |
| The vision tower saw the raw image, the VAE the canvas version | Vision-token count changes, which shifts the rotary clock of every media row | `h3_48gb/pipeline.py` |
| The canvas ignored the keyframe's aspect | A portrait photo was stretched into 16:9, since keyframe 0 is stretched, not fitted | `h3_48gb/cli.py` |
| `preprocessor_config.json` was synthesized by the converter | Values turned out correct — verified against MiniMaxAI/MiniMax-H3 — but only in the one spelling mlx-vlm reads | `convert_sawfwair.py` |

That last one is worth stating plainly, because the obvious "fix" is a regression: the official file
writes the pixel budget as `size: {shortest_edge, longest_edge}`, a key mlx-vlm's processor does not
have. Handed the official config verbatim, it silently keeps Qwen2-VL's defaults and caps a keyframe
at 1,003,520 pixels instead of 16,777,216 — a 4K keyframe would then yield 943 image tokens where
the released model produces 8160, with no exception raised.

### A canary, so the next blocker costs minutes

`scripts/canary_i2v.py` walks the entire i2v path — encoder, keyframe encode, packing, forward,
decode — with the denoising loop cut to two forwards. **3 minutes instead of 25, and 8 seconds to
reach a failure inside the encoder.** It calls the real `__call__` rather than reimplementing the
loop, because a canary that reimplements the pipeline verifies the copy. Only the loop's timestep
list is truncated: the schedule and the AdaLN table are built in full, since `check_schedule`
compares the sigma grid elementwise and refuses anything else. `--no-image` runs the identical path
without a keyframe, which is what distinguishes "the i2v path is broken" from "the canary is broken".

### The measurement

Both pairs are the same prompt and seed run twice, once with a keyframe and once without. The
control is the point: without it, a high score would only show that the clip agrees with the prompt.

| Keyframe | Canvas | Conditioned | Control | Conditioned corr | Control corr |
|---|---|---|---|---|---|
| Synthetic checkerboard 512x512 | 512x512 | **34.81 dB** | 10.35 dB | **0.999** | 0.041 |
| Photograph 1536x1024 (3:2) | 576x384 | **26.30 dB** | 9.81 dB | **0.979** | 0.314 |

The two runs are deliberately different tests. In the first, the prompt (*"a red vintage car parked
on a wet street at night"*) **contradicts** the keyframe, so a first frame that reproduces the
checkerboard can only come from the conditioning — which is why the control scores 0.041. In the
second the prompt agrees with the image, which is the harder case: the model would draw a dragon
over mountains regardless, and the control's 0.314 correlation is exactly that shared composition.
The conditioned run still clears it by 16.5 dB.

The photograph scores lower than the checkerboard for a reason that is not a defect: a keyframe
makes a round trip through the VAE, and flat colour blocks survive that far better than scales,
cloud and foliage.

The second pair also ran on the fixed code and on a 3:2 source — the case that, before the canvas
fix, would have been stretched into 16:9 without comment.


## How this fork compares to another Mac port

A ComfyUI-based port (`Bambushu/minimax-h3-mac`) published per-step figures for the same model on
comparable geometry, which is a rare chance to check whether this fork leaves performance on the
table. Measured with the canary on that port's own geometry:

| | this fork | Bambushu port |
|---|---|---|
| Canvas / duration | 1344x768, 3 s (73 frames) | 768x1376, 3 s |
| Packed sequence | 22,434 rows | "about 22k tokens" |
| Chip | **M4 Pro**, 20 GPU cores | **M5 Pro** |
| DiT precision | 4-bit | int8 |
| **Per step** | **262 s** | **131.6 s** |

Both figures are sampling only, excluding load and decode. Ours is the per-step time the pipeline
itself prints, and the two forwards measured 262.1 s and 261.6 s — within 0.2% of each other, so
neither carries hidden warm-up or load cost. The 2x gap is a chip generation and a
quantization choice, not headroom in this code: M5's GPU carries neural accelerators per core, and
int8 is a different trade than the 4-bit weights this fork uses to fit 48 GB at all. Attention here
already runs through `mx.fast.scaled_dot_product_attention`, so there is no obvious slow path to
reclaim.

One thread is worth pulling, though: that port's *whole-clip* time for a 5-second native render is
116.8 minutes against this fork's 299 — a 2.56x gap, wider than the 1.99x per step. Some of it is
outside sampling. This fork's video decode at 1344x768 measures 208 s, which is where to look first.
