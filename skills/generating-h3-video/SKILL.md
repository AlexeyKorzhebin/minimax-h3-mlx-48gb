---
name: generating-h3-video
description: Use when generating video or audio with MiniMax H3 on this Apple Silicon Mac, writing or reviewing an H3 prompt, choosing a run's mode/resolution/duration/step count, conditioning on a keyframe, or diagnosing a slow, interrupted, refused-to-start, or schedule/memory-error H3 run
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

## Writing the prompt: the documented format, not ours

The markup below is MiniMax's own and is what the model was trained to parse. Everything this
project wrote before 2026-08-12 used a house format invented here; the last section of this chapter
lists the fourteen ways the two differ, and every one of them is a way to get it wrong again.

Source of truth, both in `docs/upstream-guides/`:
`VIDEO_PROMPT_WRITING_GUIDE_base_en.md` covers t2va / i2v / flf / last-frame, and
`VIDEO_PROMPT_WRITING_GUIDE_ref_en.md` covers full-reference mode, which this fork cannot run.
Read the section of the guide that a question actually belongs to instead of guessing — this
chapter is what to hold in your head while writing, not a replacement for it.
`prompts/greek-official.txt` is a worked ten-second, four-shot example. Write the prompt to a file
under `prompts/` and pass `--prompt-file`: a multi-paragraph prompt does not survive shell quoting,
and two runs are only comparable if the prompt is byte-identical between them.

### Three fields, in this order

```text
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

Field names verbatim, colon included, one blank line between fields. Every shot lives inline inside
the first field as one continuous paragraph — `[Shot 2]` does not start a new line. Nothing else is
a field: no duration header, no `Characters:` block, no trailing notes after `non_diegetic_music`.

For a keyframe run, one fixed instruction comes first, then a blank line, then the three fields:

| Mode | First line, verbatim |
|---|---|
| **t2va** | none — the prompt starts at `integrated_multimodal_description:` |
| **i2v** (`--image`) | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` |
| **flf** (`--image` + `--end-image`) | `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.` |
| **last frame only** | `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.` |

Substitute `N`, the index of the real final shot, and `S.SS`, the effective duration to exactly two
decimals — the *snapped* duration, since `--duration 2.4` yields 73 frames = 3.04 s. Leave every
other word alone, including the em dash. The last row has no flag: `--end-image` without `--image`
is refused, so a last-frame-only run is not reachable from this CLI today.

What the body then has to do differs by mode: **i2v** anchors on the image (style, subjects,
composition, scene) and develops forward — anchor, action onset, continuous development, result;
**flf** prefers a single shot and describes the *path* between the two frames rather than two static
descriptions, landing Picture 2 at the end of the final shot.

### Shots and cuts

- `[Shot 1]` carries no timestamp. It opens with the style words, then the framing, then the
  *initial composition* — where the subjects stand relative to each other and to the frame — and
  only then lets anything move: `[Shot 1] Live-action, cinematic, a wide side-profile shot frames
  two figures facing each other on a rocky ridge… On the left stands… On the right stands… The
  camera holds a static shot as the man slashes…`. Establish before you move; this holds for t2va
  as much as for a keyframe run. Usual styles: `Cinematic`, `live-action`, `2D-animated`, `3D CG`,
  `claymation`, `watercolor`, `vintage film`. With a keyframe, take the style from the image.
- Every later shot is `[Shot N] At MM:SS.mmm, <cut verb> …`. Cut times strictly increase and all
  fall inside the duration the run will actually have.
- Cut verbs: `the camera cuts to`, `the shot cuts to`, `the shot transitions to`, `the shot changes
  to`, `the shot switches to`. Cross-dissolve, fade and wipe only when explicitly asked for.
- **Framing and camera motion are two different slots.** Framing is a noun phrase — the thing the
  cut verb lands on: `cuts to a close action shot as…`, `cuts to a medium tracking shot that
  follows the woman`, `cuts to a low-angle shot of the man planting his feet`. Motion is a verb
  from the table below, one clause per shot, with the beat of the shot hung off it (`as she
  lifts…`, `as the runner exits…`). Do not append a second, thinner camera sentence restating
  framing already given.
- A cut must introduce new information — subject, space, state, viewpoint or time. If only distance
  or a slight angle changes, that is camera motion inside the shot, not a cut.
- There is nowhere to describe a character up front. Introduce a subject where it first enters the
  frame, and in later shots restate only what would otherwise visibly drift where it shows — the
  reference example carries "skin damp and matte" and "hair and braid flying" into the last shot
  and re-lists nothing else.
- Every detail has to correspond to something visible or audible. Production notes that name no
  image — "filmed with crisp motion", "shot on…", "epic battle ambience" — describe nothing the
  model can render, and the format has no slot for them. Drop them rather than rehousing them.

### Camera: motion type, then amplitude, then speed

| Dimension | Expressions |
|---|---|
| Motion type | `Zoom In / Zoom Out` (focal length changes, body still), `Push In / Pull Out` (body moves forward/back), `Pan Left / Pan Right` (lens pivots horizontally), `Truck Left / Truck Right` (body translates horizontally), `Tilt Up / Tilt Down` (lens pivots vertically), `Pedestal Up / Pedestal Down` (body moves vertically), `Arc Shot`, `Tracking Shot`, `Static Shot`, `Shake Slightly / Shake Strongly`, `POV`, `Roll Clockwise / Roll Counterclockwise` |
| Amplitude | `with small amplitude`, `with large amplitude` — omit for medium |
| Speed | `at slow speed`, `at fast speed` — omit for normal |

