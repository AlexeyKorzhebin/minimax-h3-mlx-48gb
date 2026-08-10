# Reference: `h3` CLI flags and error codes

Deferred out of `SKILL.md` so it is only loaded when a flag's exact name or default is needed.
Source of truth: `h3_48gb/cli.py` (`build_parser`, `ERROR_CODES`) in this repository. Defaults below
were read out of the parser, not transcribed.

## `h3 generate <prompt>`

Starts, or transparently continues, a render.

| Flag | Default | Notes |
|---|---|---|
| `prompt` (positional) | — | required |
| `--width` | `896`, or the keyframe's | `None` until resolved, so "unset" is distinguishable from "asked for exactly 896"; must be a multiple of 32 |
| `--height` | `512`, or the keyframe's | same |
| `--duration` | `5.0` | seconds; snapped up to the latent grid, so 2.4 yields 73 frames = 3.04 s at 24 fps |
| `--steps` | `31` | **grid points**, not forwards: the run does N-1 forward passes. Any N works given a table that covers it (`--adaln-cache`); 31 is what the checkpoint's own table holds |
| `--seed` | `0` | 64-bit values are fine — MLX does not truncate them |
| `--tag` | `run` | part of the output filename and of the checkpoint identity |
| `--checkpoint` | `~/models/h3-converted` | converted weights directory (see `h3 doctor`) |
| `--outdir` | `~/video-out`, or `$H3_OUTDIR` | where `<stem>.mp4`/`.wav`/`.json`/`-raw.npz` and `checkpoints/` land |
| `--checkpoint-dir` | `<outdir>/checkpoints` | where the *resume* checkpoint lives; `--checkpoint` above is the model weights |
| `--image` | none | condition the first frame; the canvas follows it unless **both** `--width` and `--height` are given |
| `--end-image` | none | also condition the last frame; requires `--image` |
| `--adaln-cache` | the checkpoint's own | a table baked by `scripts/bake_adaln.py` for a different step count |
| `--turbo-lora` | none | a Turbo LoRA applied at run time, for few-step runs |
| `--turbo-strength` | `1.0` | measured optimum at the default canvas; lower toward 0.8 if the image over-sharpens |
| `--preview-every` | `5` | `<stem>-preview-stepNN.jpg` every N steps; `0` disables |
| `--preview-stem` | the run's output stem | prefix, when previews should not land beside the mp4 |
| `--preview-decoder` | `tae` | `tae` costs 0.125 s, `vae` is exact and costs 49 s, `latent` is a VAE-free heat map |
| `--json` | off | one JSON document on stdout instead of human text; also silences progress so stdout stays valid JSON |
| `--restart` | off | ignore any existing checkpoint and start from step 0 |
| `--no-checkpoint` | off | write no resume checkpoint at all; a crash then costs the whole run |

Resuming is automatic: a checkpoint under `--checkpoint-dir` whose identity matches is continued
rather than restarted. **The identity is prompt + width + height + duration + steps + seed + tag +
`--checkpoint` + `--outdir`/`--checkpoint-dir` + which adaln table + which LoRA at which strength.**
Change any of them and it is a different run. `--restart` is the supported recovery from
`checkpoint_mismatch`; the file is named after a digest you cannot compute by hand, so deleting it
by hand is not a real option.

## `h3 resume <prompt>`

Same flags as `generate` minus `--restart` and `--no-checkpoint`: `resume` exists to *assert* that a
run is being continued, so a flag turning it into a fresh start would defeat it. Raises
`checkpoint_not_found` when nothing matches, instead of quietly starting from step 0.

## `h3 list`

| Flag | Default |
|---|---|
| `--outdir` | `~/video-out`, or `$H3_OUTDIR` |
| `--json` | off |

One line, or one array entry under `--json`, per finished run's `<stem>.json` under `--outdir`.

## `h3 doctor`

| Flag | Default |
|---|---|
| `--checkpoint` | `~/models/h3-converted` |
| `--json` | off |

