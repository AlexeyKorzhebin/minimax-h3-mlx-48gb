# Reference renders

Eight clips of `prompts/centaur-battle.txt` produced by someone else's ComfyUI installation, kept
here because every quality claim in `docs/RESULTS.md` about how this fork compares to a working
MiniMax-H3 setup is measured against them.

**The clips themselves are not in git — only this file is.** `.gitignore` excludes `*.mp4`, and
`NOTICE` states that this project does not host or redistribute MiniMax H3 weights or their output;
committing eight of someone else's renders would contradict that. They live on disk beside this
README on the machine that measured them. Anyone reproducing the numbers below needs their own copy
of equivalent renders, which is why this file records exactly what produced these ones.

They exist because measuring our own output against itself could not answer the question that
started the investigation — "why does ours look grainy" — and the clips we first compared against
(`soldiers.mp4`, a Reddit repost) turned out to be a different prompt, downscaled to 608x352, which
suppresses exactly the thing being measured.

## What produced them

Not the bf16 release. A ComfyUI 0.31.0 install running quantized weights:

| component | file |
|---|---|
| DiT | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| video VAE | `minimax_h3_video_vae_fp16.safetensors` |
| audio VAE | `minimax_h3_audio_vae_fp32.safetensors` |

That matters for reading any comparison against them:

- The **DiT is int8** with rotation-based quantization, against this fork's 4-bit (or 8-bit) affine
  at group 64. The comparison is between two quantized transformers of different width, not
  between a quantized one and full precision.
- The **text encoder is 4-bit**, less precise than this fork's 8-bit. Conditioning is not where we
  are behind.
- **Both VAEs match this fork's formats exactly** — fp16 video, fp32 audio. The latent-to-pixel
  path is not a variable.
- `fl2va` is the same partition this fork converts, so the weights are comparable at all.

Sigma shift is unrecorded; if their graph has no `MiniMaxH3SigmaShift` node it is ComfyUI's default.

**These clips were rendered through a decoder with a known defect.** ComfyUI issue #15416 reports
the H3 video VAE decoding badly at >=512 px — mean absolute error 4.6 against the reference
implementation, rising to 31.4 tiled with 256 px seams and banding — and as of 2026-08-10 it is
still open, with no fixing PR and no linked branch, so 0.31.0 carries it. Every gap measured
against these clips is therefore a **lower bound**: the reference is handicapped and still ahead.

Whether this fork's decode is clean is a separate question with its own evidence — the port claims
encode+decode round-trips to 1.2e-06 tiled and untiled — but note what that claims and does not:
agreement with the reference *implementation*, not freedom from artifacts the reference
implementation itself has.

## The files

All 10.125 s, 243 frames, 24 fps, H.264.

| file | steps | canvas | seed |
|---|---|---|---|
| `centaur-20step-896x576-legacy1.mp4` | 20 | 896x576 | not recorded |
| `centaur-20step-896x576-legacy2.mp4` | 20 | 896x576 | not recorded |
| `centaur-20step-896x576-legacy3.mp4` | 20 | 896x576 | not recorded |
| `centaur-20step-1248x832-legacy.mp4` | 20 | 1248x832 | not recorded, a separate render rather than an upscale |
| `centaur-20step-896x576-seed635988198379787.mp4` | 20 | 896x576 | 635988198379787 |
| `centaur-20step-1248x832-seed635988198379787.mp4` | 20 | 1248x832 | 635988198379787 |
| `centaur-8step-896x576-seed635988198379787.mp4` | 8 | 896x576 | 635988198379787 |
| `centaur-8step-1248x832-seed635988198379787.mp4` | 8 | 1248x832 | 635988198379787 |

The three legacy 896x576 clips are what give the reference a **measured seed-to-seed spread** rather
than a single number, and that is most of their value: a comparison against one sample cannot say
whether a difference is real.

The seed does **not** reproduce these renders here. `minimax_h3_mlx.pipeline` notes it directly —
MLX's RNG is not torch's, so the same integer draws different noise. What the fixed seed buys is a
controlled series on *their* side: the 8-step and 20-step clips at each canvas differ in step count
and nothing else, which is what let the gap be split between step count and our build.

## What they measured

See "The two bands are scales, not noise and detail" in `docs/RESULTS.md`. In short, at matched
canvas and matched 8 steps, this fork's 4-bit output carries **26% more** fine-scale (2-3 px) energy
than the reference and **29% less** mid-scale (8-16 px). The deficit is specific, not a general
softness, and it splits roughly evenly between step count and this fork's own build.
