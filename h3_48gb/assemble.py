"""Final assembly (`kind="assemble"` job body) and the scene chain (`advance_project`) that feeds
it -- task 4, "Проекты": the last two moving parts of `<outdir>/projects/<id>/` turning into a
finished `final.mp4`.

**Two things live here and nothing else does.** `run(project_path)` is the ffmpeg concat: every
`done` scene's clip, one profile, one straight-cut timeline (design spec, "Сборка": "переходы v1 --
только прямые склейки"), with the project's song muxed in if `assembly.audio_mode` calls for one.
`advance_project(project, queue_root, outdir)` is the scene chain: what happens next once a scene's
own `kind="generate"` job finishes -- extract an automatic keyframe, submit the next scene, or, once
every scene is `done`, submit the `assemble` job that reaches `run` above. Task 6 (the web layer,
not built yet) submits *scene 0*; from there on `h3_48gb.worker`'s own post-job hook is the only
caller of `advance_project`, once per `done`/`failed` scene job (see `worker.py`'s
`_handle_project_scene_result`).

**Lyric-video-director's proven rules, transplanted whole (task brief, "ЖЁСТКИЕ ПРАВИЛА СБОРКИ"):**
a song is sacred -- never `-shortest` (which silently truncates whichever stream is longer, audio
included) and never any audio time-stretch; when the final render's audio *is* the song
(`audio_mode in ("song", "mix")`), the assembled video's own length must land within
`DURATION_TOLERANCE_SECONDS` of the *track's* measured duration (`project.track["duration"]`, an
`ffprobe` reading -- Task 3's own I4 fix -- never `sections[-1]["end"]`, which is a *lyric* boundary
and routinely stops short of the track's actual audio, e.g. an instrumental outro with no sung
line); a video that comes up short against that target is padded with a freeze-frame of its own
last frame, never by touching the audio.

**No `run=` on `run()` used to mean a second `SIGTERM` mid-assembly could not reach ffmpeg** (task 3
review, fix round 1: the same class of gap C1 closed for a song job's own subprocess). Fixed in this
task's own review round: `run(project_path, *, run=subprocess.run)` takes the same
`run(cmd, capture_output=True, text=True) -> CompletedProcess` seam `songrun.py` already uses, and
`h3_48gb.worker._run_assemble_job` passes `_tracked_child_run(spawn)` through it exactly as it
already does for a song job -- see that function's own docstring for the mechanism.

**No `mlx` import, ever** -- same discipline as `h3_48gb.worker`/`h3_48gb.songrun` (see their own
module docstrings): this module runs inside the worker process, which sits idle for days between
30+ GB generations. Every subprocess this module drives is `ffmpeg`/`ffprobe`, never MLX, and a
project scene's own generation runs as an ordinary `kind="generate"` job the *existing* subprocess
dispatch already handles -- `advance_project` only ever *submits* one, through `queue.submit`, never
runs one itself.

**`advance_project` never queries the CLI for a scene's canvas.** Every scene job is submitted with
an explicit `--width`/`--height` (`DEFAULT_SCENE_CANVAS`), never "derive it from the keyframe" the
way a human filling out the web form can ask for (`autoCanvasAllowed`, `app.js`) -- deriving a
canvas from an image needs `resolve_canvas`, which imports `mlx.core`, and that import must never
happen in this process (see above). Passing both flags explicitly makes `queue.submit`'s own
`output_stem` computable in pure Python (`RunSpec.output_stem`, `cli.py`: `outdir /
f"h3-{tag}-{width}x{height}"`, and neither half of that format is touched by anything an image could
change) instead of needing a real `generate --dry-run` subprocess to ask the CLI what it would be --
one fewer subprocess, and one that would otherwise need its own `python -m h3_48gb generate
--dry-run --json` round trip for every single scene. This is a deliberate v1 simplification, not
something the design spec asked for either way -- see the module's own report for why, and what a
per-project canvas override would need to look like if task 6 wants one later.

Messages a human reads are Russian; comments and docstrings are English -- same split as the rest of
the package (see `h3_48gb.queue`'s module docstring).
"""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from h3_48gb import project as project_module
from h3_48gb import queue as q

#: The four steps' shared frame rate for the final concat (design spec: "перекодировка в единый
#: профиль ... общий fps"). A fixed constant, not read off any one clip: clips can come from
#: different scenes generated at different times, and picking "whatever the first clip happens to
#: be" would make the render's own frame rate an accident of scene order.
ASSEMBLY_FPS = 24

#: The canvas every project scene's `generate` job is submitted with -- see the module docstring's
#: "`advance_project` never queries the CLI for a scene's canvas" for why this is fixed rather than
#: derived from a keyframe. Matches `cli.DEFAULT_CANVAS` so a project scene looks, by default,
#: exactly like what a human leaving the web form's canvas fields untouched would get.
DEFAULT_SCENE_CANVAS = (896, 512)

#: `h3 generate`'s own default step count (`cli.DEFAULT_STEPS`), duplicated here rather than
#: imported -- `cli.py` is safe to import (it does not pull in `mlx` at module level, see
#: `web.py`'s own `from h3_48gb.cli import ...`), but this module's only use for the number is
#: `_scene_wallclock_estimate_seconds`'s rough queue-page estimate, and a second import for one
#: integer is not worth the coupling.
DEFAULT_SCENE_STEPS = 8

#: Design spec, "Клипы": "кадр из предпоследней секунды предыдущего клипа" -- the automatic
#: keyframe is taken this far before a clip's own measured end, floored at 0 for a clip shorter
#: than this (task brief: "время = max(0, dur-1.5)").
KEYFRAME_LEAD_SECONDS = 1.5

#: Review round I2: `docs/h3-prompt-system.md`'s own "The first line, for image-conditioned
#: modes" -- the literal, word-for-word sentence a `mode: i2v` run's prompt must open with,
#: reproduced verbatim rather than derived. Task 5's own system prompt tells the model never to
#: write this line into a scene's own `prompt` text (the scene is written before its mode is
#: known -- `t2v` for scene 0, `i2v` off an automatic keyframe for every scene after it), so this
#: module has to add it itself once it actually knows a keyframe exists, the same way
#: `webui/app.js`'s `buildPromptText` joins an ordinary chat turn's own `prompt.instruction` field
#: onto the three labelled fields with a blank line between them.
SCENE_I2V_INSTRUCTION = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)

