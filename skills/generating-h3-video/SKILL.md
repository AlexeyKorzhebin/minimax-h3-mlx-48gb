---
name: generating-h3-video
description: Use when generating video or audio with MiniMax H3 on this Apple Silicon Mac, sizing a run's resolution/duration/step count, or diagnosing a slow, interrupted, refused-to-start, or schedule/memory-error H3 run
---

# Generating video with MiniMax H3 on a 48 GB Mac

A native 1344x768, 5-second clip takes about five hours end to end, and nothing about this run is
cheap to cancel and restart. Get resolution, duration and `--steps` right *before* you launch —
the wrong choice costs a night, not a retry.

## Symptom -> cause

| Symptom | Cause | What to do |
|---|---|---|
| `ScheduleMismatch`, or `--json` error code `schedule_not_baked` | The shipped AdaLN table is baked for exactly one schedule: `num_inference_steps=31` (31 grid points, 30 forwards), sigma shifts 12.0/3.0. No other step count can be evaluated. | Always pass `--steps 31` (the default). Never change it. |
| `--json` error code `geometry_not_multiple_of_32` | `--width`/`--height` must each be a multiple of 32 — checked before any weight loads. | Round the requested resolution to the nearest multiple of 32 first. |
| Not sure a checkpoint is usable, or a run refuses to start | The converted checkpoint is missing a required component or the AdaLN cache. | Run `h3 doctor --checkpoint <dir> --json` before anything else — it costs seconds, not hours. |
| `--json` error code `checkpoint_not_found` from `h3 resume` | No checkpoint under `<outdir>/checkpoints` matches this run's prompt/geometry/duration/steps/seed/tag. | Check every flag matches the interrupted `generate` call exactly, or just re-run the identical `generate` command — it auto-resumes on its own. |
| `--json` error code `checkpoint_mismatch` / `checkpoint_corrupt` | A checkpoint file exists but was written for a different request, or cannot be read. | The error message names the exact file (`<outdir>/checkpoints/h3-<digest>.safetensors`) — move or delete that specific file. A new `--tag` also works on its own (it is part of the checkpoint identity, so it gets a fresh file) but leaves the stale one on disk; delete it if you don't need it as evidence. |
| Run looks stalled, no output for a long time | Expected — a native step is ~586 s, and plain `h3 generate` (no `--json`) prints only one `checkpoint: N/M steps` line per completed step. | Wait, or check the checkpoint file's mtime under `<outdir>/checkpoints`; see "watch a run" below. |

## Workflow

1. **Validate before spending any compute.** `h3 doctor --checkpoint <dir> --json` checks that all
   four required components (`transformer`, `text_encoder`, `video_vae`, `audio_vae`) and the AdaLN
   cache are present. Seconds, versus discovering a bad checkpoint hours into a run.
2. **Decide the shape of the run out loud before launching.** Only width, height, duration, seed
   and tag are yours to choose — `--steps` must stay `31`, and width/height must be multiples of
   32. Use the runtime cost table below to say how long this specific run will take and how much
   memory it will hold at peak before you start it.
3. **Watch a run instead of waiting blind.** `h3 generate`/`h3 resume` do not currently expose a
   `--preview-every` flag; in default (non-`--json`) mode they only print a `checkpoint: N/M steps`
   line per step, which at least confirms the run is alive. To actually see a frame and judge
   composition/prompt adherence before committing to the full run, use the benchmarking entry point
   instead — it drives the same pipeline and does support previews. **The checkpoint identity is
   the prompt, `--width`, `--height`, `--duration`, `--steps`, `--seed`, `--tag`, plus which
   `--checkpoint` and `--outdir` you point at** — every one of those must match byte-for-byte
   between the preview run and the real one, or `h3 resume` will not recognise it as the same run
   (`checkpoint_not_found`) even though the video/checkpoint were produced under the exact
   geometry/duration you wanted. `run_bench.py`'s `--seed` defaults to `314159`; `h3 generate`'s
   defaults to `0` — they will *not* match unless you pass `--seed` explicitly on both sides:
   ```
   ./.venv/bin/python run_bench.py --checkpoint <ckpt> --outdir <outdir> --prompt "<prompt>" \
       --width <w> --height <h> --duration <d> --seed <seed> --tag <tag> --preview-every 5
   ```
   That writes `<stem>-preview-stepNN.jpg` every N steps (default 5), next to where `<stem>.mp4`
   will land once the run finishes, and writes checkpoints keyed the same way `h3 generate`/
   `h3 resume` key theirs (tag included), so a hand-off between the two only works when every one
   of those identity fields is repeated identically on the `h3 resume`/`h3 generate` call that
   follows. Look at a preview JPEG rather than waiting for the run to finish.
4. **Resume after an interruption; never restart blind.** Every step is checkpointed under
   `<outdir>/checkpoints`. Re-running the exact same `h3 generate` command after a crash or Ctrl-C
   picks the run back up bit-identically, because it auto-resumes whenever the checkpoint's
   identity (prompt + geometry + duration + steps + seed + tag + checkpoint/outdir) matches. Use
   `h3 resume` instead when you want that to be a hard assertion rather than an assumption: it
   fails loudly with `checkpoint_not_found` if nothing matches, instead of silently starting over
   from step 0.

## Runtime cost — decide before you launch

Measured on a MacBook Pro M4 Pro, 48 GB unified memory, the one schedule the baked AdaLN table
supports (`--steps 31`, sigma shifts 12.0/3.0):

| Resolution | Clip length | Per step | Total | Peak RSS |
|---|---|---|---|---|
| 512x512 | 2.4 s | 46 s | 24 min | 11.0 GB |
| 1344x768 (native) | 5 s | 586 s | 299 min (~5 h) | 10.0 GB |
| 1344x768 (native) | 10 s | 1881 s | ~15.7 h (extrapolated; measured for 2 steps, then stopped deliberately) | not measured |

Cost does not scale linearly with resolution or duration: attention is dense and MiniMax has not
released a sparse-attention implementation for H3, so this is an attention-FLOPs bottleneck, not a
memory one. A 10 s clip is not ~2x a 5 s clip — it measured over 3x the per-step cost.

See `reference.md` for the complete flag list of every subcommand and the full `--json` error code
table.
