"""Run one song end to end: Music3 generation -> ffmpeg mastering -> mp3 -> Whisper sung-lyrics
check. This is the `kind="song"` job body (design spec, "2. Трек") -- task 3 wires `run_song` into
the queue/worker the same way `h3_48gb.worker` already wires `h3 generate` in, under the same GPU
lock ("эксклюзив: H3 и Music3 не сосуществуют"). This module does not take that lock itself, and
never will: like `h3_48gb.worker`, it has no opinion about scheduling, only about what one song job
does once it is already running.

**No `mlx`/`mlx_audio` import, ever** -- same discipline as `h3_48gb.worker`'s "no mlx" rule, for
the same reason (this module is imported by the always-resident web server, not just the worker;
a resident audio model stack there costs gigabytes for nothing). Music3 itself lives in a
*separate* virtualenv from this repo's own (`~/models/music3/.venv` -- "music3-venv"), because
`mlx_audio`'s music stack is a separate install from `h3_48gb`; every fact this module needs from
that stack -- the generated audio, the peak memory, the Whisper transcript -- crosses the process
boundary as a subprocess's stdout or an output file it wrote, never as an import.

**Pipeline**, per song: (1) `scripts/music3-generate.py`, run under `music3_python`, writes
`song.wav` (see `_generate_wav` for the "Music3 stops early once the lyrics run out" duration
subtlety -- `--duration` is an upper bound, not a promise). (2) `master` runs the mastering ffmpeg
chain (highpass + presence/air EQ + loudness normalization -- `MASTER_FILTER`) into
`song.mastered.wav`. (3) `_transcribe` runs `mlx_whisper` (also under `music3_python` -- it is
installed in the same venv) on the mastered wav, and `check_lyrics_sung` compares the transcript
against the lyrics line by line to find each section's start timestamp and flag an undersung take.
(4) Only once that has actually succeeded does `to_mp3` encode *both* wavs to mp3 (`libmp3lame
-q:a 1`) -- the raw take and the mastered one are both kept, matching `project.py`'s
`track.mp3`/`track.mastered_mp3` fields. Encoding last, after validation, is deliberate (I4,
2026-08-18 review): `song.mp3`/`song.mastered.mp3` are the product a `kind="song"` track actually
ships, so their presence on disk should mean the *whole* job succeeded, not just that ffmpeg ran --
see `run_song`'s own docstring.

**`align_track`** (task 3, design spec addendum "импортированный трек") is the other, shorter
pipeline this module offers: for a `track.source == "import"` project, an mp3 already exists (a
human uploaded it, e.g. a Suno export) and neither generation nor mastering apply -- only step (3)
runs, against the file as given. See `align_track`'s own docstring.

**Calibration** (2026-08-18 "Колыбельная" run, a real slow song, referenced throughout this
module's docstrings): ~28 s of singing per section on a slow track (`SECONDS_PER_SECTION`);
Music3 routinely finishes *before* `--duration` once the lyrics are exhausted (that run's audio
was 268 s against a 295 s duration cap -- not a bug, not undersung, just the model stopping when
it runs out of words to sing); an undersung take instead looks like some sections sung and the
rest of the track filled with wordless/instrumental audio -- diagnosable from the *last third* of
the lyrics' lines going unmatched (see `check_lyrics_sung`), not from the audio being shorter than
requested.

Messages a human reads are Russian; comments and docstrings are English -- same split as the rest
of the package (see `h3_48gb.queue`'s module docstring).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: `scripts/music3-generate.py`, resolved relative to this file rather than the process's cwd --
#: `run_song` may be called by a worker started from anywhere.
MUSIC3_GENERATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "music3-generate.py"

#: `music3_python`'s default -- the interpreter inside `~/models/music3/.venv` ("music3-venv"),
#: the one virtualenv on this machine with `mlx_audio.music`/`mlx_whisper` installed. A caller
#: testing against a different install overrides this; nothing here hardcodes the path beyond a
#: default.
DEFAULT_MUSIC3_PYTHON = Path.home() / "models/music3/.venv/bin/python"

#: `model_dir`'s default -- the 8-bit Music3 weights `~/models/music3/fetch-parallel.sh` downloads
#: (BACKLOG.md, "Песни в mp3"), 14.18 GB, quantized for a 48 GB machine.
DEFAULT_MUSIC3_MODEL_DIR = Path.home() / "models/music3/mlx-8bit"

#: `mlx_whisper --model` -- the repo id whose weights the Whisper section-matching gate uses
#: (large-v3-turbo: the size that actually gets Russian sung lyrics right enough to fuzzy-match,
#: per the "Колыбельная" experiment; a smaller model's transcript is too degraded to trust even
#: with the fuzzy matching `check_lyrics_sung` already does).
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

#: `28 с/секция` (design spec, "Трек": "факт: ~28 с/секция для медленных, suno-валидация") --
#: measured on the "Колыбельная" run, a slow song. `estimate_duration` uses this as a flat
#: per-section rate rather than trying to tell a slow song from a fast one up front; the design
#: spec calls this an estimate "с запасом" (with margin), not a precise prediction, and the
#: Whisper gate (`check_lyrics_sung`'s `undersung`) is what catches an estimate that ran short.
SECONDS_PER_SECTION = 28.0

#: Flat pad added on top of `SECONDS_PER_SECTION * <section count>` -- covers a wordless intro/
#: outro instrumental tail (a section like `[outro]` that is mostly music, little or no lyric) that
#: the per-section rate, tuned on sung sections, would otherwise underestimate.
DURATION_PAD_SECONDS = 15.0

#: The mastering chain (design spec, "Трек": "ffmpeg: highpass, presence/air EQ, доводка до
#: -13.6 LUFS"), reproduced exactly as the task brief specifies it, values and all -- the *full*
#: seven-entry `firequalizer` curve confirmed by the chain's owner (2026-08-18 review, C2: the
#: five-entry version this module originally shipped with was a plan-brief transcription
#: shortcut, not the actual proven chain): `highpass=38` cuts sub-bass rumble below the vocal's
#: usable range; the `firequalizer` entries carve a dip at 60/120 Hz (-2.5/-1.5 dB, keeps the low
#: end from masking the vocal) and lift presence/air at 3/5/8/12/16 kHz (+1.5/+2/+3.5/+4/+3 dB,
#: the band Music3's raw output tends to leave dull); `loudnorm` brings the result to -13.6 LUFS
#: integrated, -1 dBTP true-peak ceiling, 11 LU loudness range -- the same target the three tracks
#: this chain was proven on (task brief) were mastered to. `loudnorm` here runs in its default
#: single-pass (dynamic) mode, not two-pass -- deliberate, not an oversight: two-pass needs a
#: first analysis pass over the *whole* file before encoding, which single-pass avoids, at the
#: cost of the normalizer adapting on the fly and audibly lifting quiet tails (an instrumental
#: fade, a wordless outro) more than a two-pass run would. The single quotes around `entry(...)`
#: are ffmpeg's own filtergraph escaping (protects the commas inside each `entry()` from being
#: read as filter-option separators) -- **not** shell quoting; this string is passed as one
#: `subprocess` argv element, never through a shell, so the quotes must stay exactly as ffmpeg's
#: own parser expects them. `MASTER_FILTER`'s exact string is pinned by a regression test in
#: `test_songrun.py` -- this value must not silently drift again.
MASTER_FILTER = (
    "highpass=f=38,"
    "firequalizer=gain_entry='entry(60,-2.5);entry(120,-1.5);entry(3000,1.5);entry(5000,2);"
    "entry(8000,3.5);entry(12000,4);entry(16000,3)',"
    "loudnorm=I=-13.6:TP=-1:LRA=11"
)

#: A `[tag]` line -- the suno-style section markers the design spec's lyrics already use (and any
#: user-pasted suno lyrics get converted to, per "Сценарий": "конвертация suno-разметки при
#: импорте"). Matches the whole stripped line, so a line that merely *contains* brackets somewhere
#: (unlikely in a lyric, but not this module's business to assume) is not mistaken for a tag.
_SECTION_TAG_RE = re.compile(r"^\[([^\]]+)\]$")

#: Punctuation stripped before word-overlap matching -- anything that is not a "word" character
#: (Unicode-aware, so Cyrillic letters survive) or whitespace becomes a space. See `_normalize`.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: How much of one lyric line's words must reappear in a Whisper segment's words for the two to
#: count as "the same line", 0.5 (50%). Deliberately at the *low* end of the task brief's own
#: "50-60%" range, pinned there by a real Whisper mishearing from the "Колыбельная" run: the line
#: "Имя древнее в висках" (4 words) came back from Whisper as "Имя древнее песка" (3 words, "в
#: висках" collapsed to a mis-heard "песка") -- exactly 2 of the line's 4 words survive, a 50%
#: overlap. Pushing this threshold above 0.5 would misclassify that real, correctly-sung line as
#: unsung; see `test_songrun.py`'s regression test built directly from this example.
LINE_MATCH_THRESHOLD = 0.5

#: The *fraction* `LINE_MATCH_THRESHOLD` alone is not enough of a floor for very short lines -- a
#: 2-word line needs only *one* shared word to clear 50%, and a 1-word line clears it with any
#: word at all. The real "Колыбельная" run's own short lines (the outro's "Спи, мой князь.",
#: three words, two of which -- "спи", "мой" -- recur all over the song) are exactly this shape:
#: cheap to spuriously match. `_line_matches_segment` requires *both* the 50% fraction *and* this
#: absolute count of shared words, so a thin coincidence on a short line can no longer clear the
#: bar on fraction alone. The real mishearing regression ("в висках" -> "песка") shares exactly 2
#: words and survives this floor unchanged -- see `test_songrun.py`.
MIN_SHARED_WORDS = 2

#: The undersung gate (design spec, "Трек": "недопетый хвост ловится Whisper-проверкой"; task
#: brief: "порог: <60% строк последней трети найдены"). Below this fraction of the *last third* of
#: the lyrics' lines being matched to a Whisper segment, the take is flagged `undersung=True`.
UNDERSUNG_LINE_THRESHOLD = 0.6


class SongRunError(RuntimeError):
    """Raised when a subprocess this module drives (Music3 generation, ffmpeg, `mlx_whisper`)
    exits non-zero, or produces output this module cannot make sense of (no `[audio] ... dur=`
    line, no Whisper JSON file). Never lets a bare `CalledProcessError`/`OSError`/`KeyError`
    escape, so a caller (task 3's worker glue) can catch one exception type for "this song job
    failed" the same way `h3_48gb.queue`/`h3_48gb.project` give their own callers one exception
    family each.
    """


@dataclass
class SongResult:
    """Everything one `run_song` call produced, in the shape task 3 needs to hand straight to
    `project.Project.update_track` (`wav`/`mp3`/`mastered_mp3`/`sections`/`status` are literally
    that method's own field names) and task 6 needs to render the track gate and cut clip scenes
    to `sections`' boundaries.
    """

    #: Music3's raw output -- the file `scripts/music3-generate.py` wrote.
    wav: Path
    #: `wav` run through `MASTER_FILTER`.
    mastered_wav: Path
    #: `wav` encoded to mp3 (`libmp3lame -q:a 1`).
    mp3: Path
    #: `mastered_wav` encoded to mp3 -- the product a `kind="song"` project actually ships (design
    #: spec: "mp3 -- продукт").
    mastered_mp3: Path
    #: How long Music3 actually sang, in seconds -- parsed from its own `dur=` report, **not**
    #: `duration` the caller asked for (see `_generate_wav`'s docstring: Music3 stops early once
    #: the lyrics run out, so the two routinely differ without anything being wrong).
    duration: float
    #: The full Whisper transcript, concatenated, for a human to read on the track gate page next
    #: to the lyrics.
    transcript: str
    #: One entry per section *occurrence* in the lyrics, in order (a lyric with two `[chorus]`
    #: tags gets two separate section entries), `{"name": str, "start": float | None,
    #: "end": float | None}` -- see `check_lyrics_sung`. **`start`/`end` can both be `None`** -- a
    #: section no line of which matched anything in the Whisper transcript (never sung, or sung
    #: too poorly to match) has no timestamp to anchor either boundary to; task 6 (clip scenes cut
    #: to these boundaries) must skip such a section rather than assume every entry has real
    #: numbers. Even when both are numbers, **`end - start` is not that section's singing
    #: duration** -- `end` is simply the *next* section's own known start (or the track's total
    #: `duration` for a trailing section), so it silently swallows any unsung section(s) in
    #: between (their own `start`/`end` are `None`, per `_section_ends`) into the *previous* sung
    #: section's span. A clip-scene cutter that needs to know "was this whole span actually sung"
    #: cannot get that from `end - start` alone.
    sections: list[dict] = field(default_factory=list)
    #: `True` if the last third of the lyrics' lines look unsung (design spec: "трек короче
    #: лирики, увеличь длительность"). `False` does not mean every line was matched, only that the
    #: *tail* was -- see `check_lyrics_sung`'s own docstring for exactly what this does and does
    #: not promise.
    undersung: bool = False
    #: Task 1 ("Сюжет клипа" wave, "авто-лирика"): `align_track`'s raw Whisper segments,
    #: `[{"start": float | None, "end": float | None, "text": str}, ...]`, one entry per segment
    #: `mlx_whisper` produced -- set **only** when `align_track` was called with `lyrics=None` (no
    #: reference lyrics to match against at all). `None` in every other case (`run_song`'s own
    #: generated take always has real lyrics; `align_track` given real lyrics builds `sections`
    #: instead, the same as `run_song` does) -- a caller (the worker) tells "auto-transcribed" and
    #: "matched against known lyrics" apart by checking this field for `None`, not by re-deriving it
    #: from whether `track.lyrics` happens to be empty. Task 2 (LLM scenario generation) is the
    #: intended reader: it needs each segment's own timestamp to group them into sections itself,
    #: something `sections` (built only when reference lyrics exist) cannot offer here.
    raw_segments: list[dict] | None = None


def _run_or_raise(cmd: list[str], run, what: str) -> subprocess.CompletedProcess:
    """`run(cmd, capture_output=True, text=True)`, raising `SongRunError` (never a bare
    `OSError`/`FileNotFoundError`) if the subprocess could not even be started (e.g. `music3_python`
    or `mlx_whisper`'s binary does not exist at the resolved path -- I6, 2026-08-18 review: `run`
    itself can raise before ever producing a `CompletedProcess` to check a return code on). Does
    **not** check the exit code -- callers that need to do something with the output (write a log)
    before deciding whether a nonzero exit is fatal call this directly instead of `_run` (see
    `_generate_wav`).
    """
    try:
        return run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise SongRunError(f"{what} failed to start: {exc}") from exc


def _run(cmd: list[str], run, what: str) -> subprocess.CompletedProcess:
    """`_run_or_raise`, plus raising `SongRunError` (never a bare `CalledProcessError`) if the
    process exits non-zero. `what` names the step in the raised message (e.g.
    `"music3-generate.py"`) so a failure log reads as "which of the four pipeline steps broke",
    not just an exit code.
    """
    result = _run_or_raise(cmd, run, what)
    if result.returncode != 0:
        raise SongRunError(f"{what} exited {result.returncode}: {(result.stderr or '').strip()}")
    return result


_DURATION_RE = re.compile(r"dur=([\d.]+)s")


def _generate_wav(music3_python, model_dir, caption: str, lyrics: str, duration: float,
                   steps: int, seed: int, track_dir: Path, *, run) -> tuple[Path, float]:
    """Run `scripts/music3-generate.py` under `music3_python`, writing `caption.txt`/`lyrics.txt`/
    `gen.log` into `track_dir` alongside the output `song.wav`. Returns `(wav_path,
    actual_duration)`.

    `actual_duration` is parsed from the child's own final `[audio] ... dur=<seconds>s` line
    (`scripts/music3-generate.py` computes it from the generated audio's actual sample count), not
    assumed to equal the `duration` argument: Music3 generates until it either exhausts the lyrics
    or hits the `--duration` cap, whichever comes first (the 2026-08-18 "Колыбельная" run asked
    for 295 s and got 268 s of audio back -- the lyrics simply ran out first). Raises
    `SongRunError` if that line is missing from stdout -- a change to the script's output format,
    or a crash that exited 0 some other way, must not silently hand back a fabricated duration.

    `gen.log` is written *before* this function looks at the exit code (I3, 2026-08-18 review) --
    Music3's own stdout (the `[timing]`/`[audio] ... dur=` lines, and the peak-memory report) is
    the only diagnostic this module has for a failed generation; raising on a nonzero exit before
    that output ever reaches disk would throw away the one piece of evidence a human debugging the
    failure actually needs.
    """
    caption_path = track_dir / "caption.txt"
    lyrics_path = track_dir / "lyrics.txt"
    caption_path.write_text(caption, encoding="utf-8")
    lyrics_path.write_text(lyrics, encoding="utf-8")
    wav_path = track_dir / "song.wav"

    cmd = [str(music3_python), str(MUSIC3_GENERATE_SCRIPT),
           "--model", str(model_dir),
           "--caption-file", str(caption_path),
           "--lyrics-file", str(lyrics_path),
           "--duration", str(duration),
           "--steps", str(steps),
           "--seed", str(seed),
           "--output", str(wav_path)]
    result = _run_or_raise(cmd, run, "music3-generate.py")
    (track_dir / "gen.log").write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    if result.returncode != 0:
        raise SongRunError(
            f"music3-generate.py exited {result.returncode}: {(result.stderr or '').strip()}")

    match = _DURATION_RE.search(result.stdout or "")
    if match is None:
        raise SongRunError(
            "music3-generate.py produced no '[audio] ... dur=<seconds>s' line to read the "
            "actual generated duration from")
    return wav_path, float(match.group(1))


def master(wav_path, mastered_path, *, run=subprocess.run) -> Path:
    """`wav_path` run through `MASTER_FILTER` into `mastered_path` (ffmpeg `-af`, one filter
    chain -- highpass, then the presence/air EQ, then loudness normalization, in that order,
    matching the mastering step the task brief and three prior tracks already proved). Returns
    `mastered_path`. The one step of this module's pipeline exercised against a *real* `ffmpeg`
    in tests (`tests/test_songrun.py`'s synthetic-sine test) -- everything else here is a
    subprocess this module cannot safely run for real in a test (Music3 needs the GPU; `mlx_whisper`
    needs the same venv and downloaded weights) and is mocked instead.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-af", MASTER_FILTER,
           str(mastered_path)]
    _run(cmd, run, "ffmpeg mastering")
    return Path(mastered_path)


_FFPROBE_DURATION_CMD = ("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0")


def _ffprobe_duration(mp3_path, *, run) -> float:
    """`ffprobe -v error -show_entries format=duration -of csv=p=0 <mp3_path>`, through the same
    injectable `run` seam every other subprocess call in this module goes through. Fix round 1, I4
    (2026-08-18 review): `align_track` used to take its `duration` from the *last Whisper segment's
    own end timestamp* -- cheap, but wrong for a real imported mp3 with any audible tail after the
    last recognized word (a fade, an instrumental outro, applause): that tail would be silently cut
    from `duration` and therefore from the last section's `end` (`check_lyrics_sung`'s
    `_section_ends`), understating both. `ffprobe`'s own container-level duration has no such blind
    spot -- it reads the file's actual length, not a guess derived from what Whisper happened to
    transcribe.

    Raises `SongRunError` (never a bare `ValueError`) if `ffprobe` fails or its stdout is not a
    parseable number -- matching every other subprocess call in this module.
    """
    result = _run([*_FFPROBE_DURATION_CMD, str(mp3_path)], run, "ffprobe duration")
    raw = (result.stdout or "").strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise SongRunError(f"ffprobe produced an unparseable duration for {mp3_path}: {raw!r}") \
            from exc


def to_mp3(src_wav, dst_mp3, *, run=subprocess.run) -> Path:
    """`src_wav` encoded to `dst_mp3` (`libmp3lame -q:a 1` -- the task brief's exact codec/quality
    choice). Called twice per song (design spec, "Трек": "оба файла сохраняются") -- once for the
    raw `wav`, once for `mastered_wav` -- so `run_song` keeps both the unmastered and mastered mp3.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src_wav),
           "-codec:a", "libmp3lame", "-q:a", "1", str(dst_mp3)]
    _run(cmd, run, "ffmpeg mp3 encode")
    return Path(dst_mp3)


def _mlx_whisper_binary(music3_python) -> Path:
    """The `mlx_whisper` console-script entry point living next to `music3_python` in the same
    venv's `bin/` directory. `mlx_whisper` ships no `__main__.py` (`<venv>/bin/python -m
    mlx_whisper` fails with "mlx_whisper is a package and cannot be directly executed" -- verified
    against the real music3-venv install, 2026-08-18), so this module reaches it through the
    installed console script instead of `-m`, resolved relative to the interpreter path
    `run_song`'s caller already supplied for generation rather than adding a second path a caller
    would have to configure separately -- one venv, one thing to point at.

    Deliberately NOT `.resolve()`d: a venv's `bin/python` is a symlink chain out to the base
    interpreter (`bin/python -> python3 -> ~/.pyenv/versions/.../bin/python3`), so resolving the
    *file* walks out of the venv entirely and lands in a `bin/` that has no `mlx_whisper` --
    exactly the failure the 2026-08-19 integration gate hit on the first live song job. The
    console script lives next to the symlink, not next to its target.
    """
    return Path(music3_python).with_name("mlx_whisper")


def _transcribe(music3_python, audio_path: Path, track_dir: Path, *, run) -> dict:
    """Run `mlx_whisper` (large-v3-turbo, `language=ru`) on `audio_path`, writing
    `<track_dir>/whisper.json` (the CLI's own `--output-format json`) and returning it parsed --
    `{"text": <full transcript>, "segments": [{"start": ..., "end": ..., "text": ...}, ...], ...}`.
    Raises `SongRunError` if the process fails or the JSON file it was supposed to write is
    missing/unparseable, never a bare `OSError`/`json.JSONDecodeError`.
    """
    out_name = "whisper"
    cmd = [str(_mlx_whisper_binary(music3_python)),
           "--model", WHISPER_MODEL, "--language", "ru",
           "--output-format", "json", "--output-dir", str(track_dir),
           "--output-name", out_name, str(audio_path)]
    _run(cmd, run, "mlx_whisper")

    json_path = track_dir / f"{out_name}.json"
    try:
        raw = json_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SongRunError(f"mlx_whisper did not write {json_path}: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SongRunError(f"{json_path} is not valid JSON: {exc}") from exc


def parse_lyrics(lyrics: str) -> tuple[list[str], list[tuple[int, str]]]:
    """`lyrics` split into `(section_names, lines)`: `section_names` is the ordered list of
    `[tag]` names, one entry per *occurrence* (a lyric with `[chorus]` twice gets two entries,
    both named `"chorus"`); `lines` is every non-empty, non-tag line, as `(section_index, text)`,
    in order. Blank lines (the stanza breaks between tags) are dropped -- they carry no words to
    match against Whisper.

    Lyric text appearing before the first `[tag]` is folded into an implicit section `""` at
    index 0, rather than dropped, so a hand-typed lyric that forgot its opening tag still gets
    every line counted (`count_sections`/`estimate_duration`) and matched
    (`check_lyrics_sung`) -- just under an unnamed section instead of a real one.

    **Public since fix round 1 (I's own minor, 2026-08-19 review, reviewer's verdict on task 6):**
    was `_parse_lyrics` -- `h3_48gb.web.build_clip_scenes` already called it directly from outside
    this module (the same positional convention that builds `track.sections`, not a parser worth
    duplicating), which made it a de facto public function with a private name. `_parse_lyrics`
    stays as a plain alias below for anything that still spells it the old way.
    """
    section_names: list[str] = []
    lines: list[tuple[int, str]] = []
    current = -1
    for raw in lyrics.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        tag = _SECTION_TAG_RE.match(stripped)
        if tag:
            section_names.append(tag.group(1).strip().lower())
            current = len(section_names) - 1
            continue
        if current == -1:
            section_names.append("")
            current = 0
        lines.append((current, stripped))
    return section_names, lines


#: Fix round 1 (2026-08-19 review): compatibility alias for `parse_lyrics`'s old private name --
#: see that function's own docstring.
_parse_lyrics = parse_lyrics


def count_sections(lyrics: str) -> int:
    """The number of section *occurrences* in `lyrics` -- every `[tag]` line, repeats included
    (`[chorus]` appearing three times counts as three) -- what `estimate_duration` multiplies
    `SECONDS_PER_SECTION` by, and what `check_lyrics_sung`'s `sections` list has one entry per.
    """
    section_names, _ = parse_lyrics(lyrics)
    return len(section_names)


def estimate_duration(lyrics: str) -> float:
    """`SECONDS_PER_SECTION * count_sections(lyrics) + DURATION_PAD_SECONDS` -- the task brief's
    formula exactly. An estimate *with margin* (design spec: "с запасом"), not a prediction of
    Music3's actual runtime: Music3 itself may finish earlier (see `_generate_wav`'s docstring),
    and the Whisper gate (`check_lyrics_sung`'s `undersung`) is what catches the estimate coming
    in short, not this function being made more precise.
    """
    return SECONDS_PER_SECTION * count_sections(lyrics) + DURATION_PAD_SECONDS


def _normalize(text: str) -> str:
    """`text`, lowercased, `ё`->`е` folded (Whisper and hand-typed lyrics disagree on which one
    they use for the same word), and punctuation replaced with spaces -- the shared normalization
    both a lyric line and a Whisper segment go through before `_word_set` splits them into words,
    per the task brief ("нормализация: lowercase, без пунктуации/ё->е").
    """
    return _PUNCT_RE.sub(" ", text.lower().replace("ё", "е"))


def _word_set(text: str) -> set[str]:
    """`text`, normalized and split into a set of words. A set, not a list -- word *order* inside
    one line/segment is not part of this module's matching signal (Whisper's segment boundaries
    do not line up with lyric line boundaries to begin with, so order across the two is not
    meaningfully comparable), only which words are present.
    """
    return set(_normalize(text).split())


def _overlap(line_words: set[str], segment_words: set[str]) -> float:
    """The fraction of `line_words` also present in `segment_words` -- the task brief's "доля
    совпавших слов в строке". Asymmetric on purpose: a Whisper segment often spans more than one
    lyric line (or includes a few extra filler words), so what matters is whether *the line's own*
    words survived, not whether the segment's words are a subset of the line's.
    """
    if not line_words:
        return 0.0
    return len(line_words & segment_words) / len(line_words)


def _line_matches_segment(line_words: set[str], segment_words: set[str],
                           threshold: float = LINE_MATCH_THRESHOLD) -> bool:
    """`True` if `line_words` and `segment_words` count as "the same line" -- both `_overlap(...)
    >= threshold` (the 50% fraction) *and* at least `MIN_SHARED_WORDS` words actually in common.
    The fraction alone lets a short line (2 words needs only 1 shared word for 50%) clear the bar
    on a thin, coincidental overlap; the absolute floor closes that specifically for short lines
    without moving the threshold itself (see `MIN_SHARED_WORDS`'s own docstring for the real
    "Колыбельная" example this is pinned to).
    """
    if not line_words:
        return False
    shared = len(line_words & segment_words)
    return shared >= MIN_SHARED_WORDS and shared / len(line_words) >= threshold


def _match_lines_to_segments(line_word_sets: list[set[str]],
                              segment_word_sets: list[set[str]],
                              threshold: float = LINE_MATCH_THRESHOLD) -> list[int | None]:
    """For each lyric line (in order), the index into `segment_word_sets` it matches, or `None` if
    nothing does. A pointer only ever advances (never backward), which is what keeps a *repeated*
    section (the same chorus sung twice) from matching every occurrence of its lines to the
    *first* time Whisper heard them: once a line matches segment K, the *next new* search starts
    at K + 1, so a later, unrelated line cannot walk back into a segment an earlier line already
    claimed.

    That would, on its own, also break the legitimate case the pointer's *not* supposed to break:
    several consecutive short lyric lines routinely fall inside one longer Whisper segment, and
    all of them should still be allowed to match it even though the pointer has moved past its
    index. So each line first checks whether it *also* matches the segment the immediately
    preceding line matched (`prev_match`, checked before the forward search, not after) -- if so,
    it reuses that segment without needing the forward search to find it again. This reuse is
    strictly one line's privilege at a time, handed off from line to line only while each next line
    keeps matching that same segment; the moment a line does not (a real content change, or the
    segments simply run out), the chain breaks and the *next* line is back to searching forward
    from the pointer only -- it cannot itself reach back into old territory. C1 (2026-08-18 review,
    real "Колыбельная" data): with the old "pointer never advances" version, a second, unsung
    occurrence of an identical section (the second `[chorus]`, never actually re-recorded) matched
    the *first* occurrence's own last segment purely because the text was identical and the pointer
    had never moved past it -- stealing a start timestamp from inside the first chorus's own span
    and, via `_section_ends`, truncating the first chorus's `end` to that same stolen value. See
    `test_songrun.py`'s regression built from the real chorus text and a real Whisper mishearing.
    """
    pointer = 0
    prev_match: int | None = None
    matches: list[int | None] = []
    for words in line_word_sets:
        found = None
        if prev_match is not None and _line_matches_segment(
                words, segment_word_sets[prev_match], threshold):
            found = prev_match
        else:
            for i in range(pointer, len(segment_word_sets)):
                if _line_matches_segment(words, segment_word_sets[i], threshold):
                    found = i
                    break
        matches.append(found)
        if found is not None:
            pointer = max(pointer, found + 1)
        prev_match = found
    return matches


def _section_ends(starts: list[float | None], duration: float | None) -> list[float | None]:
    """`end[i]` for each section: the next *known* start after `i` (skipping over any sections in
    between whose own start is `None`, so one unsung section does not blank out a perfectly good
    end for the section before it), or `duration` if `i` is the last section with a known start and
    nothing later has one either. A section whose own `start` is `None` (nothing in it matched)
    always gets `end=None` too -- there is no timestamp to anchor either boundary to.
    """
    ends: list[float | None] = [None] * len(starts)
    for i in range(len(starts)):
        if starts[i] is None:
            continue
        ends[i] = next((s for s in starts[i + 1:] if s is not None), None)
    if duration is not None:
        for i in range(len(starts)):
            if starts[i] is not None and ends[i] is None:
                ends[i] = duration
    return ends


def check_lyrics_sung(lyrics: str, segments: list[dict], *,
                       duration: float | None = None) -> tuple[list[dict], bool]:
    """Compare `lyrics` against a Whisper transcript's `segments`
    (`[{"start": float, "end": float, "text": str}, ...]`, the shape `mlx_whisper`'s JSON output
    already uses) and return `(sections, undersung)`.

    `sections` has one entry per section *occurrence* in `lyrics` (`parse_lyrics`), each
    `{"name": str, "start": float | None, "end": float | None}`: `start` is the timestamp of the
    *first* Whisper segment that matched any line belonging to that section (task brief: "первый
    сегмент, совпавший со строкой секции, задаёт start"), `None` if no line in the section matched
    anything; `end` is the next section's known start, or `duration` for a trailing section with
    nothing after it -- see `_section_ends`.

    `undersung` is computed from the *last third* of `lyrics`' lines (by count, `2*n//3` onward,
    not from any section boundary): the fraction of those lines that matched *any* segment. Below
    `UNDERSUNG_LINE_THRESHOLD` (60%), `undersung=True` -- the design spec's "недопетый хвост"
    signature: not the audio being short (Music3 finishing early on exhausted lyrics is normal,
    see `_generate_wav`), but the *tail of the lyrics* going unsung while the audio keeps playing
    (an instrumental fill-out). A Whisper hallucination on a fade-out (e.g. a stray "Субтитры
    делал..." caption-credit artifact some Whisper builds emit on near-silence) needs no special
    handling here: it is simply a segment whose words match no lyric line, so it is never chosen as
    a match by `_match_lines_to_segments` and never affects this count either way.

    Matching is intentionally fuzzy, not exact string equality (task brief) -- Whisper mishears
    individual words in sung audio (`LINE_MATCH_THRESHOLD`'s own docstring has the real example),
    so `_line_matches_segment` scores each line against each segment by word overlap (a fraction,
    `LINE_MATCH_THRESHOLD`, *and* an absolute floor, `MIN_SHARED_WORDS`) rather than requiring a
    verbatim match.
    """
    section_names, lines = parse_lyrics(lyrics)
    line_word_sets = [_word_set(text) for _, text in lines]
    segment_word_sets = [_word_set(seg.get("text", "")) for seg in segments]
    segment_starts = [seg.get("start") for seg in segments]

    matches = _match_lines_to_segments(line_word_sets, segment_word_sets)

    starts: list[float | None] = [None] * len(section_names)
    for (section_index, _text), match_idx in zip(lines, matches):
        if match_idx is not None and starts[section_index] is None:
            starts[section_index] = segment_starts[match_idx]
    ends = _section_ends(starts, duration)

    sections = [{"name": name, "start": start, "end": end}
                for name, start, end in zip(section_names, starts, ends)]

    if not lines:
        undersung = False
    else:
        cutoff = (len(lines) * 2) // 3
        tail = matches[cutoff:]
        matched_fraction = (sum(1 for m in tail if m is not None) / len(tail)) if tail else 1.0
        undersung = matched_fraction < UNDERSUNG_LINE_THRESHOLD

    return sections, undersung


def align_track(track_dir, mp3_path, lyrics: str | None, *,
                 music3_python=DEFAULT_MUSIC3_PYTHON, run=subprocess.run) -> SongResult:
    """The `track.source == "import"` path (design spec addendum, "импортированный трек"): no
    Music3 generation, no mastering -- an imported mp3 is already the finished product -- just
    `mlx_whisper` transcription and, when reference lyrics are given, the same section-alignment
    `run_song` uses, so a clip-on-a-song project built from an imported track gets scene boundaries
    the identical way a generated one does.

    **`lyrics=None` (Task 1, "Сюжет клипа" wave, "авто-лирика"): a legitimate mode, not an error.**
    A clip project may import an mp3 with *no* reference lyrics at all (design spec: "clip-проект
    с track.source=import принимает mp3 БЕЗ лирики") -- Whisper still runs exactly the same
    (`_transcribe` does not know or care whether a reference exists), but there is nothing to fuzzy-
    match its segments against, so `check_lyrics_sung` is skipped entirely: `sections` comes back
    empty (design spec: "sections тогда НЕ строятся"), `undersung` is always `False` (there is no
    lyric tail to judge as unsung), and `raw_segments` -- `None` in every other call to this
    function -- carries Whisper's own segments instead, trimmed to just `{"start", "end", "text"}`
    (not the full `mlx_whisper` segment dict, which also has `id`/`seek`/`tokens`/`temperature`/
    `avg_logprob`/`compression_ratio`/`no_speech_prob` -- noise no caller here needs). Whisper
    mishearings are fine as-is in this mode (design spec: "ослышки Whisper допустимы -- сценарий
    пишется по смыслу, не для караоке") -- nothing downstream fuzzy-matches this text against
    anything, so there is no threshold for a mishearing to fail.

    `lyrics` given as a non-empty string is the pre-existing behavior, unchanged: `sections`/
    `undersung` come from `check_lyrics_sung` exactly as before, and `raw_segments` stays `None`.

    Returns a `SongResult` shaped exactly like `run_song`'s, so task 3's worker glue does not need
    to know which path produced it before calling `project.Project.update_track(**fields)`:
    `wav`/`mastered_wav`/`mp3`/`mastered_mp3` all point at `mp3_path` itself (there is no separate
    wav, mastering pass or mp3 encode step for an import -- the file the human uploaded *is* all
    four of those roles at once). `duration` is `mp3_path`'s own container-level length, read via
    `ffprobe` (`_ffprobe_duration`) -- **not** the last Whisper segment's own end timestamp (fix
    round 1, I4, 2026-08-18 review: that undercounts whenever the file has any audible tail past the
    last recognized word -- a fade, an instrumental outro, applause).

    `track_dir` is created if missing, matching `run_song`; only `whisper.json` lands inside it
    (there is no `song.*` to write -- the caller already has the audio at `mp3_path`).

    Raises `SongRunError` if `mlx_whisper` or `ffprobe` fails, exactly as `run_song`'s own
    `_transcribe` does -- never a bare subprocess/JSON exception.
    """
    track_dir = Path(track_dir)
    track_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = Path(mp3_path)

    whisper_data = _transcribe(music3_python, mp3_path, track_dir, run=run)
    segments = whisper_data.get("segments", [])
    duration = _ffprobe_duration(mp3_path, run=run)
    transcript = (whisper_data.get("text") or "").strip()

    if lyrics is None:
        sections: list[dict] = []
        undersung = False
        raw_segments: list[dict] | None = [
            {"start": seg.get("start"), "end": seg.get("end"),
             "text": (seg.get("text") or "").strip()}
            for seg in segments
        ]
    else:
        sections, undersung = check_lyrics_sung(lyrics, segments, duration=duration)
        raw_segments = None

    return SongResult(
        wav=mp3_path,
        mastered_wav=mp3_path,
        mp3=mp3_path,
        mastered_mp3=mp3_path,
        duration=duration,
        transcript=transcript,
        sections=sections,
        undersung=undersung,
        raw_segments=raw_segments,
    )


def run_song(track_dir, lyrics: str, caption: str, duration: float, *, seed: int, steps: int = 30,
             music3_python=DEFAULT_MUSIC3_PYTHON, model_dir=DEFAULT_MUSIC3_MODEL_DIR,
             run=subprocess.run) -> SongResult:
    """Run one song job to completion: generate, master, encode both to mp3, transcribe, check.
    `track_dir` is created if missing; every artifact (`caption.txt`, `lyrics.txt`, `gen.log`,
    `song.wav`, `song.mastered.wav`, `song.mp3`, `song.mastered.mp3`, `whisper.json`) lands inside
    it, matching `queue.py`'s "one job, one directory" shape.

    `run` is the one seam every subprocess call in this module goes through
    (default `subprocess.run`) -- the same injection `h3_48gb.worker.run_job` uses `spawn` for, and
    for the same reason: **`tests/test_songrun.py` never actually runs Music3 or `mlx_whisper`**
    (the GPU may be busy with something else entirely when the test suite runs, and `mlx_whisper`
    needs its own downloaded weights) -- every test substitutes a fake `run`. Only the mastering
    step (`master`) is exercised against a real `ffmpeg`, on a synthetic sine wave.

    Whisper transcribes `mastered_wav`, not the raw take -- the loudness-normalized, EQ'd audio is
    the more legible signal for speech recognition, and it is what a human would actually listen to
    on the track gate page.

    Raises `SongRunError` (never a bare subprocess/JSON exception) if any of the four steps fails.
    Nothing here writes to `project.json` -- returning a `SongResult` and leaving the
    `project.update_track(**fields)` call to the caller (task 3) is deliberate: this function does
    not know which project a song job belongs to, only how to run one.

    **Retries start clean** (I4, 2026-08-18 review): before generating anything, `run_song` deletes
    any `song.*`/`whisper.json` already sitting in `track_dir` from a previous, failed attempt.
    Task 3: a retried song job must call `run_song` with the *same* `track_dir` it used the first
    time, not a fresh one -- this is what makes that safe. Without it, a half-finished take from an
    earlier failed attempt (say, mastering crashed after `song.wav` was written but before
    `song.mastered.mp3` existed) would leave `song.mp3`/`song.wav` sitting in `track_dir` looking
    like a complete, playable track to anything that only checks "does the file exist", even though
    the job as a whole never finished and no `SongResult` was ever produced for it.
    """
    track_dir = Path(track_dir)
    track_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*track_dir.glob("song.*"), track_dir / "whisper.json"]:
        stale.unlink(missing_ok=True)

    wav_path, actual_duration = _generate_wav(
        music3_python, model_dir, caption, lyrics, duration, steps, seed, track_dir, run=run)

    mastered_wav = master(wav_path, track_dir / "song.mastered.wav", run=run)

    # Transcribe (and mp3-encode only after that succeeds, I4, 2026-08-18 review): `song.mp3`/
    # `song.mastered.mp3` are the product a `kind="song"` track ships -- their presence on disk is
    # what makes an unfinished take look like a done one. Encoding them only once Whisper has
    # actually run keeps a Whisper failure (a real, not-uncommon step to fail: its own venv, its
    # own downloaded weights) from leaving a `song.mastered.mp3` behind that looks complete but
    # was never actually validated.
    whisper_data = _transcribe(music3_python, mastered_wav, track_dir, run=run)
    segments = whisper_data.get("segments", [])
    sections, undersung = check_lyrics_sung(lyrics, segments, duration=actual_duration)

    mp3 = to_mp3(wav_path, track_dir / "song.mp3", run=run)
    mastered_mp3 = to_mp3(mastered_wav, track_dir / "song.mastered.mp3", run=run)

    return SongResult(
        wav=wav_path,
        mastered_wav=mastered_wav,
        mp3=mp3,
        mastered_mp3=mastered_mp3,
        duration=actual_duration,
        transcript=(whisper_data.get("text") or "").strip(),
        sections=sections,
        undersung=undersung,
    )