#: Task brief: "для kind=clip длительность final == длительность трека ±0.5 с". Applied to every
#: `audio_mode` whose final audio *is* the track (`"song"`/`"mix"`), not only `kind == "clip"` --
#: see the module docstring's "Lyric-video-director's proven rules" for why the audio_mode is the
#: actual trigger, kind only usually implies it (a `kind="clip"` project defaults to
#: `audio_mode="song"`, design spec, but nothing stops a `kind="video"` project from opting into a
#: song too, design spec "Суть": "звук ... опционально песня").
DURATION_TOLERANCE_SECONDS = 0.5

#: Below this, an unpadded video is already inside `DURATION_TOLERANCE_SECONDS` on its own -- no
#: freeze-frame ffmpeg call is worth spending on a shortfall this small.
_FREEZE_PAD_EPSILON_SECONDS = 0.05

#: P0-2 (боевые ворота 2026-08-19): a chaining scene's own frame 0 is *supposed* to duplicate the
#: automatic keyframe the previous scene's clip already ended on (design spec, "Клипы": "кадр из
#: предпоследней секунды предыдущего клипа") -- the repro (`20260819-0518-scene-1-e0a3`) landed an
#: i2v run's own frame 0 corrupted (RGB banding/blocking) instead of a clean copy of the reference,
#: right on the visible seam between two scenes, the single most noticeable place a defect like
#: this could land. `_chaining_scene_indices` below is exactly "every scene idx>0" -- scene 0 has
#: no keyframe behind it (design spec, "Первая сцена: t2v") and is never touched.

#: P1-3 (боевые ворота 2026-08-19): ffmpeg's own `signalstats` filter has no direct standard-
#: deviation output (`ffmpeg -h filter=signalstats` lists only MIN/LOW/AVG/HIGH/MAX per plane) --
#: `_frame_luma_spread` uses `YHIGH-YLOW` (the 5%/95%-trimmed luma range) as the cheap stand-in
#: instead. A flat, single-colour fill (the boевые ворота repro's own grey tail) collapses this to
#: 0; any frame with real picture content spreads it out by orders of magnitude more than this
#: threshold, so the substitution still separates the two cases the task brief's own "стандартное
#: отклонение ... > ~4-6 из 255" was aimed at.
_FREEZE_FRAME_LUMA_SPREAD_THRESHOLD = 6.0

#: How far back into a clip's own tail `_extract_valid_last_frame` is willing to look for a frame
#: that clears `_FREEZE_FRAME_LUMA_SPREAD_THRESHOLD` before giving up and falling back to the
#: literal last frame anyway (task brief: "все 24 плоские -- берём последний как раньше").
_FREEZE_FRAME_MAX_LOOKBACK_FRAMES = 24


class AssembleError(RuntimeError):
    """Raised when a subprocess this module drives (`ffmpeg`/`ffprobe`) exits non-zero or produces
    output this module cannot make sense of, or when the project itself is not in a shape `run`/
    `advance_project` can act on (an unfinished scene, a missing clip, a duration that will not
    converge). Never lets a bare `CalledProcessError`/`OSError`/`KeyError` escape -- matching
    `songrun.SongRunError`'s own discipline, for the same reason: a caller (`worker.py`'s job
    dispatch) catches exactly one exception type for "this job failed".
    """


# -- Subprocess plumbing (mirrors `songrun._run_or_raise`/`_run`) --------------------------------


def _run_or_raise(cmd: list[str], run, what: str) -> subprocess.CompletedProcess:
    try:
        return run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise AssembleError(f"{what} failed to start: {exc}") from exc


def _run_ffmpeg(cmd: list[str], run, what: str) -> subprocess.CompletedProcess:
    result = _run_or_raise(cmd, run, what)
    if result.returncode != 0:
        raise AssembleError(f"{what} exited {result.returncode}: {(result.stderr or '').strip()}")
    return result


_FFPROBE_DURATION_CMD = ("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0")


def _ffprobe_duration(path, *, run) -> float:
    result = _run_ffmpeg([*_FFPROBE_DURATION_CMD, str(path)], run, f"ffprobe duration ({path})")
    raw = (result.stdout or "").strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise AssembleError(f"ffprobe produced an unparseable duration for {path}: {raw!r}") \
            from exc


