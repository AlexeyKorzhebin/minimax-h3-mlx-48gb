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

    The audio-stream preflight probe (`_has_audio_stream`, `-select_streams a`) is answered
    separately from that queue -- "every clip has audio" by default -- rather than treated as one
    more duration call: it asks a different question, and making it share the same queue would
    force every `"mix"`-mode test to insert an extra, semantically irrelevant value just to keep
    the real duration answers lined up. `_NoClipAudioFakeRun` below overrides this one answer for
    the tests that actually want a clip found missing its audio track.
    """

    def __init__(self, ffprobe_durations=None):
        self.calls: list[list[str]] = []
        self._ffprobe_durations = list(ffprobe_durations or [])

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(list(cmd))
        if cmd[0] == "ffprobe" and "-select_streams" in cmd:
            return _FakeResult(0, stdout="0\n")  # "has an audio stream", by default
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
    # First ffprobe: the raw concat's own duration (short); second: after padding, matches track;
    # third: final.mp4 itself, post-mux (I4) -- also matches, the mux introduces no drift here.
    fake = _FakeRun(ffprobe_durations=[8.0, 10.0, 10.0])

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
    # First ffprobe: the raw concat, already within tolerance; second: final.mp4 itself (I4).
    fake = _FakeRun(ffprobe_durations=[10.1, 10.1])

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
    fake = _FakeRun(ffprobe_durations=[8.0, 10.0, 10.0])

    assemble.run(proj.path, run=fake)

    for cmd in fake.calls:
        assert "-shortest" not in cmd


# -- run(): I4 (fix round 1, 2026-08-18 review) -- final.mp4 itself is checked too ----------------


def test_run_song_mode_raises_when_final_mp4_itself_drifts_from_the_track(tmp_path):
    """The pre-mux, video-only concat can measure exactly on target and still not guarantee
    `final.mp4` itself lands within tolerance -- the mux step is a second place a drift could sneak
    in (module docstring, I4). This scripts exactly that: the video-only concat matches the track
    perfectly (no padding), but `final.mp4` itself -- the next `ffprobe` call, after the mux --
    measures off by more than `DURATION_TOLERANCE_SECONDS`.
    """
    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    fake = _FakeRun(ffprobe_durations=[10.0, 12.0])

    with pytest.raises(assemble.AssembleError, match="final.mp4"):
        assemble.run(proj.path, run=fake)


# -- run(): "mix" audio mode -----------------------------------------------------------------------


def test_run_mix_mode_mixes_clip_audio_at_minus_18db_with_the_track(tmp_path):
    proj = _make_project(tmp_path, "clip", audio_mode="mix",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(tmp_path / "a.mp4"))])
    # First ffprobe: the raw concat, exactly on target; second: final.mp4 itself (I4).
    fake = _FakeRun(ffprobe_durations=[10.0, 10.0])

    assemble.run(proj.path, run=fake)

    mix_calls = [c for c in fake.calls if "-filter_complex" in c]
    assert len(mix_calls) == 1
    filter_complex = mix_calls[0][mix_calls[0].index("-filter_complex") + 1]
    assert "volume=-18dB" in filter_complex
    assert "amix=inputs=2:duration=longest" in filter_complex


class _NoClipAudioFakeRun(_FakeRun):
    """`_FakeRun` plus an override for the audio-stream preflight probe (`-select_streams a`):
    every clip path in `no_audio_clips` answers "no audio stream" (empty stdout) instead of the
    base class's default "has one" -- everything else (durations, ffmpeg calls) is unchanged.
    """

    def __init__(self, *, no_audio_clips, ffprobe_durations=None):
        super().__init__(ffprobe_durations=ffprobe_durations)
        self._no_audio_clips = {str(p) for p in no_audio_clips}

    def __call__(self, cmd, capture_output=True, text=True):
        if cmd[0] == "ffprobe" and "-select_streams" in cmd and cmd[-1] in self._no_audio_clips:
            self.calls.append(list(cmd))
            return _FakeResult(0, stdout="")
        return super().__call__(cmd, capture_output=capture_output, text=text)


def test_run_mix_mode_raises_a_readable_error_when_a_clip_has_no_audio_stream(tmp_path):
    """Minor fix (review round 1): a clip missing its own audio track must fail `run` with a
    message a human can read (which clip, and why), not an opaque ffmpeg stderr blob from `amix`
    choking on a stream that was never there.
    """
    clip_no_audio = tmp_path / "a.mp4"
    proj = _make_project(tmp_path, "clip", audio_mode="mix",
                          track={"mastered_mp3": str(tmp_path / "song.mastered.mp3"),
                                 "duration": 10.0},
                          scenes=[_make_scene(0, clip_path=str(clip_no_audio))])
    fake = _NoClipAudioFakeRun(no_audio_clips=[clip_no_audio], ffprobe_durations=[10.0])

    with pytest.raises(assemble.AssembleError, match="audio track"):
        assemble.run(proj.path, run=fake)


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


def test_scene_note_format_is_frozen():
    """Minor fix (review round 1): the exact string, not just a round trip -- `h3_48gb.worker`'s
    post-job hook and a future task 6 both depend on this literal shape, so a refactor that changes
    it (even one that still round-trips through `parse_scene_note` fine) must fail a test loudly.
    """
    assert assemble.scene_note("p", 3) == "project scene p #3"


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


# -- _scene_generate_args: retry-safe output_stem (minor fix, review round 1) --------------------


def test_scene_generate_args_produces_a_different_output_stem_on_each_call(tmp_path):
    """A retry within the same wall-clock minute as a scene's previous attempt must not compute
    the same `output_stem`/`--tag` as the attempt it replaces -- `queue.submit`'s own `_stem_taken`
    conflict check would otherwise reject the retry outright, since `_relocate_to_job_subdir`'s own
    subdirectory naming only has minute precision. `_scene_generate_args` is called fresh for every
    `_submit_next_scene` attempt, with no attempt-count field on the scene's own schema to key off,
    so it must vary its own output on its own, run to run.
    """
    scene = _make_scene(0, status="pending")

    args1, stem1 = assemble._scene_generate_args(scene, None, tmp_path / "scenes")
    args2, stem2 = assemble._scene_generate_args(scene, None, tmp_path / "scenes")

    assert stem1 != stem2
    assert args1[args1.index("--tag") + 1] != args2[args2.index("--tag") + 1]


# -- _scene_generate_args: the i2v instruction line (review round, I2) ---------------------------


def test_scene_generate_args_prepends_the_i2v_instruction_when_a_keyframe_is_attached():
    """Review I2: a scene submitted with `--image` runs as `mode: i2v`, and
    `docs/h3-prompt-system.md`'s own format ("The first line, for image-conditioned modes")
    requires that run's prompt to *open* with a specific literal sentence -- `scene["prompt"]`
    itself never carries it (Task 5's own report, "scenario mode": the model is told never to
    write `instruction` into a scene, the pipeline must add it once it knows the scene is i2v).
    Without this, a keyframed scene's clip loses the one sentence that tells H3 the picture is a
    real reference and not just an arbitrary conditioning image -- the same gap `buildPromptText`
    (`webui/app.js`) closes for an ordinary chat turn's own `prompt.instruction` field.
    """
    scene = _make_scene(1, prompt="integrated_multimodal_description: a fox runs\n\n"
                                   "overall_soundscape: wind\n\nnon_diegetic_music: N/A")

    args, _ = assemble._scene_generate_args(scene, "/tmp/keyframe-000.png", Path("/tmp/scenes"))

    prompt_arg = args[1]
    assert prompt_arg.startswith(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n")
    assert scene["prompt"] in prompt_arg


def test_scene_generate_args_leaves_a_t2v_scenes_prompt_untouched():
    """The counterpart: a scene with no keyframe (`t2v`, scene 0 with no `start_image`) must not
    grow an i2v instruction line -- that sentence is a lie about a run that has no reference image
    at all.
    """
    scene = _make_scene(0, prompt="integrated_multimodal_description: a fox runs\n\n"
                                   "overall_soundscape: wind\n\nnon_diegetic_music: N/A")

    args, _ = assemble._scene_generate_args(scene, None, Path("/tmp/scenes"))

    prompt_arg = args[1]
    assert prompt_arg == scene["prompt"]
    assert "is fully referenced" not in prompt_arg


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


# -- C1 (fix round 1, 2026-08-18 review): claim before submit -----------------------------------


def test_advance_project_does_not_corrupt_a_scene_when_invalidated_between_decision_and_claim(
        tmp_path, monkeypatch):
    """The exact race the review reproduced: an invalidation (a human's "пересчитать сцену") lands
    between `advance_project`'s own `next_pending_scene()` preview (which already saw scene 1 as
    next) and the moment `_submit_next_scene` actually claims it. Before the fix, `submit()` ran
    first and `claim_next_scene` (no `expected_idx`) claimed *whatever* scene was next-ready by
    then -- scene 0, freshly reset `pending` by the injected invalidation -- under the *wrong*
    job's id (one built for scene 1, note "#1"), and only found out afterward: scene 0 was already
    stuck `"running"` forever, since the worker only ever revisits the scene index a job's own
    `note` names. After the fix, the claim happens first, `expected_idx=1` refuses inside the lock
    before any write, and `submit` is never even called for the race this call lost.
    """
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
        _make_scene(1, status="pending"),
    ])
    real_claim = project_module.Project.claim_next_scene

    def racy_claim(self, job_id, *, expected_idx=None):
        # Simulate a concurrent invalidation landing exactly between advance_project's own
        # next_pending_scene() preview (already stale by the time this runs) and this claim.
        self.invalidate_scene_chain(0)
        return real_claim(self, job_id, expected_idx=expected_idx)

    monkeypatch.setattr(project_module.Project, "claim_next_scene", racy_claim)
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun(ffprobe_durations=[6.0]))

    assert submit.calls == [], "the race must be caught before any job is ever submitted"
    assert result["action"] == "nothing_to_do"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "pending", "scene 0 must not be left corrupted"
    assert reloaded.scenes[0]["job_id"] is None


def test_advance_project_rolls_a_scene_back_to_pending_when_keyframe_extraction_raises(tmp_path):
    """Round 2 (2026-08-18 re-review) regression: fix round 1's C1 moved the claim *before*
    keyframe extraction and job-arg construction, but only wrapped `submit()` itself in the
    rollback `try`/`except`. A failing `ffmpeg` call inside `_extract_keyframe` (a corrupt clip,
    a full disk, ...) between that claim and `submit()` left the scene claimed -- `"running"`,
    `job_id="claiming"` -- forever: `claim_next_scene` only ever looks at `"pending"` scenes, so
    nothing would ever retry it and no human "пересчитать" button could reach it either (the scene
    was never marked `"failed"`). This must roll back to `"pending"` exactly like a `submit()`
    failure does.
    """
    clip = tmp_path / "scene0.mp4"
    clip.write_bytes(b"fake mp4")
    proj = _make_project(tmp_path, "video", scenes=[
        _make_scene(0, status="done", clip_path=str(clip)),
        _make_scene(1, status="pending"),
    ])
    submit = _RecordingSubmit()

    def failing_run(cmd, capture_output=True, text=True):
        return _FakeResult(1, stdout="", stderr="ffmpeg: no such file or directory")

    with pytest.raises(assemble.AssembleError, match="ffprobe duration"):
        assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                  run=failing_run)

    assert submit.calls == [], "submit must never be reached once keyframe extraction fails"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[1]["status"] == "pending"
    assert reloaded.scenes[1]["job_id"] is None


def test_advance_project_rolls_a_scene_back_to_pending_when_submit_itself_raises(tmp_path):
    """A `submit()` that raises (a queue-side failure, `OutputStemConflict`, ...) must not leave
    the scene stuck `"running"` under the claim's own placeholder job id -- it must be rolled back
    to `"pending"` so a later retry (or a human's "пересчитать") is not required to recover from a
    transient queue error.
    """
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])

    def failing_submit(*a, **kw):
        raise RuntimeError("queue is on fire")

    with pytest.raises(RuntimeError, match="queue is on fire"):
        assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=failing_submit,
                                  run=_FakeRun())

    reloaded = project_module.load_project(proj.path)
    assert reloaded.scenes[0]["status"] == "pending"
    assert reloaded.scenes[0]["job_id"] is None


# -- I2 (fix round 1, 2026-08-18 review): stages.scenes' own live lifecycle ----------------------


def test_advance_project_marks_scenes_stage_running_on_the_first_scene_submit(tmp_path):
    proj = _make_project(tmp_path, "video", scenes=[_make_scene(0, status="pending")])
    assert proj.stages["scenes"] == "draft"
    submit = _RecordingSubmit()

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                              run=_FakeRun())

    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["scenes"] == "running"


def test_advance_project_marks_scenes_stage_done_before_submitting_assembly(tmp_path):
    proj = _make_project(tmp_path, "video", audio_mode="clips", scenes=[
        _make_scene(0, status="done", clip_path=str(tmp_path / "a.mp4")),
    ])
    proj.set_stage_status("scenes", "running")
    submit = _RecordingSubmit()

    result = assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out", submit=submit,
                                       run=_FakeRun())

    assert result["action"] == "submitted_assembly"
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["scenes"] == "done"


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
    # Minor fix (review round 1): a successful assembly cleans up its own intermediates -- the
    # video-only concat, its concat-list file, and the freeze-frame pad workdir -- keeping only
    # final.mp4 itself.
    assembly_dir = proj.path.parent / "assembly"
    assert not (assembly_dir / "concat_video.mp4").exists()
    assert not (assembly_dir / "concat_video.concat.txt").exists()
    assert not (assembly_dir / "pad").exists()


def test_run_leaves_intermediate_files_in_place_when_assembly_fails(tmp_path):
    """Minor fix (review round 1): the flip side of the cleanup above -- a *failed* assembly must
    leave every intermediate it managed to write on disk, for a human to inspect. A track far
    shorter than the video (no amount of freeze-frame padding could ever close a *negative* gap --
    padding never trims) fails the duration check right after the video-only concat is written,
    which is exactly the file this asserts survives.
    """
    clip0 = tmp_path / "clip0.mp4"
    _make_lavfi_clip(clip0, 2.0, "green")
    track_mp3 = tmp_path / "short_track.mp3"
    _make_sine_mp3(track_mp3, 0.3)

    proj = _make_project(tmp_path, "clip", audio_mode="song",
                          track={"mastered_mp3": str(track_mp3),
                                 "duration": _real_ffprobe_duration(track_mp3)},
                          scenes=[_make_scene(0, clip_path=str(clip0))])

    with pytest.raises(assemble.AssembleError, match="tolerance"):
        assemble.run(proj.path, run=subprocess.run)

    assembly_dir = proj.path.parent / "assembly"
    assert (assembly_dir / "concat_video.mp4").is_file(), (
        "the video-only concat must survive a failed assembly for diagnosis")
    reloaded = project_module.load_project(proj.path)
    assert reloaded.stages["assembly"] != "done"


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