Conjugate it into the sentence as an action of the camera; never stack labels at the end of a line
and never invent a term (`whip-pan`, `LOW ANGLE fast shot` are not in the vocabulary — a low angle
is framing, and a fast pan is `pans right with large amplitude at fast speed`). Amplitude and speed
are meaning, not decoration: add them where the brief actually calls for a wide or quick move, and
leave them off otherwise rather than qualifying every motion out of habit.

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera holds a static shot as the runner exits the frame.
```

### Speakers and dialogue

An ID goes only to a subject that speaks, sings, or produces an off-screen human voice: `(S1)`,
`(S2)`, and `(S1,S2)` for a line delivered together by two already-numbered speakers. The ID is
stable across shots. **Characters who never vocalise get no ID at all.** At a speaker's first
appearance, establish the voice: type, age, gender, on- or off-screen, pitch, timbre, rate, accent.

Outside `<d>`: who is speaking, the ID, the action, the delivery. Inside `<d>`: the language tag and
the words themselves, verbatim down to the punctuation, never translated or rewritten.

```text
The young woman with a quiet, breathy voice (S1) says: <d>[English] I get off at the next station.</d>
The two children (S1,S2) shout together, <d>[English] Wait for us!</d>
```

Eleven languages are stably supported — Arabic, Chinese, English, French, German, Italian,
Japanese, Korean, Portuguese, Russian, Spanish — and others to a lesser degree. The tag is always
the English name of the language; the line itself is in that language: `<d>[Russian] Сдавайся!</d>`.

- **Voiceover** uses the exact phrase `says in an off-screen voiceover`, and immediately after the
  closing `</d>` the on-screen character's lips must be stated closed:
  `<d>[English] I still remember that road.</d> while his lips remain completely closed.`
- **A line crossing a cut** takes `<scenetrans>` at the connecting point in *both* halves, plus an
  explicit continuity phrase: `continues seamlessly across the cut`, `continues uninterrupted into
  the next shot`, `carries over from the previous shot`, `remains audible across the transition`.
- **A line the end of the clip truncates** takes `<cutoff>`.

### Text visible in the frame

Any banner, sign, label, subtitle or neon text that is actually legible on screen goes in double
quotation marks, verbatim and untranslated: `A red neon sign reading "营业中" glows above the doorway.`

### The two sound fields

`overall_soundscape` — 1–4 English sentences, one paragraph, covering the whole clip: ambience,
physical action sounds, and non-verbal human sounds (wind, footsteps, fabric, impacts, breathing,
panting, laughter, grunting). **Dialogue, singing and diegetic music do not go here** — they are
already in the description, and repeating them doubles them. The line is words: effort sounds stay,
and anything with words in it, a shouted order included, is dialogue and belongs in a `<d>` tag in
the description. `N/A` only when total silence was asked for.

`non_diegetic_music` — 1–3 English sentences on music only the audience hears: instrumentation,
tempo, rhythm, dynamic change. **Abstract mood words are forbidden outright**, as is explaining what
the score does emotionally: not "epic, tense, fast-paced", but "orchestral score at a fast tempo,
opening with low war drums on a steady pulse… ends on a single loud hit". Pinning a change to a
visible beat is not a mood word and is welcome — "joined by brass at the clash", "ending
immediately after the glass breaks". Music the characters can hear — singing, an instrument, a
radio, a phone — is diegetic and belongs in the description instead. `N/A` when there is none.

For scale, `prompts/greek-official.txt` spends 380 words on four shots across ten seconds and
about forty on each sound field. Nearly all of the prompt is the description; the two sound fields
are short by rule.

### The fourteen ways this project got it wrong

Left column is what our own prompts did, and the next agent will do it again unless it reads this.
`prompts/greek-warrior-battle-overcast.txt` is the old format and `prompts/greek-official.txt` the
same scene rewritten; diffing them shows all of it at once.

| # | Don't | Do |
|---|---|---|
| 1 | start with a bare paragraph of description | name the field: `integrated_multimodal_description: [Shot 1] …` |
| 2 | `[10s, multi-shot dynamic action sequence]` as a header | no such line exists; duration is carried by `--duration` and by the cut times |
| 3 | `[0.0-2.5s]`, `[2.5-5.0s]` ranges, one per line | `[Shot 1]` untimed, then `[Shot 2] At 00:02.500, the shot cuts to …`, all inline |
| 4 | style declared in the header | style as the first words inside `[Shot 1]` |
| 5 | a `Characters:` block, or a trailing `Breast physics:` note | nothing outside the three fields; fold each detail into the shot where it is visible |
| 6 | `WIDE SHOT`, `whip-pan`, `LOW ANGLE fast shot` | the twelve motion types plus amplitude and speed, written as a sentence |
| 7 | `(M1)`, `(W1)`, `(C1)` on every character | `(S1)`, `(S2)` on speakers only; silent figures get no ID |
| 8 | speech left as prose, or left out because there was no syntax for it | `<d>[Russian] Сдавайся!</d>`, speaker and delivery outside the tag |
| 9 | a line running across a cut with nothing marking it | `<scenetrans>` in both halves plus a continuity phrase |
| 10 | speech that the clip's end chops off, unmarked | `<cutoff>` |
| 11 | narration described loosely | the exact phrase `says in an off-screen voiceover`, then lips stated closed |
| 12 | on-screen text paraphrased or translated | in double quotes, verbatim |
| 13 | `overall_soundscape: Epic battle ambience — … sharp grunts and battle cries from both` | only what is audible: grunts and hard breathing stay, words do not, and "epic ambience" names no sound at all |
| 14 | `non_diegetic_music: a driving epic … tense and fast-paced` | instruments, tempo, rhythm, dynamics; no mood words |

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

`reference.md` has the complete flag list for every subcommand, the full `--json` error table, and
the prompt-format lookups — which guide section answers which question, the full-reference sections
this fork cannot use, and the checklist to run over a finished prompt before spending an hour on it.