_FFPROBE_AUDIO_STREAMS_CMD = ("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                               "stream=index", "-of", "csv=p=0")


def _has_audio_stream(path, *, run) -> bool:
    """Whether `path` has at least one audio stream -- a readable-error preflight for `"mix"`
    mode's clip-audio concat (module docstring's minor fix, review round 1): without it, a clip
    missing its own audio track (a silent generation, a hand-edited project) makes the concat
    demuxer or `amix` fail with an opaque ffmpeg stderr blob a human has to decode by hand, instead
    of the readable `AssembleError` `run` raises when this comes back `False`.
    """
    result = _run_ffmpeg([*_FFPROBE_AUDIO_STREAMS_CMD, str(path)], run,
                          f"ffprobe audio-stream probe ({path})")
    return bool((result.stdout or "").strip())


# -- P0-2 (боевые ворота 2026-08-19): drop a chaining scene's own duplicate/corrupted frame 0 -----


def _chaining_scene_indices(scenes) -> list[int]:
    """Which of `scenes` need their own clip's frame 0 dropped before assembly -- every scene after
    the first (design spec, "Клипы": "Первая сцена: t2v (или i2v, если ... загружен стартовый
    кадр)" -- every scene *after* scene 0 is chained off the previous scene's own automatic
    keyframe, scene 0 never is, regardless of whether it happens to be i2v off an uploaded
    `start_image`). This is the "plan" half of the fix, kept as a pure function deliberately: a
    test can assert exactly which indices it names without spinning up any `ffmpeg` call at all.
    """
    return sorted(scene["idx"] for scene in scenes if scene["idx"] > 0)


def _drop_first_frame(clip_path, out_path: Path, *, run) -> Path:
    """A copy of `clip_path` with its own frame 0 removed -- the fix for P0-2 (боевые ворота
    2026-08-19): a chaining scene's frame 0 is *meant* to duplicate the automatic keyframe the
    previous scene's own clip already ended on (`_extract_keyframe`'s own "кадр из предпоследней
    секунды предыдущего клипа"), but the repro landed an i2v run's own frame 0 corrupted (RGB
    banding/blocking) instead -- squarely on the one frame most likely to sit right on the visible
    cut between two scenes. Dropping it does not reopen the seam: the cut becomes "scene i's own
    last frame -> scene i+1's own *second* frame", which reads as the identical cut to a human,
    since frame 0 and frame 1 of an i2v run are meant to look like the same reference picture held
    for a beat.

    Re-encoded to the same profile every other step in this module uses (crf 18/yuv420p/aac), so
    the file this writes needs no further re-encode wherever `run` uses it downstream. Audio is
    carried through unmodified via an *optional* map (`0:a:0?`) -- a scene clip may have no audio
    track at all (`_has_audio_stream`'s own precondition, checked separately later in `run`), and
    this step must not fail over a stream that was simply never there.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip_path),
           "-vf", "trim=start_frame=1,setpts=PTS-STARTPTS",
           "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", str(out_path)]
    _run_ffmpeg(cmd, run, f"ffmpeg first-frame drop ({clip_path})")
    return out_path


def _build_video_clip_paths(scenes, raw_clip_paths, workdir: Path, *, run) -> list[str]:
    """The path list every video-bearing concat call in `run` uses in place of the scenes' own raw
    `clip_path`s -- scene 0's own clip passed through untouched, every chaining scene's clip
    (`_chaining_scene_indices`) replaced with `_drop_first_frame`'s own trimmed copy. Kept separate
    from the raw `clip_paths` list `run` also keeps around: the audio-only concat/preflight
    (`"mix"` mode) reads the clips' own *original* audio, never this trimmed copy, since P0-2 is a
    picture-only fix -- nothing about a chaining scene's own audio track duplicates anything.
    """
    chaining = set(_chaining_scene_indices(scenes))
    paths = []
    for scene, raw_path in zip(scenes, raw_clip_paths):
        if scene["idx"] not in chaining:
            paths.append(raw_path)
            continue
        out_path = workdir / f"scene-{scene['idx']:03d}-trimmed.mp4"
        paths.append(str(_drop_first_frame(raw_path, out_path, run=run)))
    return paths


# -- ffmpeg concat / mux building blocks ----------------------------------------------------------


def _write_concat_list(paths, list_path: Path) -> None:
    """The concat demuxer's own file format: one `file '<absolute path>'` line per input, in
    order -- direct cuts only (design spec, "переходы v1"), nothing between two entries. Single
    quotes inside a path are escaped the way the concat protocol itself documents
    (`'` -> `'\\''`) -- unlikely in practice (a project's own directories, not free text a human
    typed), but a broken quote here would silently truncate the list mid-file rather than raise.
    """
    lines = [f"file '{str(Path(p).resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
              for p in paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _concat(paths, out_path: Path, *, run, video: bool, audio: bool) -> Path:
    """Concat `paths` (already-existing clips) into `out_path`, re-encoded to one profile (design
    spec: "перекодировка в единый профиль ... libx264 crf 18, общий fps"). `video`/`audio` pick
    which stream(s) survive: `video=True, audio=False` for the video-only track `run` needs before
    it knows which audio to attach (song replacement, freeze-frame math); `video=False, audio=True`
    for the clips' own diegetic audio alone (`"mix"` mode's other ingredient); both `True` for
    `audio_mode == "clips"`, where the clips' own sound *is* the final render's sound and nothing
    downstream needs to touch it separately.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = out_path.parent / f"{out_path.stem}.concat.txt"
    _write_concat_list(paths, list_path)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_path)]
    if video:
        cmd += ["-vf", f"fps={ASSEMBLY_FPS}", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-vn"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd.append(str(out_path))
    _run_ffmpeg(cmd, run, "ffmpeg concat")
    return out_path


def _extract_frame_at_lookback(video_path, out_path: Path, frames_back: int, *, run) -> Path:
    """The frame `frames_back` frames from the end of `video_path` (1 = the literal last frame) --
    `_extract_valid_last_frame`'s own building block for walking backward through a clip's tail
    (task brief, P1-3: "извлекать кадры с хвоста по одному"). `reverse,select=eq(n\\,K)`
    (`K = frames_back - 1`) rather than a `-sseof` timestamp: a `-sseof` pad small enough to target
    a single specific frame a handful of frames before the end landed off by a frame or more
    against a real encode (checked empirically against a real `ffmpeg` output while building this
    fix -- see the module's own report) -- `reverse` decodes the whole clip and re-indexes it from
    the end, so `select`'s own frame-index match is exact regardless of how short the gap to the
    real end is.
    """
    k = frames_back - 1
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
           "-vf", f"reverse,select=eq(n\\,{k})", "-frames:v", "1", str(out_path)]
    _run_ffmpeg(cmd, run, f"ffmpeg tail-frame extraction (n={frames_back})")
    return out_path


