"""Final assembly (`run`) and the scene chain (`advance_project`) -- task 4, "Проекты".

**Every `ffmpeg`/`ffprobe` call is mocked through the `run` seam**, mirroring `test_songrun.py`'s
own discipline, with one deliberate exception: `test_run_concats_pads_and_replaces_audio_on_a_
real_ffmpeg_synthesis` runs a real `ffmpeg` against `lavfi`-synthesized clips and a sine "track" --
the one test task 4's brief asks for by name ("один реальный тест сборки на lavfi-синтетике").
Everything else here substitutes a fake `run` (a `spawn` for `submit`), the same way
`test_worker.py` never starts a real generation.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import project as project_module
from h3_48gb import queue as q

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -- fakes -----------------------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    """Records every command it is asked to run, and answers with a scripted result keyed off the
    command's own first-two-tokens shape (`ffprobe`/`ffmpeg ... -sseof`/anything else). Duration
    answers are queued per-call (`ffprobe_durations`), in the order `_ffprobe_duration` is expected
    to call them, so a test can script "video came up short, then padded video matches" without
    needing a real ffmpeg to measure anything.
    """

    def __init__(self, ffprobe_durations=None):
        self.calls: list[list[str]] = []
        self._ffprobe_durations = list(ffprobe_durations or [])

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(list(cmd))
        if cmd[0] == "ffprobe":
            duration = self._ffprobe_durations.pop(0) if self._ffprobe_durations else 0.0
            return _FakeResult(0, stdout=f"{duration}\n")
        return _FakeResult(0, stdout="", stderr="")


def _make_scene(idx, *, status="done", clip_path=None, prompt="a scene", duration=5.0,
                 job_id=None, keyframe_path=None):
    return {"idx": idx, "prompt": prompt, "duration": duration, "status": status,
            "job_id": job_id, "clip_path": clip_path, "keyframe_path": keyframe_path}


def _make_project(tmp_path, kind="clip", *, audio_mode=None, scenes=None, track=None,
                   title="Тестовый проект"):
    proj = project_module.create_project(tmp_path / "out", kind, title)
    if scenes is not None:
        proj.scenes = scenes
    if track is not None:
        proj.track = {**proj.track, **track}
    if audio_mode is not None:
        proj.assembly = {**proj.assembly, "audio_mode": audio_mode}
    proj.save()
    return proj


# -- run(): guard rails ------------------------------------------------------------------------


def test_run_rejects_a_song_kind_project(tmp_path):
    proj = _make_project(tmp_path, "song", scenes=[])
    with pytest.raises(assemble.AssembleError, match="song"):
        assemble.run(proj.path, run=_FakeRun())


def test_run_rejects_a_project_with_no_scenes(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[])
    with pytest.raises(assemble.AssembleError, match="no scenes"):
        assemble.run(proj.path, run=_FakeRun())


def test_run_rejects_a_project_with_undone_scenes(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, status="pending"),
    ])
    with pytest.raises(assemble.AssembleError, match="not yet done"):
        assemble.run(proj.path, run=_FakeRun())


def test_run_rejects_a_done_scene_missing_its_clip_path(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="done", clip_path=None)])
    with pytest.raises(assemble.AssembleError, match="clip_path"):
        assemble.run(proj.path, run=_FakeRun())


def test_run_rejects_song_audio_mode_with_no_mastered_track(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    with pytest.raises(assemble.AssembleError, match="mastered track"):
        assemble.run(proj.path, run=_FakeRun())


# -- run(): "clips" audio mode -- no track needed, no duration validation ------------------------


def test_run_clips_mode_concats_with_audio_kept_and_no_duration_check(tmp_path):
    proj = _make_project(tmp_path, "video", audio_mode="clips", scenes=[
        _make_scene(0, clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, clip_path=str(tmp_path / "b.mp4")),
    ])
    fake = _FakeRun()

    final = assemble.run(proj.path, run=fake)

    assert final == proj.path.parent / "assembly" / "final.mp4"
    # Exactly one concat call, keeping both streams (no "-an", no "-vn").
    concat_calls = [c for c in fake.calls if "-f" in c and "concat" in c]
    assert len(concat_calls) == 1
    assert "-an" not in concat_calls[0]
    assert "-vn" not in concat_calls[0]
    reloaded = project_module.load_project(proj.path)
    assert reloaded.assembly["final_path"] == str(final)
    assert reloaded.stages["assembly"] == "done"


# -- run(): "song" audio mode -- duration validation + freeze-frame padding ----------------------


def test_run_song_mode_pads_a_short_video_with_a_freeze_frame(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    # First ffprobe: the raw concat's own duration (short); second: after padding, matches track.
    fake = _FakeRun(ffprobe_durations=[8.0, 10.0])

    final = assemble.run(proj.path, run=fake)

    assert final == proj.path.parent / "assembly" / "final.mp4"
    freeze_calls = [c for c in fake.calls if "-loop" in c]
    assert len(freeze_calls) == 1, "a short video must be padded exactly once"
    assert "-t" in freeze_calls[0]
    pad_seconds = float(freeze_calls[0][freeze_calls[0].index("-t") + 1])
    assert pad_seconds == pytest.approx(2.0)
    mux_calls = [c for c in fake.calls if "-map" in c and "1:a:0" in c]
    assert len(mux_calls) == 1
    assert "-shortest" not in mux_calls[0], "a song's own duration must never be silently truncated"


def test_run_song_mode_skips_padding_when_video_already_covers_the_track(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[10.1])

    assemble.run(proj.path, run=fake)

    freeze_calls = [c for c in fake.calls if "-loop" in c]
    assert freeze_calls == [], "already within tolerance -- no freeze-frame call needed"


def test_run_song_mode_raises_when_the_gap_survives_padding(tmp_path):
    """A video that is still off by more than `DURATION_TOLERANCE_SECONDS` after padding is an
    assembly error (task brief: "расхождение сверх допуска = ошибка сборки"), not a silently
    shipped mismatch.
    """
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[8.0, 8.9])  # padding math itself came up short

    with pytest.raises(assemble.AssembleError, match="tolerance"):
        assemble.run(proj.path, run=fake)


def test_run_song_mode_raises_when_video_is_already_too_long(tmp_path):
    """Padding never trims -- a video that overshoots the track by more than the tolerance is an
    error, not silently accepted, because nothing here is allowed to touch the audio to compensate.
    """
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[11.0])

    with pytest.raises(assemble.AssembleError, match="tolerance"):
        assemble.run(proj.path, run=fake)

    assert not any("-loop" in c for c in fake.calls), "an overshoot must never be padded"


def test_run_never_passes_shortest_to_any_ffmpeg_call(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[8.0, 10.0])

    assemble.run(proj.path, run=fake)

    for cmd in fake.calls:
        assert "-shortest" not in cmd


# -- run(): "mix" audio mode -----------------------------------------------------------------------


def test_run_mix_mode_mixes_clip_audio_at_minus_18db_with_the_track(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="mix",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[10.0])

    assemble.run(proj.path, run=fake)

    mix_calls = [c for c in fake.calls if "-filter_complex" in c]
    assert len(mix_calls) == 1
    filter_complex = mix_calls[0][mix_calls[0].index("-filter_complex") + 1]
    assert "volume=-18dB" in filter_complex
    assert "amix=inputs=2:duration=longest" in filter_complex


# -- run(): direct-cut concat only -- no scene-transition filters anywhere -----------------------


def test_run_never_uses_a_crossfade_or_transition_filter(tmp_path):
    proj = _make_project(tmp_path, "video", audio_mode="clips", scenes=[
        _make_scene(0, clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, clip_path=str(tmp_path / "b.mp4")),
    ])
    fake = _FakeRun()

    assemble.run(proj.path, run=fake)

    for cmd in fake.calls:
        joined = " ".join(cmd)
        assert "xfade" not in joined
        assert "fade=" not in joined


# -- scene_note / parse_scene_note -----------------------------------------------------------------


def test_scene_note_round_trips_through_parse_scene_note(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[])
    note = assemble.scene_note(proj, 3)
    assert assemble.parse_scene_note(note) == (proj.id, 3)


def test_scene_note_accepts_a_bare_id_string():
    note = assemble.scene_note("20260818-1200-my-video", 0)
    assert assemble.parse_scene_note(note) == ("20260818-1200-my-video", 0)


@pytest.mark.parametrize("note", [None, "", "just a note", "project scene", "project scene abc"])
def test_parse_scene_note_returns_none_for_anything_else(note):
    assert assemble.parse_scene_note(note) is None


# -- advance_project(): the scene chain -------------------------------------------------------------


class _RecordingSubmit:
    """A `submit` fake that hands back a real-shaped `queue.Job`-like object (only `.id` is ever
    read by `advance_project`) and records every call it saw.
    """

    def __init__(self):
        self.calls = []
        self._n = 0

    def __call__(self, queue_root, args, note, dry_run_report, estimate, *, kind):
        self._n += 1
        self.calls.append({"queue_root": queue_root, "args": list(args), "note": note,
                            "dry_run_report": dict(dry_run_report), "estimate": dict(estimate),
                            "kind": kind})

        class _Job:
            id = f"fake-job-{self._n}"

        return _Job()


def test_advance_project_submits_scene_zero_as_t2v_with_no_image_flag(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "submitted_scene"
    assert result["idx"] == 0
    assert len(submit.calls) == 1
    args = submit.calls[0]["args"]
    assert args[0] == "generate"
    assert "--image" not in args
    assert submit.calls[0]["kind"] == q.KIND_GENERATE
    assert submit.calls[0]["note"] == assemble.scene_note(proj, 0)
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "running"
    assert reloaded.scenes[0]["job_id"] == "fake-job-1"


def test_advance_project_uses_a_start_image_for_scene_zero_when_the_project_has_one(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])
    data = proj.as_dict()
    data["start_image"] = "/tmp/uploaded-start.png"
    q.write_json_durably(proj.path, data)
    proj = project_module.load_project(proj.path)
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "submitted_scene"
    args = submit.calls[0]["args"]
    assert "--image" in args
    assert args[args.index("--image") + 1] == "/tmp/uploaded-start.png"


def test_advance_project_extracts_a_keyframe_and_submits_the_next_scene_with_image(tmp_path):
    clip = tmp_path / "scene0.mp4"
    clip.write_bytes(b"fake mp4")
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip)),
        _make_scene(1, status="pending"),
    ])
    submit = _RecordingSubmit()
    fake_run = _FakeRun(ffprobe_durations=[6.0])  # clip duration -- keyframe at max(0, 6-1.5)=4.5

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=fake_run)

    assert result["action"] == "submitted_scene"
    assert result["idx"] == 1
    args = submit.calls[0]["args"]
    assert "--image" in args
    keyframe_path = args[args.index("--image") + 1]
    assert Path(keyframe_path).name == "keyframe-000.png"
    keyframe_calls = [c for c in fake_run.calls if "-ss" in c]
    assert len(keyframe_calls) == 1
    assert keyframe_calls[0][keyframe_calls[0].index("-ss") + 1] == "4.500"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[1]["keyframe_path"] == keyframe_path


def test_advance_project_keyframe_timestamp_floors_at_zero_for_a_short_clip(tmp_path):
    clip = tmp_path / "scene0.mp4"
    clip.write_bytes(b"fake mp4")
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip)),
        _make_scene(1, status="pending"),
    ])
    submit = _RecordingSubmit()
    fake_run = _FakeRun(ffprobe_durations=[0.8])  # shorter than KEYFRAME_LEAD_SECONDS

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=fake_run)

    keyframe_calls = [c for c in fake_run.calls if "-ss" in c]
    assert keyframe_calls[0][keyframe_calls[0].index("-ss") + 1] == "0.000"


def test_advance_project_stops_and_marks_scenes_failed_without_submitting_anything(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, status="failed"),
        _make_scene(2, status="pending"),
    ])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "stopped_on_failed_scene"
    assert submit.calls == []
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["scenes"] == "failed"


def test_advance_project_does_not_resubmit_once_a_scene_is_already_running(tmp_path):
    """Idempotency (task brief: "по job_id в project.json"): calling `advance_project` twice for
    the same event must not double-submit -- the second call sees the scene `claim_next_scene`
    already claimed on the first call and finds nothing else ready.
    """
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])
    submit = _RecordingSubmit()

    first = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                      run=_FakeRun())
    second = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert first["action"] == "submitted_scene"
    assert second["action"] == "nothing_to_do"
    assert len(submit.calls) == 1


def test_advance_project_submits_assembly_once_every_scene_is_done(tmp_path):
    proj = _make_project(tmp_path, "video", audio_mode="clips", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, status="done", clip_path=str(tmp_path / "b.mp4")),
    ])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "submitted_assembly"
    assert len(submit.calls) == 1
    call = submit.calls[0]
    assert call["args"] == ["assemble", "--project", str(proj.path)]
    assert call["kind"] == q.KIND_ASSEMBLE
    assert call["dry_run_report"]["output_stem"] == str(proj.path.parent / "assembly" / "job-final")
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["assembly"] == "running"


def test_advance_project_does_not_resubmit_assembly_twice(tmp_path):
    proj = _make_project(tmp_path, "video", audio_mode="clips", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
    ])
    submit = _RecordingSubmit()

    first = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                      run=_FakeRun())
    second = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert first["action"] == "submitted_assembly"
    assert second["action"] == "nothing_to_do"
    assert len(submit.calls) == 1


def test_advance_project_returns_nothing_to_do_for_a_project_with_no_scenes(tmp_path):
    proj = _make_project(tmp_path, "song", scenes=[])
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "nothing_to_do"
    assert submit.calls == []


def test_advance_project_re_reads_the_project_from_disk_rather_than_trusting_the_caller(tmp_path):
    """A caller may hand `advance_project` a `Project` object that is stale relative to disk (the
    worker just wrote a scene's outcome through its *own* `Project` instance) -- `advance_project`
    must re-load from `project.path` rather than act on `project.scenes` as given.
    """
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])
    stale = project_module.load_project(proj.path)
    # Mutate on disk through a second, independent Project object.
    fresh = project_module.load_project(proj.path)
    fresh.set_scene_status(0, "failed")
    submit = _RecordingSubmit()

    result = assemble.advance_project(stale, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "stopped_on_failed_scene"
    assert submit.calls == []


# -- run(): the one real-ffmpeg integration test (task brief, verbatim) --------------------------


def _make_lavfi_clip(path: Path, duration: float, color: str) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"color=c={color}:size=64x64:rate=24:duration={duration}",
           "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_sine_mp3(path: Path, duration: float) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={duration}", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _real_ffprobe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def test_run_concats_pads_and_replaces_audio_on_a_real_ffmpeg_synthesis(tmp_path):
    """The brief's own required real test: two tiny lavfi clips (1 s each, so the concatenated
    video is short against a longer sine "track") get concatenated, freeze-frame padded to the
    track's duration, and muxed with the track audio -- verified end to end with real `ffmpeg`/
    `ffprobe`, no mocks.
    """
    clip0 = tmp_path / "clip0.mp4"
    clip1 = tmp_path / "clip1.mp4"
    _make_lavfi_clip(clip0, 1.0, "red")
    _make_lavfi_clip(clip1, 1.0, "blue")
    track_mp3 = tmp_path / "track.mp3"
    _make_sine_mp3(track_mp3, 2.6)  # concatenated video is ~2.0s -- track is longer by ~0.6s

    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(track_mp3),
                                 "duration": _real_ffprobe_duration(track_mp3)},
                          scenes=[
                              _make_scene(0, clip_path=str(clip0)),
                              _make_scene(1, clip_path=str(clip1)),
                          ])

    final = assemble.run(proj.path, run=subprocess.run)

    assert final.is_file()
    assert final.stat().st_size > 0
    track_duration = _real_ffprobe_duration(track_mp3)
    final_duration = _real_ffprobe_duration(final)
    assert final_duration == pytest.approx(track_duration, abs=assemble.DURATION_TOLERANCE_SECONDS)
    reloaded = project_module.load_project(proj.path)
    assert reloaded.assembly["final_path"] == str(final)
    assert reloaded.stages["assembly"] == "done"


# -- no-mlx discipline --------------------------------------------------------------------------


def test_assemble_module_does_not_import_mlx():
    """`h3_48gb.assemble` runs inside the worker process (mirrors `h3_48gb.songrun`/
    `h3_48gb.worker`'s own "no mlx, ever" rule -- see the module docstring). Run in a subprocess so
    this test session's own earlier imports cannot hide a leak -- mirrors
    `test_worker_module_does_not_import_mlx`/`test_songrun_module_does_not_import_mlx`.
    """
    code = "import sys; import h3_48gb.assemble; print('mlx' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.assemble must not import mlx"