Checks that `transformer/`, `text_encoder/`, `video_vae/`, `audio_vae/` and
`transformer/adaln_cache.safetensors` are present. Seconds, against discovering a gap hours in.

**`doctor --json` does not use the `error` envelope, deliberately.** An incomplete checkpoint is
doctor's *finding*, not its failure — the command did what it was asked. So it emits its report with
`ok: false` and exits non-zero:

```json
{"ok": false, "checkpoint": "/path/to/h3-converted", "missing": ["transformer/adaln_cache.safetensors"]}
```

A wrapper should branch on the presence of `error`, not on `ok` alone: `error` present means the CLI
refused; `error` absent with `ok: false` means doctor ran and the checkpoint is incomplete.

## `scripts/bake_adaln.py <steps>`

| Flag | Default | Notes |
|---|---|---|
| `steps` (positional) | — | grid points the table will cover |
| `--out` | — | where to write the table |
| `--lora` / `--strength` | none | fold a LoRA into the baked modulation |
| `--schedule` | `simple` | `simple`, `beta`, or `tail-split` |
| `--tail-split` | `2` | with `tail-split`: how many Euler steps replace the final jump. The prefix stays bit-identical to `simple`'s, which is what makes it a controlled change |

## `--json` error codes

Stable codes from `ERROR_CODES`. Match on `error.code`, never on `error.message` — the sentence can
be reworded at any time, the code cannot.

| Code | Meaning |
|---|---|
| `geometry_not_multiple_of_32` | `--width` or `--height` is not a multiple of 32; the port cannot pack it |
| `schedule_not_baked` | `--steps` does not match the grid size the AdaLN table covers |
| `adaln_cache_unreadable` | `--adaln-cache` exists but is not a readable AdaLN table |
| `checkpoint_not_found` | `resume` was asked for, but no checkpoint matches this run's identity |
| `checkpoint_mismatch` | a checkpoint exists but was written for a different request or model |
| `checkpoint_corrupt` | a checkpoint exists but could not be read |
| `preview_interval_negative` | `--preview-every` is negative; `0` disables, `N > 0` sets a cadence |
| `end_image_without_image` | `--end-image` without `--image`; the end frame is the far anchor of a run that must also have a start frame |
| `image_not_found` | a keyframe path does not exist |
| `image_unreadable` | a keyframe exists but could not be decoded as an image |
| `image_aspect_unsupported` | a keyframe's aspect ratio is outside the 1:4..4:1 the model supports |
| `partial_canvas_with_image` | `--image` with only one of `--width`/`--height` |
| `lora_not_found` | `--turbo-lora` points at a file that does not exist |
| `turbo_strength_invalid` | `--turbo-strength` is not a finite number |
| `upstream_patch_missing` | a keyframe was given but the vendored `upstream/` checkout is unpatched |
| `internal_error` | an unexpected exception reached the CLI boundary; see `detail` for its type |

Without `--json` a failure prints its sentence to stderr and exits non-zero; there is no code to
parse in that mode.

## `run_bench.py` — the benchmarking entry point, not the installed `h3` command

Drives the same pipeline but is a repository script with **different defaults**, which is the whole
trap: `--prompt` defaults to a hummingbird sample, `--width`/`--height` to 512x512, `--duration` to
2.4, and **`--seed` to `314159` rather than `0`**. Starting with `run_bench.py` and handing off to
`h3 resume` only works if every identity field is passed explicitly and identically on both sides;
relying on either side's defaults silently produces two unrelated checkpoints.

What it still has that the CLI does not: a per-phase timing report (`phases`, `seconds_per_step`) in
its `<stem>.json`. Everything else — previews, `--restart`, `--checkpoint-dir`, `--no-checkpoint` —
now exists on `h3 generate`. Prefer the CLI for real runs.

`night_queue.sh` sits above it, driving a series of runs with `memwatch.sh` sampling memory. Both
predate `~/Research/TestVideo/_очередь/`, which is where current queues live.