def _frame_luma_spread(image_path, *, run) -> float:
    """A cheap stand-in for the still frame `image_path`'s own luma standard deviation (module
    constant `_FREEZE_FRAME_LUMA_SPREAD_THRESHOLD`'s own docstring explains the substitution:
    ffmpeg's `signalstats` filter has no direct stddev output) -- `YHIGH-YLOW`, the 5%/95%-trimmed
    luma range, read back off `metadata=print`'s own stdout. A flat, single-colour fill collapses
    this to (near) 0; any frame with real picture content spreads it out far past the threshold.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(image_path),
           "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"]
    result = _run_ffmpeg(cmd, run, f"ffmpeg signalstats ({image_path})")
    stdout = result.stdout or ""
    low_match = re.search(r"lavfi\.signalstats\.YLOW=([0-9.]+)", stdout)
    high_match = re.search(r"lavfi\.signalstats\.YHIGH=([0-9.]+)", stdout)
    if low_match is None or high_match is None:
        raise AssembleError(
            f"ffmpeg signalstats produced no YLOW/YHIGH reading for {image_path}: "
            f"{stdout.strip()!r}")
    return float(high_match.group(1)) - float(low_match.group(1))


def _extract_valid_last_frame(video_path, out_path: Path, *, run) -> Path:
    """P1-3 (боевые ворота 2026-08-19): the freeze-frame pad's own source frame, chosen for
    *validity* instead of blindly grabbing the literal last frame -- the failing gate's own clip
    ended on a run of flat grey frames, and the old unconditional `_extract_last_frame` froze that
    mush across the entire padded tail.

    Walks backward from the literal last frame, up to `_FREEZE_FRAME_MAX_LOOKBACK_FRAMES` frames,
    extracting straight into `out_path` each try (a fresh `_extract_frame_at_lookback` call
    overwrites whatever the previous try left there -- no separate candidate file or python-level
    copy needed, since `out_path` already holds exactly the frame this returns the moment a try
    clears `_FREEZE_FRAME_LUMA_SPREAD_THRESHOLD`). If every candidate in that window reads flat,
    re-extracts the literal last frame (task brief: "все 24 плоские -- берём последний как раньше,
    с warning в лог") -- padding with *something* the video already had beats refusing to assemble
    at all, and this module's own caller (`run`) has no better fallback to reach for.
    """
    for n in range(1, _FREEZE_FRAME_MAX_LOOKBACK_FRAMES + 1):
        _extract_frame_at_lookback(video_path, out_path, n, run=run)
        if _frame_luma_spread(out_path, run=run) > _FREEZE_FRAME_LUMA_SPREAD_THRESHOLD:
            return out_path
    print(f"WARNING: freeze-frame source for {video_path} -- all "
          f"{_FREEZE_FRAME_MAX_LOOKBACK_FRAMES} frames from the tail read as a flat fill (luma "
          f"spread <= {_FREEZE_FRAME_LUMA_SPREAD_THRESHOLD}) -- falling back to the literal last "
          f"frame", file=sys.stderr, flush=True)
    _extract_frame_at_lookback(video_path, out_path, 1, run=run)
    return out_path


def _freeze_segment(image_path, seconds: float, out_path: Path, *, run) -> Path:
    """`image_path` held for `seconds`, encoded to the same profile every other step here uses, so
    concatenating it onto the end of the video-only track (`_pad_with_freeze_frame`) needs no
    further re-encode.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(image_path),
           "-t", f"{seconds:.3f}", "-vf", f"fps={ASSEMBLY_FPS}", "-c:v", "libx264", "-crf", "18",
           "-pix_fmt", "yuv420p", str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg freeze-frame segment")
    return out_path


def _pad_with_freeze_frame(video_path: Path, pad_seconds: float, workdir: Path, *, run) -> Path:
    """`video_path` (video-only) with `pad_seconds` of its own last *valid* frame appended -- task
    brief: "недостающий хвост видео добивается freeze-frame последнего кадра". Never touches audio:
    this runs before `run()` ever attaches one, and the whole point of padding video instead of
    trimming audio is that the audio is never the thing that moves (module docstring: "a song is
    sacred").

    P1-3 (боевые ворота 2026-08-19): "last frame" is `_extract_valid_last_frame`'s own validity-
    checked pick, not the literal last frame -- the failing gate's own clip ended on a run of flat
    grey frames, and the old unconditional last-frame grab froze that mush for the whole pad.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    last_frame = workdir / "last_frame.png"
    _extract_valid_last_frame(video_path, last_frame, run=run)
    freeze_clip = workdir / "freeze_tail.mp4"
    _freeze_segment(last_frame, pad_seconds, freeze_clip, run=run)
    padded = workdir / "padded.mp4"
    _concat([video_path, freeze_clip], padded, run=run, video=True, audio=False)
    return padded


def _mux_song_audio(video_path, audio_path, out_path: Path, *, run) -> Path:
    """`audio_mode == "song"`: the track replaces the clips' own diegetic sound entirely.
    Deliberately no `-shortest` (module docstring: "a song is sacred -- never `-shortest`") -- by
    the time this runs, `video_path`'s own length has already been padded and validated against the
    track's duration (`run`'s own flow), so the two streams are within
    `DURATION_TOLERANCE_SECONDS` of each other and neither needs `-shortest`'s silent truncation to
    line up.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-i", str(audio_path),
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg audio mux")
    return out_path


def _mux_mixed_audio(video_path, clip_audio_path, track_audio_path, out_path: Path, *, run) -> Path:
    """`audio_mode == "mix"`: the track underneath the clips' own diegetic sound, the clips'
    channel attenuated -18 dB (design spec, "Сборка": "диегетический звук клипов ... микшируется на
    низкой громкости"; task brief: "amix с клипами на −18 дБ"). `amix=duration=longest` -- not the
    filter's own default (`"longest"` is actually the default, spelled out here so the choice reads
    as deliberate) -- pads whichever input is shorter with silence rather than truncating the
    longer one, the same "never truncate the song" discipline `_mux_song_audio`'s missing
    `-shortest` already follows, just inside a filter instead of at the muxer.
    """
    filter_complex = ("[1:a]volume=-18dB[clipaud];"
                       "[clipaud][2:a]amix=inputs=2:duration=longest[aout]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", str(video_path), "-i", str(clip_audio_path), "-i", str(track_audio_path),
           "-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg mixed audio mux")
    return out_path


# -- run(): the assemble job body -----------------------------------------------------------------


