# Reference: `h3` CLI flags and error codes

Deferred out of `SKILL.md` so it is only loaded when a flag's exact name or default is needed.
Source of truth: `h3_48gb/cli.py` (`build_parser`, `ERROR_CODES`) in this repository.

## `h3 generate <prompt>`

Starts (or transparently continues, see below) a render.

| Flag | Default | Notes |
|---|---|---|
| `prompt` (positional) | — | required |
| `--width` | `1344` | must be a multiple of 32 |
| `--height` | `768` | must be a multiple of 32 |
| `--duration` | `5.0` | seconds |
| `--steps` | `31` | the only value the baked AdaLN table can serve — do not change it |
| `--seed` | `0` | |
| `--tag` | `run` | becomes part of the output filename and the checkpoint identity |
| `--checkpoint` | `~/models/h3-converted` | path to a converted checkpoint directory |
| `--outdir` | `~/models/video-out` | where `<stem>.mp4`/`.wav`/`.json` and `checkpoints/` land |
| `--json` | off | emit `{"ok": true, ...report}` or `{"ok": false, "error": {...}}` on stdout instead of human text; also silences progress printing to keep stdout valid JSON |

Resuming is automatic and always on: if a checkpoint already exists under `<outdir>/checkpoints`
matching this exact prompt/geometry/duration/steps/seed/tag, `generate` continues it rather than
starting over. There is no flag to force a from-scratch restart short of choosing a new `--tag` or
clearing `<outdir>/checkpoints` yourself.

There is currently no `--preview-every` (or any preview) flag on `generate` — see `run_bench.py`
below if you need one.

## `h3 resume <prompt>`

Same flags as `generate`. Difference: it asserts a matching checkpoint must already exist and
raises `checkpoint_not_found` (see below) if not, instead of silently starting a new run from step
0. Useful when you want "continue the interrupted run" to be a checked fact, e.g. from a script.

## `h3 list`

| Flag | Default |
|---|---|
| `--outdir` | `~/models/video-out` |
| `--json` | off |

Prints one line (or, under `--json`, one array entry) per finished run's `<stem>.json` report
under `--outdir`, oldest tag first.

## `h3 doctor`

| Flag | Default |
|---|---|
| `--checkpoint` | `~/models/h3-converted` |
| `--json` | off |

Checks that `transformer/`, `text_encoder/`, `video_vae/`, `audio_vae/` and
`transformer/adaln_cache.safetensors` are all present under `--checkpoint`. Run this before any
other command — it is the cheapest possible check and catches the checkpoint problems that would
otherwise surface only after minutes-to-hours of compute.

## `--json` error codes

Every failure this CLI can raise under `--json` carries one of these stable codes (from
`ERROR_CODES` in `h3_48gb/cli.py`) — match on `error.code`, never on `error.message`, since the
sentence can be reworded at any time:

| Code | Meaning |
|---|---|
| `geometry_not_multiple_of_32` | `--width` or `--height` is not a multiple of 32; the port cannot pack it |
| `schedule_not_baked` | `--steps` does not equal the one grid size the baked AdaLN table covers (31) |
| `checkpoint_not_found` | `resume` was asked for, but no checkpoint matches this run's identity |
| `checkpoint_mismatch` | a checkpoint exists but was written for a different request or model |
| `checkpoint_corrupt` | a checkpoint exists but could not be read |
| `internal_error` | an unexpected exception reached the CLI boundary; see `detail` for its type |

Without `--json`, a failure prints only its human sentence to stderr and exits non-zero — there is
no code to parse in that mode.

## `run_bench.py` — the benchmarking entry point, not the installed `h3` command

`./.venv/bin/python run_bench.py` drives the same pipeline as `h3 generate` but is a repository
script, not the packaged console command, and exposes flags `generate`/`resume` do not:

| Flag | Default | Notes |
|---|---|---|
| `--preview-every` | `5` | decode `<stem>-preview-stepNN.jpg` every N steps; `0` disables it |
| `--checkpoint-dir` | `<outdir>/checkpoints` | override where the resume checkpoint lives |
| `--no-checkpoint` | off | disable checkpointing entirely — a crash then costs the whole run |
| `--restart` | off | ignore any existing checkpoint and start over from step 0 |

It also always writes a phase-timed JSON report (`<stem>.json`, including `phases` and
`seconds_per_step`) and does not support `--json`-mode machine-readable failures the way `h3`'s
subcommands do — its errors are plain Python tracebacks. Prefer `h3 generate`/`h3 resume` for a
real run; reach for `run_bench.py` specifically when you want in-flight previews or per-phase
timing.
