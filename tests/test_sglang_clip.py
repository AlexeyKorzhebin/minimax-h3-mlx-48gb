"""A clip on an imported track, no Whisper (spec §3.3.10, §4.3, §4.1.5)."""
import json
import subprocess
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb import web
from test_web import _call, _pending, _serve


def _mp3(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame", str(path)],
                   check=True)
    return path


def _probe(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _imported_clip(outdir, seconds):
    mp3 = _mp3(outdir / "uploads" / "song.mp3", seconds)
    proj = p.create_project(outdir, "clip", "Clip")
    proj.track["source"] = "import"
    proj.track["mp3"] = str(mp3)
    proj.stages["script"] = "awaiting_approval"
    proj.save()
    return proj, mp3


def test_approving_an_imported_track_measures_it_and_queues_no_song_job(live):
    proj, mp3 = _imported_clip(live.outdir, 12.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    reloaded = p.load_project(proj.path)
    assert abs(reloaded.track["duration"] - _probe(mp3)) < 0.01
    assert (reloaded.track["mastered_mp3"], reloaded.track["status"]) == (str(mp3), "approved")
    assert (reloaded.stages["script"], reloaded.stages["track"]) == ("approved", "approved")
    assert _pending(live) == []


def test_import_shorter_than_one_scene_is_refused(live):
    """Review Focus 5."""
    proj, _ = _imported_clip(live.outdir, 4.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert (status, body["error"]["code"]) == (400, "track_too_short")
    assert p.load_project(proj.path).stages["script"] == "awaiting_approval"


def test_on_mlx_import_still_queues_the_song_job(live, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "mlx")
    proj, _ = _imported_clip(live.outdir, 6.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/script", {})
    assert status == 200, body
    assert [job.kind for job in _pending(live)] == ["song"]


def test_scenario_context_without_lyrics_and_with_references():
    assert web._scenario_context("", [], "warm summer song", 13.0,
                                 references_block="## Reference tags\n@alice (person): a woman") == (
        "## Context\nmode: clip_scenario\nduration: 13 s\n\ncaption:\nwarm summer song\n\n"
        "no lyrics and no transcript: the track is an imported recording; build the scenes from "
        "the caption, the mood and the duration alone\n\n"
        "## Reference tags\n@alice (person): a woman\n\nWrite the clip's scenario now.")


def test_scenario_context_with_lyrics_is_unchanged_without_references():
    assert web._scenario_context("la la", [], "c", 10.0) == (
        "## Context\nmode: clip_scenario\nduration: 10 s\n\ncaption:\nc\n\nlyrics:\nla la\n\n"
        "Write the clip's scenario now.")


def _clip_with_duration(outdir, seconds, caption="warm summer song"):
    proj = p.create_project(outdir, "clip", "Clip")
    proj.track.update({"source": "import", "duration": seconds, "caption": caption,
                       "status": "approved"})
    proj.stages.update({"script": "approved", "track": "approved"})
    proj.save()
    return proj


def test_generate_without_lyrics_reaches_the_llm_on_sglang(live, monkeypatch):
    (live.outdir / "uploads").mkdir(exist_ok=True)
    (live.outdir / "uploads" / "f.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = lib.create_card(live.outdir, tag="@alice", kind="person", description="a woman",
                           assets=[live.outdir / "uploads" / "f.png"])
    proj = _clip_with_duration(live.outdir, 13.0)
    proj.set_references([{"tag": "@alice", "version": 1}])
    seen = []
    turn = {"reply": "ok", "scenario": {"style_block": "sunlit", "sections": [
        # `fresh_start` omitted, not null: `_scenario_turn_to_scenes` adds it only when present,
        # and `_typed_scenario_scene` refuses an explicit None (web.py, its own comment)
        {"tag": "a", "start": 0.0, "end": 6.5, "scene": {"prompt": "@alice dances", "duration": 6.5,
                                                         "state_in": "s", "state_out": "t"}},
        {"tag": "b", "start": 6.5, "end": 13.0, "scene": {"prompt": "@alice waves", "duration": 6.5,
                                                          "state_in": "", "state_out": "u"}}]}}
    monkeypatch.setattr(web.provider, "load_providers", lambda outdir: {
        "active": "x", "providers": {"x": {"type": "openai", "available": True,
                                           "base_url": "http://127.0.0.1:9", "model": "m"}}})
    monkeypatch.setattr(web.provider, "load_env", lambda outdir: {})
    monkeypatch.setattr(web.provider, "chat_scenario",
                        lambda cfg, env, messages: seen.append(messages) or turn)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenario/generate", {})
    assert status == 200, body
    assert seen[0][1]["content"] == web._scenario_context(
        "", [], "warm summer song", 13.0, references_block=lib.references_context([card]))


def test_procedural_on_sglang_cuts_equal_pieces(live):
    proj = _clip_with_duration(live.outdir, 13.0)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/scenario/generate",
                         {"procedural": True})
    assert status == 200, body
    scenes = p.load_project(proj.path).scenario_scenes
    assert [(s["start"], s["end"], s["duration"], s["prompt"]) for s in scenes] == [
        (0.0, 6.5, 6.5, "warm summer song"), (6.5, 13.0, 6.5, "warm summer song")]


def test_equal_scenes_respect_5_to_10_seconds():
    assert [s["duration"] for s in web._equal_scenario_scenes(25.0, "c")] == \
        [25.0 / 3, 25.0 / 3, 25.0 / 3]
    assert [s["duration"] for s in web._equal_scenario_scenes(5.0, "c")] == [5.0]


def test_build_clip_scenes_on_sglang_covers_the_whole_track(monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    scenario = [{"tag": "a", "start": 0.0, "end": 7.0, "prompt": "a", "duration": 7.0},
                {"tag": "b", "start": 7.0, "end": 14.0, "prompt": "b", "duration": 7.0},
                {"tag": "c", "start": 14.0, "end": 20.0, "prompt": "c", "duration": 6.0}]
    scenes = web.build_clip_scenes({"duration": 20.0}, style_block="", scenario_scenes=scenario)
    assert [s["duration"] for s in scenes] == [158 / 24, 174 / 24, 157 / 24]
    total = sum(s["duration"] for s in scenes)
    assert 20.0 <= total < 20.0 + 17 / 24


def test_build_clip_scenes_on_mlx_is_unchanged(monkeypatch):
    scenario = [{"tag": "a", "start": 0.0, "end": 7.0, "prompt": "a", "duration": 7.0},
                {"tag": "b", "start": 7.0, "end": 14.0, "prompt": "b", "duration": 7.0},
                {"tag": "c", "start": 14.0, "end": 20.0, "prompt": "c", "duration": 6.0}]
    scenes = web.build_clip_scenes({"duration": 20.0}, style_block="", scenario_scenes=scenario)
    assert sum(s["duration"] for s in scenes) <= 20.0


def _clip_project_with_scenes(tmp_path, durations_frames, mp3):
    proj = p.create_project(tmp_path / "out", "clip", "Clip")
    proj.track.update({"mastered_mp3": str(mp3), "duration": _probe(mp3)})
    proj.scenes = [{"idx": i, "prompt": "x", "duration": f / 24, "status": "pending",
                    "job_id": None, "clip_path": None, "keyframe_path": None}
                   for i, f in enumerate(durations_frames)]
    proj.save()
    return proj


def test_track_pieces_follow_snapped_frames_and_the_one_frame_overlap(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    commands = []

    def run(cmd, capture_output=True, text=True):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    pieces = proj.path.parent / "track" / "pieces"
    assert assemble._cut_track_piece(proj, 0, run=run) == pieces / "scene-000.wav"
    assemble._cut_track_piece(proj, 1, run=run)
    assert commands == [
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.000000",
         "-i", str(mp3), "-vn", "-af", "apad", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         "-t", f"{158 / 24:.6f}", str(pieces / "scene-000.wav")],
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{157 / 24:.6f}",
         "-i", str(mp3), "-vn", "-af", "apad", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         "-t", f"{175 / 24:.6f}", str(pieces / "scene-001.wav")]]


def test_a_real_piece_has_the_requested_length(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    piece = assemble._cut_track_piece(proj, 1, run=subprocess.run)
    assert abs(_probe(piece) - 175 / 24) < 0.03


def test_a_clip_scene_carries_its_piece_as_the_last_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    proj.stages["scenes"] = "running"
    proj.save()
    submitted = []

    class _Job:
        id = "j0"

    assemble.advance_project(proj, tmp_path / "queue", tmp_path / "out",
                             submit=lambda root, args, *a, **k: submitted.append(args) or _Job(),
                             run=subprocess.run)
    piece = proj.path.parent / "track" / "pieces" / "scene-000.wav"
    assert submitted[0][-4:] == ["--task", "ref2va", "--audio", str(piece)]
    assert piece.is_file()


def _test_clip(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=64x64:rate=24:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds}", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _assembly_case(tmp_path, overshoot_track_seconds):
    out = tmp_path / "out"
    proj = p.create_project(out, "clip", "Clip")
    pdir = proj.path.parent
    clips = [_test_clip(pdir / f"c{i}.mp4", 1.0) for i in range(2)]
    mp3 = _mp3(pdir / "song.mp3", overshoot_track_seconds)
    proj.track.update({"mastered_mp3": str(mp3), "duration": _probe(mp3)})
    proj.scenes = [{"idx": i, "prompt": "x", "duration": 1.0, "status": "done", "job_id": f"j{i}",
                    "clip_path": str(c), "keyframe_path": None, "head_drop_frames": 0}
                   for i, c in enumerate(clips)]
    proj.save()
    # these tests are about trimming the raw parts, not about the upscale stage (tests/test_route.py)
    proj.set_route_stage("upscale", False)
    return proj


def test_an_overshooting_clip_is_trimmed_to_the_track_on_sglang(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _assembly_case(tmp_path, 1.3)
    final = assemble.run(proj.path)
    assert abs(_probe(final) - p.load_project(proj.path).track["duration"]) <= 0.1


def test_the_same_overshoot_still_fails_on_mlx(tmp_path):
    proj = _assembly_case(tmp_path, 1.3)
    with pytest.raises(assemble.AssembleError):
        assemble.run(proj.path)


def _scenario_gate_clip(outdir, seconds=14.0):
    mp3 = _mp3(outdir / "uploads" / "song.mp3", seconds)
    proj = _clip_with_duration(outdir, seconds)
    proj.track.update({"mp3": str(mp3), "mastered_mp3": str(mp3)})
    proj.save()
    proj.update_scenario(scenario_scenes=[
        {"tag": "a", "start": 0.0, "end": 7.0, "prompt": "a woman dances", "duration": 7.0},
        {"tag": "b", "start": 7.0, "end": 14.0, "prompt": "a woman waves", "duration": 7.0}],
        scenario_style_block=None)
    proj.set_stage_status("scenario", "awaiting_approval")
    return proj


def test_approving_the_scenario_snaps_clip_scenes_and_submits_the_first_with_its_piece(live):
    """Task 5 leftover: the gate for a clip snaps onto sglang's grid, accepts a scene with no
    @-tag (the track piece is its audio reference) and queues scene 0 with that piece."""
    proj = _scenario_gate_clip(live.outdir)
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/scenario", {})
    assert status == 200, body
    reloaded = p.load_project(proj.path)
    assert [round(s["duration"] * 24) for s in reloaded.scenes] == [158, 191]
    piece = proj.path.parent / "track" / "pieces" / "scene-000.wav"
    jobs = _pending(live)
    assert len(jobs) == 1 and jobs[0].args[-2:] == ["--audio", str(piece)]
    assert abs(_probe(piece) - 158 / 24) < 0.03


def test_a_scenario_that_sglang_would_refuse_is_refused_at_the_gate(live, monkeypatch):
    """The gate runs the submission's own argv through sglang_args.parse: a piece that would
    overflow the audio-reference budget is a 400 there, nothing queued and nothing snapped."""
    proj = _scenario_gate_clip(live.outdir)
    monkeypatch.setattr(web, "_scene_reference_errors",
                        lambda *a, **k: [{"idx": 0, "code": "x", "message": "no"}])
    status, body = _call(live, "POST", f"/api/projects/{proj.id}/approve/scenario", {})
    assert (status, body["error"]["code"]) == (400, "scene_references_invalid")
    assert _pending(live) == []
    assert p.load_project(proj.path).stages["scenario"] == "awaiting_approval"


# == fix round 1 ====================================================================================

@pytest.mark.parametrize("seconds", [60.0, 90.0, 120.0])
def test_equal_pieces_of_a_long_track_snap_to_sglang_bounds_not_the_mac_ones(seconds, monkeypatch):
    """Review 1: on sglang a scene is bounded by sglang's 73..345 requested frames, not by the
    mac 5..10 s clamp -- that clamp starved the middle scenes and overflowed the last one."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    scenario = web._equal_scenario_scenes(seconds, "c")
    scenes = web.build_clip_scenes({"duration": seconds}, style_block="",
                                   scenario_scenes=scenario)
    frames = [round(s["duration"] * 24) for s in scenes]
    total = sum(frames) / 24
    assert seconds <= total < seconds + 17 / 24
    assert frames == EXPECTED_FRAMES[seconds]
    assert all(f <= 344 for f in frames)


EXPECTED_FRAMES = {
    60.0: [226, 242, 242, 242, 242, 259],
    90.0: [226] + [242] * 8,
    120.0: [226] + [242] * 7 + [225, 242, 242, 259],
}


def test_a_scene_inside_the_grid_cell_is_trimmed_but_a_big_overshoot_is_not(tmp_path, monkeypatch):
    """Review 2: only an overshoot of up to one grid step plus the tolerance is trimmed."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    proj = _assembly_case(tmp_path, 1.3)
    assert abs(_probe(assemble.run(proj.path)) - 1.3) <= 0.1
    big = _assembly_case(tmp_path / "b", 0.5)   # video 2.0 s, track 0.5 s: 1.5 s over
    with pytest.raises(assemble.AssembleError):
        assemble.run(big.path)


def test_the_tail_piece_is_padded_with_silence_to_the_scene_length(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157], mp3)
    commands = []

    def run(cmd, capture_output=True, text=True):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assemble._cut_track_piece(proj, 2, run=run)
    pieces = proj.path.parent / "track" / "pieces"
    assert commands == [
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{331 / 24:.6f}",
         "-i", str(mp3), "-vn", "-af", "apad", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         "-t", f"{158 / 24:.6f}", str(pieces / "scene-002.wav")]]


def test_a_piece_past_the_end_of_the_track_still_has_the_scene_length(tmp_path):
    mp3 = _mp3(tmp_path / "song.mp3", 21.0)
    # scenes run to 26 s, the track ends at 21 s
    proj = _clip_project_with_scenes(tmp_path, [158, 174, 157, 135], mp3)
    piece = assemble._cut_track_piece(proj, 3, run=subprocess.run)
    assert abs(_probe(piece) - 136 / 24) < 0.03


def test_the_sglang_coverage_error_speaks_sglang(monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(web, "_snap_scene_duration", lambda *a, **k: (12.0, 0.0))
    scenario = [{"tag": "a", "start": 0.0, "end": 5.0, "prompt": "a", "duration": 5.0}]
    with pytest.raises(web.ProjectSceneBuildError) as err:
        web.build_clip_scenes({"duration": 5.0}, style_block="", scenario_scenes=scenario)
    assert str(err.value) == (
        "scene durations snapped to sglang's frame grid cover 12.000s, track is 5.000s -- they "
        "must cover [5.000s, 5.708s) and the last scene may not exceed 15s (it is 12.000s)")