def run(project_path, *, run=subprocess.run) -> Path:
    """Assemble every `done` scene's clip into `<project>/assembly/final.mp4` and return its path.
    This *is* the `kind="assemble"` job body -- `h3_48gb.worker._run_assemble_job` calls it, under
    the same lease/caffeinate wrapping every other job kind gets (see `worker.py`'s own docstrings).

    `run` (the parameter shadows the function's own name deliberately, matching `songrun.master`'s
    own `run=subprocess.run` convention) is `run(cmd, capture_output=True, text=True) ->
    CompletedProcess` -- the seam every `ffmpeg`/`ffprobe` call in this module goes through, so a
    test never has to shell out for real, and so `worker._run_assemble_job` can inject
    `_tracked_child_run(spawn)` the same way it already does for a song job's own subprocess (see
    the module docstring's "No `run=`" note).

    Refuses (`AssembleError`) a project whose `kind` has no assembly stage (`"song"` -- design
    spec: "для kind=song проект на этом завершён"), one with no scenes at all, or one with any
    scene not yet `"done"` -- `advance_project` is the only thing that should ever submit this job,
    and it only does once every scene is done (see its own docstring), so reaching any of these
    states means something upstream skipped a check, not a case to degrade gracefully for.

    **The duration/freeze-frame contract** (module docstring, "Lyric-video-director's proven
    rules") applies whenever `assembly.audio_mode in ("song", "mix")`: concat every clip's video
    stream alone, measure it, pad with a freeze-frame of its own last frame if it falls short of
    `project.track["duration"]` by more than `_FREEZE_PAD_EPSILON_SECONDS`, then insist the result
    is within `DURATION_TOLERANCE_SECONDS` of the track either way -- a shortfall the padding
    could not close, or a video that was already *longer* than the track by more than the
    tolerance (padding never trims), both raise `AssembleError` rather than silently shipping a
    render whose picture and song do not line up. `audio_mode == "clips"` skips all of this: there
    is no track to measure against, so the concatenated clips' own combined length is the answer.

    **P0-2 (боевые ворота 2026-08-19): every measurement above already reflects the frame-0 drop.**
    `video_clip_paths` (`_build_video_clip_paths`) replaces every chaining scene's raw clip with a
    copy that has had its own frame 0 removed *before* the first `_concat`/`_ffprobe_duration` call
    in this function ever runs -- the shortfall this measures, and the freeze-frame pad it computes
    to close it, are both already sized against the *actual*, post-drop frame count, not the
    pre-drop one. Nothing downstream needs a separate correction for the dropped frames.

    **I4 (fix round 1, 2026-08-18 review): the same tolerance check is repeated against
    `final.mp4` itself**, after the mux step, not only against the pre-mux, video-only concat --
    the mux (`amix`'s own `duration=longest`, an unexpected container quirk) is a second place a
    drift could sneak in between "the video-only track measured right" and "the file a human
    actually opens is right".

    On success, records `assembly.final_path` and `stages.assembly = "done"` on the project through
    its own locked mutators (`update_assembly`/`set_stage_status`) -- mirroring
    `worker._run_song_job`'s own "only locked mutators, never `save()`" discipline, even though
    this function, unlike that one, is not itself the worker glue: `advance_project` may be reading
    the same project concurrently (through its own `Project` object) as this runs, and a blind
    `save()` here could clobber whatever it just wrote.
    """
    proj = project_module.load_project(project_path)
    if proj.kind not in ("video", "clip"):
        raise AssembleError(
            f"project {proj.id!r} is kind={proj.kind!r}, which has no assembly stage")
    scenes = sorted(proj.scenes, key=lambda scene: scene["idx"])
    if not scenes:
        raise AssembleError(f"project {proj.id!r} has no scenes to assemble")
    not_done = [scene["idx"] for scene in scenes if scene.get("status") != "done"]
    if not_done:
        raise AssembleError(f"project {proj.id!r} has scenes not yet done: {not_done}")
    clip_paths = []
    for scene in scenes:
        clip_path = scene.get("clip_path")
        if not clip_path:
            raise AssembleError(f"scene {scene['idx']} of {proj.id!r} is done but has no clip_path")
        clip_paths.append(clip_path)

    assembly_dir = proj.path.parent / "assembly"
    assembly_dir.mkdir(parents=True, exist_ok=True)
    audio_mode = proj.assembly.get("audio_mode", "clips")
    final_path = assembly_dir / "final.mp4"

    # P0-2 (боевые ворота 2026-08-19): every concat call below that touches the *picture* uses
    # `video_clip_paths`, not the scenes' own raw `clip_paths` -- a chaining scene's own clip has
    # had its frame 0 dropped (`_build_video_clip_paths`/`_drop_first_frame`). Audio-only work
    # (`_has_audio_stream`'s preflight, the "mix"-mode clip-audio concat below) still reads the
    # untouched `clip_paths` -- this fix is picture-only, nothing about a chaining scene's own
    # audio track duplicates anything the way frame 0 does.
    video_clip_paths = _build_video_clip_paths(scenes, clip_paths, assembly_dir / "trim", run=run)

    if audio_mode in ("song", "mix"):
        mastered_mp3 = proj.track.get("mastered_mp3")
        track_duration = proj.track.get("duration")
        if not mastered_mp3 or track_duration is None:
            raise AssembleError(
                f"audio_mode={audio_mode!r} needs a mastered track with a known duration, "
                f"project {proj.id!r} has mastered_mp3={mastered_mp3!r} duration={track_duration!r}")

        video_only = _concat(video_clip_paths, assembly_dir / "concat_video.mp4", run=run,
                              video=True, audio=False)
        video_duration = _ffprobe_duration(video_only, run=run)
        shortfall = track_duration - video_duration
        if shortfall > _FREEZE_PAD_EPSILON_SECONDS:
            video_only = _pad_with_freeze_frame(
                video_only, shortfall, assembly_dir / "pad", run=run)
            video_duration = _ffprobe_duration(video_only, run=run)

        if abs(video_duration - track_duration) > DURATION_TOLERANCE_SECONDS:
            raise AssembleError(
                f"assembled video is {video_duration:.2f}s, track is {track_duration:.2f}s -- "
                f"outside the {DURATION_TOLERANCE_SECONDS}s tolerance even after freeze-frame "
                f"padding")

        if audio_mode == "song":
            _mux_song_audio(video_only, mastered_mp3, final_path, run=run)
        else:
            missing_audio = [p for p in clip_paths if not _has_audio_stream(p, run=run)]
            if missing_audio:
                raise AssembleError(
                    f"audio_mode='mix' needs every clip to carry its own audio track to mix "
                    f"underneath the song -- missing an audio stream: {missing_audio}")
            clip_audio = _concat(clip_paths, assembly_dir / "concat_audio.m4a", run=run,
                                  video=False, audio=True)
            _mux_mixed_audio(video_only, clip_audio, mastered_mp3, final_path, run=run)

        # I4 (fix round 1, 2026-08-18 review): the duration check above only ever measured the
        # video-only concat, *before* the mux step attached the track -- catching a padding
        # shortfall, but never anything the mux itself could introduce (a filter's own `duration=`
        # policy, a container quirk). `final.mp4` is what a human actually opens, so it is the one
        # ffprobe reading that must land within tolerance before this is ever marked `done`.
        final_duration = _ffprobe_duration(final_path, run=run)
        if abs(final_duration - track_duration) > DURATION_TOLERANCE_SECONDS:
            raise AssembleError(
                f"final.mp4 is {final_duration:.2f}s, track is {track_duration:.2f}s -- outside "
                f"the {DURATION_TOLERANCE_SECONDS}s tolerance after muxing the audio in")
    else:
        _concat(video_clip_paths, final_path, run=run, video=True, audio=True)

    proj.update_assembly(final_path=str(final_path))
    proj.set_stage_status("assembly", "done")
    # Minor fix (review round 1): every intermediate file `run` wrote on the way to `final.mp4` is
    # only useful for diagnosing a *failed* assembly -- once the run above has actually succeeded
    # (every check has already passed and the project is already marked `done`), keep only
    # `final.mp4` itself. A failure anywhere above this point returns/raises before reaching here,
    # so the intermediates from a failed attempt are always left in place for a human to inspect.
    _cleanup_intermediate_assembly_files(assembly_dir)
    return final_path


def _cleanup_intermediate_assembly_files(assembly_dir: Path) -> None:
    """Remove the video-only concat, the `"mix"`-mode clip-audio-only concat, every concat-demuxer
    list file `_concat` leaves next to them, the freeze-frame pad workdir, and the P0-2
    frame-0-drop workdir -- everything `run` may have written under `assembly_dir` on its way to
    `final.mp4` besides `final.mp4` itself. Best-effort: a stray permission error here must not
    turn an otherwise-successful assembly into a failed job over disk cleanup, so this never
    raises.
    """
    for name in ("concat_video.mp4", "concat_video.concat.txt",
                 "concat_audio.m4a", "concat_audio.concat.txt"):
        path = assembly_dir / name
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    for dir_name in ("pad", "trim"):
        dir_path = assembly_dir / dir_name
        try:
            if dir_path.is_dir():
                shutil.rmtree(dir_path, ignore_errors=True)
        except OSError:
            pass


