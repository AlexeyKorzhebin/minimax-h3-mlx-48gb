---
name: generating-h3-video
description: Use when generating video or audio with MiniMax H3 on this Apple Silicon Mac, choosing a run's mode/resolution/duration/step count, conditioning on a keyframe, or diagnosing a slow, interrupted, refused-to-start, or schedule/memory-error H3 run
---

# Generating video with MiniMax H3 on a 48 GB Mac

Runs are minutes to hours and cost the same whether the flags were right. Decide the mode, the
canvas and the step count *before* launching, and say the predicted cost out loud first.

## Never launch a run outside `caffeinate`

```bash
caffeinate -dimsu <the run, or the queue script driving several>
```

Wrap the whole script, not one `generate`, so the assertion covers the gaps between runs. Nothing
here takes a sleep assertion, and a run pins the GPU while the keyboard and display sit idle —
which `powerd` reads as an idle machine. On 2026-08-10 that cost a 1344x768 run at step 2 of 7: the
Mac idle-slept and the GPU firmware panicked coming out of it, `agx_power failed to transition to
state 0`. Check `pmset -g` first; `sleep 1` and `displaysleep 30` are the defaults and both are
fatal. Logs go on disk, never `/tmp` — the reboot that kills the run wipes the evidence with it.

## The three modes

One checkpoint (`fl2va` partition) serves all of them; the mode is decided by which flags appear.

| Mode | Flags | What it does |
|---|---|---|
| **t2va** — text to video+audio | no `--image` | The whole clip from the prompt. |
| **i2v** — first frame conditioning | `--image photo.jpg` | The clip starts from that frame. The canvas is *derived from the image* unless both `--width` and `--height` are given; passing only one is refused (`partial_canvas_with_image`). |
| **first+last** | `--image a.jpg --end-image b.jpg` | Interpolates between two anchors. `--end-image` alone is refused. |

The model was trained on exactly those two keyframe arrangements, so the flags deliberately cannot
express a third. **`ref2va`** — conditioning on reference images of a character or style rather
than on frames — is a different weight partition this fork does not convert. There is no flag for
it and adding one would silently produce nonsense.

A keyframe is *stretched* onto the canvas without preserving aspect, which is why the canvas
follows the image by default. EXIF rotation is applied before the size is read.

## Step count is free, but each count needs its own AdaLN table

The old rule "always `--steps 31`, never change it" is retired and was wrong. What is true: this
build ships no AdaLN modulation projections at all — they are precomputed into a table — so a run
can only use a step count some table covers. The checkpoint's own table is 31 grid points; anything
else needs one baked first:

```bash
python scripts/bake_adaln.py 8 --out ~/models/turbo/adaln_8_plain.safetensors
h3 generate "<prompt>" --steps 8 --adaln-cache ~/models/turbo/adaln_8_plain.safetensors
```

Baking is seconds and a few hundred MB. `--steps N` counts **grid points**, and the run does N-1
forward passes: `--steps 8` is 7 forwards. A mismatch between `--steps` and the table is refused
before any weight loads (`schedule_not_baked`).

`--schedule tail-split` bakes a grid whose prefix is bit-identical to `simple`'s and subdivides only
the final Euler step — the way to change the tail without changing the composition.

## Sizing a run

```
rows    ~ (5.53 + 1.641 * (seconds - 2.4)) * (W/16) * (H/16) + 81 * seconds + text_rows
seconds ~ 5.699e-3 * rows + 2.671e-7 * rows**2      # one forward
```

Fitted on five measured forwards from 6,671 to 73,061 rows, every point within 3%. Multiply by
(grid points - 1) for the diffusion time; loading is a couple of minutes and decoding scales on its
own. Worked points, 8 grid points:

| Canvas | Duration | Rows | Diffusion | Wall clock |
|---|---|---|---|---|
| 896x512 (default) | 2.4 s | 10,375 | 10.3 min | 12.5 min |
| 768x768 | 2.4 s | 13,191 | 14.2 min | 16.3 min |
| 896x576 | 10 s | 37,657 | 69 min | 78 min |
| 1344x768 (native) | 5 s | 40,202 | 77 min | not measured |
| 1344x768 (native) | 10 s | 73,686 | 218 min | not measured |

Wall clock is diffusion plus loading and decoding, and those two are a couple of minutes at
2.4 s and much more at ten. Quote the column you actually mean.

Attention is quadratic and everything else linear, which is why no single exponent ever fit: the
quadratic term is 21% of the cost at 512x512 and 77% at native resolution. Ten seconds is not twice
five; it is nearly three times.

Width and height must each be a multiple of 32, checked before any weight loads.

## Memory is not the constraint; wall clock is

The peak is **27 GB on every canvas tried**, from 512x512 to 1344x768, and it belongs to the text
encoding phase — not to diffusion, which runs at 13-15 GB on small canvases and 24 GB on an 8-bit
build. Nothing in that range is memory-limited.

The exception worth knowing: an 8-bit DiT is 21.35 GB resident, and a 15 s native clip is projected
past 24 GB of activations. That sum is ~46 GB on a 48 GB machine, so at native resolution *and*
long duration the 8-bit build becomes the peak. Short clips on middling canvases pay nothing for it.

## Getting quality for the money

Measured against reference renders of the same prompt from a working ComfyUI install (see
`reference/README.md`), the gap was never excess noise — it was missing mid-scale detail, and it
split roughly evenly between step count and this fork's own quantization. The levers, cheapest
first:

- **Use the 8-bit DiT build.** Same wall clock, same peak memory, and at 8 steps it reaches what 16
  steps reached on the 4-bit build. Point `--checkpoint` at it.
- **Turbo LoRA at strength 1.0** (`--turbo-lora <path>`, the default strength). Adds roughly what
  doubling the steps adds, for free, and lands motion at 113% of a 31-step reference where 8 steps
  alone manage 52%. Lower toward 0.8 only if the image over-sharpens.
- **Spend pixels before steps when steps are short.** Rendering bigger recovers most of what few
  steps cost: on the reference model, 896x576 gains 31% of mid-scale detail going 8 -> 20 steps,
  while 1248x832 gains 3% — it already had it. Faces specifically need pixels and are not helped by
  steps at all: over three seeds, 768x768 beat 512x512 on the mean and had a third of the spread.
- **More steps, last.** They saturate before 31, and 50 measured slightly *worse* than 31 at almost
  six times the cost. Upstream's advice to use 50 did not reproduce here.

## Watching and resuming

Previews are on by default every 5 steps, decoded by TAE in 0.125 s — the flag exists mostly to
turn them off (`--preview-every 0`) or to pay 49 s for a real VAE preview (`--preview-decoder vae`).
They land as `<stem>-preview-stepNN.jpg` beside where the mp4 will be.

Every step is checkpointed under `<outdir>/checkpoints`. Re-running the identical `generate`
command resumes bit-identically; `h3 resume` does the same but *asserts* a matching checkpoint
exists instead of silently starting over. The identity is prompt + width + height + duration +
steps + seed + tag + which `--checkpoint`, `--outdir`/`--checkpoint-dir`, adaln table, LoRA and
strength — change any of them and it is a different run. `--restart` is the way out of a
`checkpoint_mismatch` refusal.

## Before spending compute

`h3 doctor --checkpoint <dir> --json` verifies all four components and the AdaLN cache in seconds.
Run it against a checkpoint you have not used before, rather than discovering a gap an hour in.

`reference.md` has the complete flag list for every subcommand and the full `--json` error table.
