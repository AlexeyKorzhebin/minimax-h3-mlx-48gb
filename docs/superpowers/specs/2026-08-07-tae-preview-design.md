# TAE preview decoder — design

**Status:** approved 2026-08-07. Experimental: lives on its own branch and merges only if it produces usable frames.

## Why

An in-flight preview currently costs **49.3 s and 8.46 GB peak** at native resolution. The reason is
structural, not incidental: the real video VAE is causal and chunked, so it cannot decode fewer than
**7 latent frames**, and its spatial tiling runs 28 tiles at 1344×768 regardless. Every preview
therefore decodes roughly a second of video to show one frame, and loads a 5.21 GB module to do it.

TAE (`Kijai/MiniMax-H3-TAE`, 9.3 MB) is a plain 2D decoder. No temporal state, no chunk floor, no
tiling. If it works, a preview costs a fraction of a second and under a gigabyte, and the 7-frame
floor disappears — which also means previews become useful at native resolution, where they matter
most because a run is five hours long.

## What the weights actually are

Established from the safetensors header, not from documentation:

- 81 tensors, all F32, 9.78 MB
- Input `1.weight [96, 24, 3, 3]` — **24 channels, exactly our video VAE's `latent_channels`**
- Output `23.weight [3, 64, 3, 3]` — RGB
- Four upsample stages (slots 7, 12, 17, 21), each ×2 → **16× spatial**, matching our VAE's ratio
- Width steps 96 → 64 at slot 13, the only block carrying a seventh tensor (the residual projection)
- A flat `nn.Sequential`, indices 1..23, of `Conv2d` and three-conv residual blocks

So the architecture does not need guessing: the slot layout determines it.

## The open question this design must answer first

**In what normalisation does TAE expect its input?**

Our pipeline denormalises before the real VAE — `_decode_video` does `latents * std + mean` using
`latents_mean` / `latents_std` from the VAE config (`upstream/minimax_h3_mlx/pipeline.py:359-366`),
and `h3_48gb/preview.py:117-122` repeats it. TAE was trained by a third party and may expect either
form.

This is decided by measurement, not assumption: decode one latent both ways and compare against the
real VAE's output for the same latent. Whichever is closer is the answer, and the losing form must
look visibly wrong — if both produce plausible images, something else is off and the port is not
trustworthy yet.

## Design

**A new module, `h3_48gb/tae.py`**, containing the decoder and its loader. It does not touch
`preview.py`'s existing path.

**Selection at the call site.** `emit_preview` gains a decoder choice with three states: the real VAE
(today's behaviour), TAE, and the existing VAE-free latent heat map that already serves as the last
fallback. Default stays the real VAE until the measurement below says otherwise — an experimental
decoder must not become the default by being merged.

**Fallback order when TAE is selected:** TAE → latent heat map → skip, logging each step. The existing
guarantee holds unchanged: `emit_preview` never raises, because a preview failure must not kill a
five-hour render. That property is already covered by a test and must stay covered.

**Weights live outside the repository** (`~/models/tae/taeh3.safetensors`, 9.3 MB, already fetched).
They are third-party weights; we ship the loader, not the file. A missing file is a clean skip with a
log line, not an error.

## Verification

1. **Shape and load.** All 81 tensors map onto the module tree with no missing or unexpected keys —
   the same `strict` discipline the other loaders use.
2. **Normalisation decision.** One latent, decoded three ways: real VAE (the reference), TAE on
   normalised input, TAE on denormalised input. Report PSNR of each TAE variant against the
   reference. Record the numbers; pick the winner; state the loser's score so the margin is visible.
3. **Cost.** Time and peak memory for one TAE preview at 1344×768, measured the same way the 49.3 s /
   8.46 GB figure was. Both numbers go in the report.
4. **The failure path still holds.** With the TAE file absent and with a corrupt file, `emit_preview`
   returns without raising and logs the reason.
5. **A frame a human can judge.** Decode a real latent from an existing run and save the image beside
   the real VAE's decode of the same latent, for visual comparison. "Beats latent2rgb" is the bar the
   author himself set; if it does not clear that, this does not merge.

## What would make this not worth merging

Stated in advance so the decision is not made after the effort is sunk:

- TAE frames are not recognisably the same scene as the real VAE's decode
- the win is smaller than about 10× in time — below that, the added surface is not worth it
- the port needs changes to `upstream/` or to the real preview path

Any of these: record the finding, keep the branch, do not merge.

## Out of scope

Turbo LoRA — separate work, tracked in the backlog. Using TAE for the *final* decode: it is an
approximation for watching progress, never for the delivered clip.