# -- Scene note: how a project scene's ordinary `kind="generate"` job is tagged ------------------


#: Design spec, "Очередь/воркер": "Клипы -- обычные generate-задачи с пометкой project/scene в
#: note". This is that format, in full -- `h3_48gb.worker`'s post-job hook (`_handle_project_scene_
#: result`) is the only reader; task 6 (submitting scene 0) must build the exact same string via
#: `scene_note`, not its own paraphrase, or the worker will never recognize scene 0's own job as a
#: project scene when it finishes.
_SCENE_NOTE_RE = re.compile(r"^project scene (?P<project_id>\S+) #(?P<idx>\d+)$")


def scene_note(project, idx: int) -> str:
    """The `note` a project scene's `kind="generate"` job must carry. `project` is either a
    `Project` (this module's own callers already have one) or a bare id string (a caller that only
    knows the id, e.g. task 6 building scene 0's job before it has any reason to load the project
    object at all) -- both produce the identical string `parse_scene_note` will read back.
    """
    project_id = project.id if hasattr(project, "id") else str(project)
    return f"project scene {project_id} #{idx}"


def parse_scene_note(note) -> tuple[str, int] | None:
    """The `(project_id, idx)` a `scene_note`-shaped `note` carries, or `None` for anything else --
    an ordinary, non-project generate job's note, `None` itself, or free text a human happened to
    type that merely looks similar. No fuzzy matching: a job worth this bookkeeping is one this
    module (or task 6, using the same `scene_note`) actually submitted, never a guess.
    """
    if not note:
        return None
    match = _SCENE_NOTE_RE.match(note)
    if match is None:
        return None
    return match.group("project_id"), int(match.group("idx"))


# -- advance_project(): the scene chain -----------------------------------------------------------


def _scene_by_idx(scenes, idx: int) -> dict | None:
    for scene in scenes:
        if scene.get("idx") == idx:
            return scene
    return None


def _scene_wallclock_estimate_seconds(width: int, height: int, duration: float,
                                       steps: int = DEFAULT_SCENE_STEPS) -> float:
    """A rough per-scene queue-page estimate, the same fitted forward-pass/overhead model
    `web.estimate` uses (`docs/RESULTS.md`) minus its `peak_gb` half -- `peak_gb` needs
    `quant_bits(checkpoint)`, which stats an actual checkpoint directory on disk, and this module
    has no checkpoint path to give it (a project scene's job never names one explicitly, see
    `_scene_generate_args`; `h3 generate` falls back to its own default at run time). `queue.submit`
    does not care about this dict's shape either way (`Job.estimate`'s own docstring, and
    `worker.song_job_wallclock_estimate_seconds`'s precedent for "just enough of a number to show a
    human") -- duplicating the "seconds" half of the formula here, rather than importing `web.py`
    for it, keeps this module's only dependency on a checkpoint existing on disk at zero.
    """
    rows = (5.53 + 1.641 * (duration - 2.4)) * (width / 16) * (height / 16) + 81 * duration + 820
    seconds_per_forward = 5.699e-3 * rows + 2.671e-7 * rows ** 2
    diffusion = seconds_per_forward * (steps - 1)
    overhead = 36 + 7.44e-5 * width * height * duration
    return diffusion + overhead


def _scene_generate_args(scene: dict, keyframe, scenes_dir: Path) -> tuple[list[str], str]:
    """The `args`/`output_stem` pair for one scene's `kind="generate"` job -- `--width`/`--height`
    always explicit (module docstring: "`advance_project` never queries the CLI for a scene's
    canvas"), `--image` only when a keyframe exists (scene 0 with no uploaded start frame gets
    none, and is a `t2v` run exactly as the design spec's "Первая сцена: t2v" describes). The exact
    flag is `--image`, matching both `cli.py`'s own `add_argument("--image", ...)` and `app.js`'s
    `buildArgs` (`if (form.image) args.push("--image", form.image);`) -- there is no separate
    `i2v`-flavoured flag; `--mode` is never passed at all, since it is derived from `--image`'s
    presence alone (`cli.py`'s own `_add_run_flags` and `resolve_mode`).

    Review round I2: when a keyframe exists, the prompt handed to the CLI is prefixed with
    `SCENE_I2V_INSTRUCTION` -- the literal sentence `docs/h3-prompt-system.md` requires every
    `mode: i2v` run's prompt to open with, separated from `scene["prompt"]`'s own three labelled
    fields by a blank line, the same joining rule `webui/app.js`'s `buildPromptText` already uses
    for an ordinary chat turn's `instruction` field. A `t2v` scene (no keyframe -- scene 0 with no
    uploaded start frame) gets no such line: `scene["prompt"]` is passed through untouched.

    **I3 (final review): not prefixed if `scene["prompt"]` already opens with it.** The chat
    system prompt (`docs/h3-prompt-system.md`, "The first line, for image-conditioned modes")
    teaches the model to write this exact sentence itself whenever it believes a run is `mode:
    i2v` -- but a scenario scene is written before its own mode is actually known (`t2v` for scene
    0, `i2v` for every scene after it, decided here by whether an automatic keyframe exists), so a
    model that follows that instruction literally for what it assumes will be an `i2v` scene, and
    this function's own unconditional prefix, doubled the sentence at the top of the prompt. A
    doubled instruction line is not merely redundant text -- H3 reads it as *two* keyframe
    references at 0.00s, which is not what either the model or this function meant. Checked once,
    against the prompt's own leading whitespace stripped (`lstrip()`), so a prompt the model wrote
    with the sentence already in place is passed through unchanged instead of getting a second copy
    glued on top of the first.
    """
    idx = scene["idx"]
    width, height = DEFAULT_SCENE_CANVAS
    # Minor fix (review round 1): a retry of this same scene within the same wall-clock minute as
    # its previous attempt (a human's "пересчитать сцену" after `invalidate_scene_chain`) would
    # otherwise compute the exact same `--tag` -- and therefore, through `_relocate_to_job_subdir`'s
    # own minute-precision subdirectory naming and `RunSpec.output_stem`'s tag-derived filename, the
    # exact same `output_stem` -- as the attempt it is replacing. `queue.submit`'s own `_stem_taken`
    # check would then reject the retry as a conflict with a job that may not even be pending any
    # more. A short random suffix (four hex characters -- not `queue._suffix`'s own alphabet, kept
    # local rather than importing a queue-private helper) makes every attempt's own tag unique
    # regardless of timing, without needing an attempt counter on the scene's own schema.
    tag = f"scene-{idx}-{secrets.token_hex(2)}"
    prompt = scene["prompt"]
    if keyframe is not None and not prompt.lstrip().startswith(SCENE_I2V_INSTRUCTION):
        prompt = f"{SCENE_I2V_INSTRUCTION}\n\n{prompt}"
    args = ["generate", prompt,
            "--width", str(width), "--height", str(height),
            "--duration", str(scene["duration"]),
            "--tag", tag,
            "--outdir", str(scenes_dir)]
    if keyframe is not None:
        args += ["--image", str(keyframe)]
    output_stem = str(Path(scenes_dir) / f"h3-{tag}-{width}x{height}")
    return args, output_stem


