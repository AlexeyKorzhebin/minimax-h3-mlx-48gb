# Reference: prompt-format lookups, `h3` CLI flags and error codes

Deferred out of `SKILL.md` so it is only loaded when a flag's exact name or default is needed, or
when a prompt-format question goes past what fits in the skill. Two sources of truth, and neither
is this file: `docs/upstream-guides/` for the prompt format, and `h3_48gb/cli.py` (`build_parser`,
`ERROR_CODES`) for everything below the format section. Defaults were read out of the parser, not
transcribed.

## Which guide section answers which prompt question

`VIDEO_PROMPT_WRITING_GUIDE_base_en.md` is the one to open for an ordinary run. Its four worked
cases at the end — one per mode — are usually faster to copy the shape of than the rules are to
re-read.

| Question | Section |
|---|---|
| what each mode's body has to accomplish | 1, and 3.1–3.3 per mode |
| the verbatim first-line instruction for a keyframe run | 2.1 |
| what the three fields mean | 2.2 |
| style words, and where the style goes | 4.1 |
| timestamps, cut verbs, when a cut is justified at all | 4.2 |
| the twelve camera motions, amplitude, speed | 4.3 (table) |
| speaker IDs, group speech, voiceover, `<scenetrans>`, `<cutoff>` | 4.4 |
| text visible in frame | 4.5 |
| what may and may not appear in the two sound fields | 4.6, 4.7 |
| a complete t2va / i2v / flf / last-frame prompt | 5, cases 1–4 |

## Full-reference mode is documented but not runnable here

`VIDEO_PROMPT_WRITING_GUIDE_ref_en.md` describes a **different output format** for `ref2va` —
conditioning on reference assets rather than on frames. That partition is not converted by this
fork and no flag reaches it, so a prompt written in this format will be fed to the wrong parser.
Read it to understand a MiniMax example that arrives in this shape; do not write one.

Its six sections, in order, are `subject_definitions`, `summary`, `retention_analysis`,
`detailed_description`, `overall_soundscape`, `non_diegetic_music`. The main body is
`detailed_description`, not `integrated_multimodal_description`; the style opener sits on its own
line *before* `[Shot 1]` rather than inside it; and reference labels `<Subject N>`, `<Picture N>`,
`<Video N>`, `<Audio N>` are threaded through every section. Shots, camera, speakers, dialogue and
the two sound fields are shared with the base guide unchanged.

## Speech: the eleven stable languages

Arabic, Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish.
Others are supported to varying degrees. The tag inside `<d>` is always the English name of the
language, whatever language the line is in.

## Checklist for a finished prompt

Cheap to run, and each line is an hour of GPU time if it is wrong.

- [ ] `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` present, in
      that order, one blank line apart.
- [ ] With `--image` or `--end-image`: the matching instruction is the first line, verbatim, followed
      by a blank line, with `N` and `S.SS` substituted and `S.SS` at exactly two decimals.
- [ ] `[Shot 1]` has no timestamp and opens with the style words.
- [ ] Later shots read `[Shot N] At MM:SS.mmm, …`, numbered consecutively, times strictly
      increasing, the last one inside the run's duration.
- [ ] No header line, no `Characters:` block, nothing trailing the third field.
- [ ] Camera movement uses the vocabulary from guide 4.3, inside a sentence.
- [ ] `<d>` and `</d>` are balanced; every one carries a language tag from the eleven; only the
      spoken words are inside.
- [ ] `(S…)` appears only on subjects that vocalise, and the same subject keeps the same number.
- [ ] Every voiceover is followed by the closed-lips statement.
- [ ] `overall_soundscape` is 1–4 sentences and repeats nothing that is spoken or sung.
- [ ] `non_diegetic_music` is 1–3 sentences, no mood words.

## `h3 generate <prompt>`

Starts, or transparently continues, a render.

| Flag | Default | Notes |
|---|---|---|
| `prompt` (positional) | — | pass this **or** `--prompt-file`, not both |
| `--prompt-file` | none | read the prompt from a file instead of the positional argument; strips every trailing newline, exactly as `$(cat file)` does |
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
| `--mode` | none | one of `t2v`/`t2va`/`i2v`/`flf`; asserts the mode `--image`/`--end-image` already imply and refuses with `mode_mismatch` if they disagree. Sets nothing — it exists so a typo in a filename or a flag is caught in the first second, not an hour later in the finished clip |
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

## `h3 status`

| Flag | Default |
|---|---|
| `--outdir` | `~/video-out`, or `$H3_OUTDIR` |
| `--json` | off |

Scans `--outdir` recursively for resume checkpoints and reports every run found, in-flight ones
first (then `unknown`, then `stale`). Refuses with `outdir_not_found` if `--outdir` does not exist
— a checked failure, unlike `watch`, which has nowhere to report one to.

Each run's state is one of:

| State | Meaning |
|---|---|
| `in_flight` | a checkpoint was written recently enough to still be running; reports a rate and an ETA |
| `unknown` | the checkpoint predates `started_at` (older code wrote it), so age and rate cannot be computed — a reason to keep watching, not a verdict either way |
| `stale` | old enough that nothing is plausibly still writing it |
| `unreadable` | the checkpoint file exists but could not be parsed; reported with its error, not dropped |
| `finished` | the run has a completed `<stem>.json` report, same as `h3 list` |

Without `--json`, this is the human-readable render `format_status` produces: one block per
in-flight run with progress and ETA, one line per `unknown`/`stale`/`unreadable` run, or
`ничего не идёт в <outdir>` when nothing was found.

## `h3 watch`

| Flag | Default | Notes |
|---|---|---|
| `--outdir` | `~/video-out`, or `$H3_OUTDIR` | |
| `--interval` | `20.0` | seconds between redraws; `0` renders once and exits |

Redraws `h3 status`'s human report in place (a leading escape clears the previous block, so both a
plain terminal and a redirected log stay readable) until no run is `in_flight` or `unknown` — i.e.
until every run is `stale`, `unreadable`, or gone. No `--json`: it exists to watch a queue on a
terminal, not to be scripted against; script against `h3 status --json` in a loop instead.

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
| `outdir_not_found` | `--outdir` does not exist or cannot be read (`h3 status`) |
| `prompt_file_not_found` | `--prompt-file` points at a file that does not exist |
| `prompt_file_unreadable` | `--prompt-file` exists but could not be read as UTF-8 |
| `prompt_file_empty` | `--prompt-file` is empty once trailing newlines are stripped |
| `prompt_both_given` | both a positional prompt and `--prompt-file` were given; pass exactly one |
| `prompt_missing` | no prompt: pass one positionally or with `--prompt-file` |
| `mode_mismatch` | `--mode` contradicts the `--image`/`--end-image` flags given |
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
