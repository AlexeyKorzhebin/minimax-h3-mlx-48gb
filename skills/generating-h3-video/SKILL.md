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
| `--json` error code `checkpoint_mismatch` / `checkpoint_corrupt` | A checkpoint file exists but was written for a different request, or cannot be read. | Re-run the same command with `--restart`: it ignores whatever is on disk and starts from step 0, still checkpointing. That is the intended recovery — the file is named after a digest you cannot compute by hand. The error message does name the exact path (`<checkpoint-dir>/h3-<digest>.safetensors`) if you would rather move it aside as evidence; a fresh `--tag` also gets a new file but leaves the stale one behind. |
| Run looks stalled, no output for a long time | Expected — a native step is ~586 s, and plain `h3 generate` (no `--json`) prints only one `checkpoint: N/M steps` line per completed step. | Wait, or check the checkpoint file's mtime under `<outdir>/checkpoints`; see "watch a run" below. |

## Workflow

1. **Validate before spending any compute.** `h3 doctor --checkpoint <dir> --json` checks that all
   four required components (`transformer`, `text_encoder`, `video_vae`, `audio_vae`) and the AdaLN
   cache are present. Seconds, versus discovering a bad checkpoint hours into a run.
2. **Decide the shape of the run out loud before launching.** Only width, height, duration, seed
   and tag are yours to choose — `--steps` must stay `31`, and width/height must be multiples of
   32. Use the runtime cost table below to say how long this specific run will take and how much
   memory it will hold at peak before you start it.
3. **Watch a run instead of waiting blind.** Pass `--preview-every N` to `h3 generate`. It writes
   `<stem>-preview-stepNN.jpg` every N steps, next to where `<stem>.mp4` will land once the run
   finishes, so you can judge composition and prompt adherence long before the run ends. It is off
   by default because one preview costs roughly 49 s at native resolution; `--preview-every 5` at
   1344x768 is a frame roughly every 49 minutes, which is cheap against a five-hour render:
   ```
   h3 generate "<prompt>" --checkpoint <ckpt> --outdir <outdir> \
       --width <w> --height <h> --duration <d> --seed <seed> --tag <tag> --preview-every 5
   ```
   Without it, default (non-`--json`) mode prints only a `checkpoint: N/M steps` line per step,
   which confirms the run is alive but shows you nothing. **The checkpoint identity is the prompt,
   `--width`, `--height`, `--duration`, `--steps`, `--seed`, `--tag`, plus which `--checkpoint` and
   `--outdir` (or `--checkpoint-dir`) you point at** — every one of those must match between a run
   and the `h3 resume` that continues it, or resume will not recognise it as the same run
   (`checkpoint_not_found`). Previewing no longer requires switching to `run_bench.py`, which
   removes the seed trap that used to come with it (`run_bench.py --seed` defaults to `314159`,
   `h3 generate --seed` to `0`).
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

| Resolution | Requested | Per step | Total | Peak RSS |
|---|---|---|---|---|
| 512x512 | 2.4 s (73 frames) | 46 s | 24 min | 11.5 GB |
| 1344x768 (native) | 5 s (124 frames) | 586 s | 299 min (~5 h) | 10.1 GB |
| 1344x768 (native) | 10 s (243 frames) | 1881 s | ~15.7 h (extrapolated; measured for 2 steps, then stopped deliberately) | not measured |

`--duration` is snapped up to the latent grid, so a 2.4 s request yields 73 frames = 3.04 s at
24 fps. Peak RSS covers the diffusion phase only; the ~10-second text-encoding phase that precedes
it peaks at 28.2 GB by MLX's own counter. See `docs/RESULTS.md` in the repository for how each of
those numbers was measured, and which of them were not.

Cost does not scale linearly with resolution or duration: attention is dense and MiniMax has not
released a sparse-attention implementation for H3, so this is an attention-FLOPs bottleneck, not a
memory one. A 10 s clip is not ~2x a 5 s clip — it measured over 3x the per-step cost.

See `reference.md` for the complete flag list of every subcommand and the full `--json` error code
table.