def _extract_keyframe(clip_path, dest_dir: Path, source_idx: int, *, run) -> Path:
    """The automatic keyframe for the scene *after* `source_idx` -- design spec, "Клипы": "кадр из
    предпоследней секунды предыдущего клипа"; task brief: "посмотри длительность клипа ffprobe,
    время = max(0, dur-1.5)". Named after the scene it was pulled *from* (`source_idx`), not the
    one it will feed, so the file on disk documents its own provenance.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    duration = _ffprobe_duration(clip_path, run=run)
    timestamp = max(0.0, duration - KEYFRAME_LEAD_SECONDS)
    out_path = dest_dir / f"keyframe-{source_idx:03d}.png"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(clip_path),
           "-frames:v", "1", str(out_path)]
    _run_ffmpeg(cmd, run, "ffmpeg keyframe extraction")
    return out_path


#: A placeholder `job_id` for `_submit_next_scene`'s own pre-claim (see its docstring, C1) --
#: `claim_next_scene` requires a `job_id` as part of one atomic write (`status="running"` +
#: `job_id=...`), but the *real* job id does not exist until `submit()` returns, and claiming must
#: happen *before* `submit()` runs (that ordering is the fix). Overwritten with the real job id
#: once `submit()` succeeds, or with `None` (scene reset to `"pending"`) if it raises -- never left
#: on disk either way once `_submit_next_scene` returns.
_SCENE_CLAIM_PLACEHOLDER_JOB_ID = "claiming"


def _submit_next_scene(proj, scene: dict, queue_root, *, submit, run) -> dict:
    """Claim scene `scene["idx"]` on the project *first*, then submit its `kind="generate"` job --
    task brief C1 (fix round 1, 2026-08-18 review).

    **Claim before submit, not after (C1).** The previous order -- `submit()`, then
    `claim_next_scene(job.id)` -- left a window between the two calls: if something else (an
    `invalidate_scene_chain`, in practice a human's "пересчитать сцену") mutated the project in
    that window, `claim_next_scene` would claim *whatever* scene was next-ready by then, under a
    job id that was built for a *different* one, and only notice the mismatch after the write had
    already happened -- by which point the wrong scene was already corrupted (`"running"` forever
    under a job whose own `note` names some other scene index, so the worker's post-job hook would
    never revisit it). Claiming first, with `expected_idx=idx` (`Project.claim_next_scene`'s own
    C1 fix), moves the check *inside* the same lock hold the write happens under, and *before* it:
    a race is detected with nothing written at all, and this function returns `nothing_to_do`
    without ever calling `submit` for a scene that has already moved on. `_SCENE_CLAIM_PLACEHOLDER_
    JOB_ID` stands in for the not-yet-known real job id for the length of that claim; the real one
    (or a rollback to `"pending"`) is written right after `submit()` resolves, below.

    **Anything fallible between a successful claim and a successful `submit()` rolls the claim
    back to `"pending"`** (`set_scene_status(idx, "pending", job_id=None)`) rather than leaving the
    scene stuck `"running"` under the placeholder. That `try` covers keyframe extraction
    (`_extract_keyframe`, any `ffmpeg`/`ffprobe` failure -- a corrupt clip, a full disk), building
    the job's args (`_scene_generate_args`), and `submit()` itself, not `submit()` alone: fix round
    1 only wrapped `submit()`, which left a scene claimed but stranded forever (`"running"`,
    `job_id="claiming"`, invisible to `claim_next_scene`'s `"pending"`-only scan) on any
    keyframe-extraction failure in that gap -- caught by re-review and closed here (round 2,
    2026-08-18). A transient error here must not need a human's manual "пересчитать" to recover
    from. The reverse case -- a crash between `submit()` returning and the follow-up `set_scene_
    status` call recording the real job id -- is not solved here (a job would exist with no scene
    pointing at it): see the module's own report for why this narrower, pre-existing gap is
    accepted rather than closed in this round.
    """
    idx = scene["idx"]

    claimed = proj.claim_next_scene(_SCENE_CLAIM_PLACEHOLDER_JOB_ID, expected_idx=idx)
    if claimed is None:
        # Raced away: some other advance_project call (or an invalidation) already moved the chain
        # past the scene this call was asked to submit. Nothing to do -- the next real advance_
        # project call re-derives the correct next step from scratch.
        return {"action": "nothing_to_do"}

    # I2 (fix round 1, 2026-08-18 review): "живой лайфсайкл" -- stages.scenes moves to "running"
    # the moment the *first* scene is actually claimed, from whichever of "draft"/"approved" it
    # was resting in. Idempotent by construction: a scene is only ever claimed once, but the guard
    # also keeps a repeat call (there is none here, given the claim above, but future callers are
    # not this function's business to assume) from writing over "done"/"failed".
    if proj.stages.get("scenes") in ("draft", "approved"):
        proj.set_stage_status("scenes", "running")

    try:
        keyframe = None
        if idx > 0:
            prev = _scene_by_idx(proj.scenes, idx - 1)
            prev_clip = prev.get("clip_path") if prev else None
            if prev_clip:
                keyframe = _extract_keyframe(
                    prev_clip, proj.path.parent / "keyframes", idx - 1, run=run)
        else:
            # Design spec, "Клипы": "Первая сцена: t2v (или i2v, если у проекта загружен стартовый
            # кадр)". Task 1's schema has no formal `start_image` field -- reading it off
            # `as_dict()`'s pass-through for unknown top-level keys (`Project._apply`'s own
            # `self._extra`, I3, 2026-08-18 review) lets a future writer (task 6) add
            # `"start_image": "<path>"` to project.json without this module needing a schema change
            # to notice it.
            start_image = proj.as_dict().get("start_image")
            if start_image:
                keyframe = Path(start_image)

        scenes_dir = proj.path.parent / "scenes"
        args, output_stem = _scene_generate_args(scene, keyframe, scenes_dir)
        note = scene_note(proj, idx)
        width, height = DEFAULT_SCENE_CANVAS
        estimate = {"seconds": _scene_wallclock_estimate_seconds(width, height, scene["duration"])}

        job = submit(queue_root, args, note, {"output_stem": output_stem}, estimate,
                     kind=q.KIND_GENERATE)
    except Exception:
        proj.set_scene_status(idx, "pending", job_id=None)
        raise

    proj.set_scene_status(idx, "running", job_id=job.id,
                           keyframe_path=str(keyframe) if keyframe is not None else None)

    return {"action": "submitted_scene", "idx": idx, "job_id": job.id,
            "keyframe": str(keyframe) if keyframe is not None else None}


def _submit_assembly(proj, queue_root, *, submit) -> dict:
    """Submit the `kind="assemble"` job once every scene is `done` -- idempotent by
    `stages.assembly`: only fires from `"draft"` (a fresh project that has never had one queued),
    never re-fires against `"running"`/`"done"`/`"failed"` -- a failed assembly is not retried
    automatically here; that is task 6's "пересчёт"-style button, out of this function's scope.
    """
    if proj.stages.get("assembly") != "draft":
        return {"action": "nothing_to_do"}
    assembly_dir = proj.path.parent / "assembly"
    # Task 3's own `output_stem` rule (its report, "Контракты для Task 4/Task 6"): must not collide
    # with a suffix ffmpeg/assemble actually writes (`final.mp4`) -- `job-final` is not that name.
    output_stem = str(assembly_dir / "job-final")
    args = ["assemble", "--project", str(proj.path)]
    note = f"assemble project {proj.id}"
    job = submit(queue_root, args, note, {"output_stem": output_stem}, {}, kind=q.KIND_ASSEMBLE)
    proj.set_stage_status("assembly", "running")
    return {"action": "submitted_assembly", "job_id": job.id}


def advance_project(project, queue_root, outdir, *, submit=q.submit, run=subprocess.run) -> dict:
    """Progress `project` by exactly one step, and return what it did (`{"action": ...}`, for tests
    and for a caller that wants to log it -- `h3_48gb.worker`, the only real caller, currently
    ignores the return value beyond that).

    **Called by the worker after every `done`/`failed` job belonging to a project** (task brief:
    "вызывается воркером после каждого done/failed задания проекта", see `worker.py`'s
    `_handle_project_scene_result`) -- but it never trusts *which* job that was, or what it
    changed: every call re-derives the next action purely from `project.json`'s own current state
    (re-read fresh from `project.path`, not from whatever `self.scenes`/`self.stages` the caller's
    `Project` object happened to have in memory), so calling it twice in a row for the same event
    is safe by construction (see "Idempotency" below), and it is exactly what task 6 can call to
    kick off *scene 0* too -- a fresh project's scene 0 is `"pending"` with no scene before it,
    which is precisely what `next_pending_scene()` finds and `_submit_next_scene` already handles
    (`idx == 0` -> no keyframe, or the project's own `start_image` if one was uploaded).

    **Three outcomes, checked in this order, matching the task brief exactly:**

    1. **Any scene `"failed"`** -- stop. `stages["scenes"]` is set to `"failed"` (idempotently: a
       repeat call sees it already there and does not write again) and nothing further is
       submitted, ever, for this project -- task brief: "сцена failed -> проект stage failed (не
       сыпать дальше)". A human must act (task 6's retry button, out of scope here) before this
       project moves again.
    2. **A scene is ready** (`next_pending_scene()` is not `None`) -- extract the automatic
       keyframe from the previous scene's own clip (or the project's `start_image` for scene 0),
       submit the next scene's `kind="generate"` job, and claim it
       (`_submit_next_scene`).
    3. **Every scene is `"done"`** -- submit the `kind="assemble"` job (`_submit_assembly`).

    A project with no scenes at all, or one whose scenes are a mix of `"done"`/`"pending"` in a
    shape `next_pending_scene()` cannot make sense of (e.g. every scene `"pending"` behind one
    that has not been reached, `Project`'s own sequential-dependency rule -- see `next_pending_
    scene`'s docstring), returns `{"action": "nothing_to_do"}`: nothing failed, nothing is ready
    either, so there is genuinely nothing this call can do right now.

    **Idempotency (task brief: "идемпотентность повторного advance (по job_id в project.json)")**
    falls directly out of `Project`'s own locked mutators, not out of anything this function checks
    by hand: a scene `_submit_next_scene` already claimed carries a `job_id` and a `"running"`
    status, so `next_pending_scene()`'s sequential walk stops *at* it on a repeat call (it is
    neither `"done"` nor `"pending"`) and returns `None` for everything after -- no second submit.
    The assembly branch is idempotent the same way, gated on `stages["assembly"] != "draft"`
    (`_submit_assembly`'s own docstring). Neither needs its own repeat-call bookkeeping because the
    thing being checked -- the scene's/stage's own status -- *is* the record of "already
    submitted".

    `outdir` is accepted, per the task brief's signature, but is **not currently load-bearing**:
    every path this function and `run()` use (`scenes/`, `keyframes/`, `assembly/`) is derived from
    `project.path.parent` instead, which is self-consistent regardless of what `outdir` says and
    cannot point at the wrong project's directory if the two ever disagree (a moved project, a
    caller passing a stale `outdir`). See the module's own report for the alternative considered
    (deriving those paths from `outdir` instead) and why it was not the safer choice.
    """
    proj = project_module.load_project(project.path)

    if any(scene.get("status") == "failed" for scene in proj.scenes):
        if proj.stages.get("scenes") != "failed":
            proj.set_stage_status("scenes", "failed")
        return {"action": "stopped_on_failed_scene"}

    next_scene = proj.next_pending_scene()
    if next_scene is not None:
        return _submit_next_scene(proj, next_scene, queue_root, submit=submit, run=run)

    if proj.scenes and all(scene.get("status") == "done" for scene in proj.scenes):
        # I2 (fix round 1, 2026-08-18 review): "живой лайфсайкл" -- stages.scenes moves to "done"
        # once every scene actually is, and it does so *before* the assembly job is submitted (not
        # after), so a caller reading the project while the assemble job is in flight never sees
        # scenes still "running" next to an assembly already underway.
        if proj.stages.get("scenes") != "done":
            proj.set_stage_status("scenes", "done")
        return _submit_assembly(proj, queue_root, submit=submit)

    return {"action": "nothing_to_do"}
