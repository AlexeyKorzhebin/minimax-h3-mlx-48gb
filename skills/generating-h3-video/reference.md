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
| `--checkpoint-dir` | `<outdir>/checkpoints` | where the *resume* checkpoint lives (`--checkpoint` above is the model weights) |
| `--no-checkpoint` | off | write no resume checkpoint at all; a crash then costs the whole run |
| `--restart` | off | ignore any existing checkpoint and start from step 0 |
| `--preview-every` | `0` | decode `<stem>-preview-stepNN.jpg` every N steps; `0` disables previews |
| `--preview-stem` | the run's output stem | prefix for the preview JPEGs, if they should not land beside the mp4 |
| `--json` | off | emit `{"ok": true, ...report}` or `{"ok": false, "error": {...}}` on stdout instead of human text; also silences progress printing to keep stdout valid JSON |

Resuming is automatic and on by default: if a checkpoint already exists under `--checkpoint-dir`
matching this exact prompt/geometry/duration/steps/seed/tag, `generate` continues it rather than
starting over. **`--restart` is how you override that**, and it is the supported recovery from a
`checkpoint_mismatch` refusal — that error names a file called `h3-{digest}.safetensors` whose
digest you have no way to compute by hand, so deleting it manually is not a real option.

## `h3 resume <prompt>`

Same flags as `generate`, minus `--restart` and `--no-checkpoint`: `resume` exists to *assert* that
a run is being continued, so a flag turning it into a fresh start would defeat its purpose. It
raises `checkpoint_not_found` (see below) if no matching checkpoint exists, instead of silently
starting a new run from step 0. Useful when you want "continue the interrupted run" to be a checked
fact, e.g. from a script.

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

**`doctor --json` does not use the `error` envelope, and this is deliberate.** `h3_48gb/cli.py`'s
module docstring describes failures as `{"ok": false, "error": {"code", "message", "detail"}}`, but
that shape belongs to `CliError` — a *refusal to run*. An incomplete checkpoint is `doctor`'s
finding, not its failure: the command did exactly what it was asked to do. So a failing `doctor
--json` emits its report, with `ok` set to `false`, and exits non-zero:

```json
{"ok": false, "checkpoint": "/path/to/h3-converted", "missing": ["transformer/adaln_cache.safetensors"]}
```

A wrapper should therefore branch on the presence of the `error` key, not on `ok` alone: `error`
present means the CLI refused, `error` absent with `ok: false` means `doctor` ran and the checkpoint
is incomplete, and `missing` lists exactly which components. (`doctor` can still emit the `error`
envelope, but only for an `internal_error`.)

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
| `preview_interval_negative` | `--preview-every` is negative; `0` disables previews, `N > 0` sets a cadence |
| `internal_error` | an unexpected exception reached the CLI boundary; see `detail` for its type |

Without `--json`, a failure prints only its human sentence to stderr and exits non-zero — there is
no code to parse in that mode.

## `run_bench.py` — the benchmarking entry point, not the installed `h3` command

`./.venv/bin/python run_bench.py` drives the same pipeline as `h3 generate` but is a repository
script, not the packaged console command, and exposes flags `generate`/`resume` do not:

| Flag | Default | Notes |
|---|---|---|
| `--preview-every` | `5` | same flag as `h3 generate`, but **on by default here** (`h3 generate` defaults to `0`) |
| `--prompt` | the hummingbird sample | `h3 generate` takes the prompt as a required positional instead |

It shares `--checkpoint`, `--outdir`, `--width`, `--height`, `--duration`, `--steps`, `--seed` and
`--tag` with `h3 generate`/`h3 resume`, and now passes `tag` into the same checkpoint identity
`h3` does — but **its own defaults are not the same ones**: `--checkpoint` defaults to
`~/models/h3-converted` and `--outdir` to `~/models/video-out` (matching `h3`), `--prompt` defaults
to the hummingbird sample prompt (`h3 generate`'s `prompt` is a required positional with no
default), `--width`/`--height` default to `512`/`512` (`h3 generate` defaults to `1344`/`768`),
`--duration` defaults to `2.4` (`h3 generate` defaults to `5.0`), and **`--seed` defaults to
`314159`, not `h3`'s `0`**. Starting a preview with `run_bench.py` and later
handing off to `h3 resume` (or `h3 generate`) only works if you pass every one of prompt,
`--checkpoint`, `--outdir`, `--width`, `--height`, `--duration`, `--steps`, `--seed` and `--tag`
explicitly and identically on both invocations — relying on either side's defaults will silently
give you two different, unrelated checkpoints, not a resumed one (`checkpoint_not_found` from `h3
resume`, or a quiet fresh start from `h3 generate`).

It also always writes a phase-timed JSON report (`<stem>.json`, including `phases` and
`seconds_per_step`) and does not support `--json`-mode machine-readable failures the way `h3`'s
subcommands do — its errors are plain Python tracebacks. Previews, `--restart`, `--checkpoint-dir`
and `--no-checkpoint` are no longer exclusive to it: `h3 generate` has all four. Prefer `h3
generate`/`h3 resume` for a real run; reach for `run_bench.py` specifically when you want the
per-phase timing report, which is the one thing it still has that the CLI does not.

`night_queue.sh` sits one level above `run_bench.py`: it drives a series of runs overnight, lightest
geometry first, with `memwatch.sh` sampling process RSS and machine-wide memory beside each one and
a summary appended to `~/models/logs/h3-night-summary.txt`. Used for the measurements in
`docs/RESULTS.md`, not for producing clips.
