"""Task 6, "Проекты": the web API -- `h3_48gb.web`'s new `build_clip_scenes` (a pure function, no
server) and the `/api/projects` routes (creation from a chat session or empty, the script/track
gates, scene/assembly retry, delete) -- plus the two existing routes (`PUT`/`POST .../duplicate`)
this task teaches to refuse a project scene's own job.

**Nothing here runs a real generation, a real Music3 take or a real ffmpeg concat.** A project
scene's own `kind="generate"` job is driven through `worker.run_job` with the same fakes
`tests/test_worker.py` already validates (`_scene_hook_spawn`, `_FakeGenerateProcess`) -- imported
from there rather than reinvented, so a drift between the two never goes unnoticed. `songrun.
run_song`/`align_track` and `assemble.run` are monkeypatched directly for the same reason
`tests/test_worker.py` monkeypatches them: this task's own job is proving `/api/projects` submits
the *right* job (kind, args, note, estimate) and reacts correctly to what comes back, not
re-proving Task 2/3/4's own ffmpeg/Music3 logic.

Three rules repeat from `tests/test_web.py`/`tests/test_chat_web.py`:

* **A refusal is checked by its code and its status**, never by "not 200".
* **A gate is checked from both sides** -- skipping it is refused, and passing it in order works.
* **Garbage from the chat model is validated honestly** (`args_invalid`, never a 500) -- a session's
  own `project` field is never assumed to already match `PROMPT_SCHEMA` (task 5 report, "сомнение
  3": stored without validating its shape).
"""
import json
from pathlib import Path

import pytest

from h3_48gb import assemble as assemble_module
from h3_48gb import project as project_module
from h3_48gb import queue as q
from h3_48gb import songrun as sr
from h3_48gb import web
from h3_48gb import worker
from test_chat_web import _serve  # noqa: F401 -- reused fixture, see its own module docstring
from test_web import _request as _raw_request  # bytes-in/bytes-out, for /media -- see C2 tests
from test_worker import _caffeinate_spy, _scene_hook_spawn

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16
_MP3_BYTES = b"ID3" + b"0" * 32


# == Pure function: build_clip_scenes and its building blocks ====================================


def _track(sections, duration, lyrics, caption="Warm pop ballad.\nSteady beat.\n"):
    return {"sections": sections, "duration": duration, "lyrics": lyrics, "caption": caption}


_TWO_SECTION_LYRICS = "[verse]\nHello there my friend\n[chorus]\nSing along with me\n"

#: `[intro]` carries no lyric lines of its own -- a real, unsung instrumental tag, matching
#: `sections`' own `start=None` entry for it in the tests below (Task 2's own convention: one
#: `sections` entry per tag *occurrence*, sung or not).
_THREE_SECTION_LYRICS = "[intro]\n" + _TWO_SECTION_LYRICS


def _assert_scene_durations_on_h3_grid(scenes):
    """Every scene's own `duration` already sits on H3's `17n + 5` frame grid (C1, final review) --
    `round(duration * 24) % 17 == 5` is `align_num_frames`'s own fixed point, proving that the
    pipeline's own upward rounding is a no-op against what `build_clip_scenes` promised, not a
    silent stretch."""
    for s in scenes:
        frames = round(s["duration"] * 24)
        assert frames % 17 == 5, f"scene {s['idx']} duration {s['duration']} is not on the H3 grid"


def _assert_scene_total_within_snap_tolerance(scenes, duration):
    """C1 (final review): the *grid-snapped* total may fall short of `track["duration"]` by up to
    `web._SNAPPED_COVERAGE_SHORTFALL_SECONDS` (a freeze-frame pad closes the rest at assembly time)
    but must never run over it -- nothing downstream can trim an overshoot."""
    total = sum(s["duration"] for s in scenes)
    lower = duration - web._SNAPPED_COVERAGE_SHORTFALL_SECONDS
    assert lower - 1e-6 <= total <= duration + 1e-6, (total, duration)


def test_build_clip_scenes_covers_the_full_track_with_an_intro_gap():
    """A typical shape: an unsung intro tag, then two sung sections whose `end`s already tile to
    `duration` (Task 2/3's own convention -- see `_clip_raw_segments`'s docstring)."""
    sections = [{"name": "intro", "start": None, "end": None},
                {"name": "verse", "start": 4.0, "end": 12.0},
                {"name": "chorus", "start": 12.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 20.0, _THREE_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 20.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert all(web.SCENE_MIN_SECONDS - 0.01 <= s["duration"] <= web.SCENE_MAX_SECONDS + 0.01
              for s in scenes)
    # I3 (fix round 1, 2026-08-19 review): the *effective* gap threshold is SCENE_MIN_SECONDS
    # (5s), not GAP_SCENE_THRESHOLD_SECONDS (1.5s) -- an intro of 4s clears 1.5s but not 5s, so
    # the unconditional second (H3-length) fold pass swallows it straight back into a neighbour,
    # discarding the gap-fallback prompt entirely. Stated directly, not as a tautological "A or
    # len(scenes) == 3" that was true regardless of which branch actually ran.
    assert not any("инструментальная интерлюдия" in s["prompt"] for s in scenes)
    assert [s["idx"] for s in scenes] == list(range(len(scenes)))
    for s in scenes:
        assert s["status"] == "pending"
        assert s["job_id"] is None and s["clip_path"] is None and s["keyframe_path"] is None


def test_build_clip_scenes_keeps_a_long_enough_intro_as_its_own_instrumental_scene():
    """The other side of I3's own point: an intro of 8s clears *both* thresholds (1.5s and the
    effective 5s), so it survives the second fold pass and keeps its own gap-fallback prompt."""
    sections = [{"name": "intro", "start": None, "end": None},
                {"name": "verse", "start": 8.0, "end": 16.0},
                {"name": "chorus", "start": 16.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 24.0, _THREE_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 24.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert scenes[0]["duration"] == pytest.approx(8.0, abs=0.01)  # 8.0s is already grid-exact
    assert scenes[0]["prompt"].startswith("инструментальная интерлюдия:")
    assert "no vocals" in scenes[0]["prompt"]


def test_build_clip_scenes_folds_a_short_intro_into_the_first_sung_scene():
    sections = [{"name": "intro", "start": None, "end": None},
                {"name": "verse", "start": 1.0, "end": 9.0},
                {"name": "chorus", "start": 9.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 17.0, _THREE_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 17.0)
    _assert_scene_durations_on_h3_grid(scenes)
    # The intro (1s, under the 1.5s gap threshold) never becomes its own scene.
    assert not any("инструментальная интерлюдия" in s["prompt"] for s in scenes)
    assert scenes[0]["duration"] == pytest.approx(8.708, abs=0.01)  # 1s intro + 8s verse, snapped


def test_build_clip_scenes_splits_a_section_longer_than_ten_seconds():
    sections = [{"name": "verse", "start": 0.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 23.0, "[verse]\nOne line to sing along to\n"))
    _assert_scene_total_within_snap_tolerance(scenes, 23.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert len(scenes) == 3  # ceil(23/10) == 3
    for s in scenes:
        assert web.SCENE_MIN_SECONDS <= s["duration"] <= web.SCENE_MAX_SECONDS
    # A split section shares one prompt across every one of its pieces (task 6's own decision:
    # a straight cut inside the same section is invisible -- one continuous shot).
    assert len({s["prompt"] for s in scenes}) == 1


def test_build_clip_scenes_merges_a_short_trailing_section_into_its_neighbour():
    sections = [{"name": "verse", "start": 0.0, "end": None},
                {"name": "chorus", "start": 10.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 12.0, _TWO_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 12.0)
    _assert_scene_durations_on_h3_grid(scenes)
    # The 2s tail (chorus) is under SCENE_MIN_SECONDS -- merged backward into the verse, and the
    # merged 12s scene then re-splits (>10s) into two pieces sharing one prompt.
    assert len(scenes) == 2
    assert len({s["prompt"] for s in scenes}) == 1


def test_build_clip_scenes_handles_a_track_with_nothing_sung_at_all():
    sections = [{"name": "verse", "start": None, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 14.0, _TWO_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 14.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert all("instrumental" in s["prompt"] for s in scenes)


def test_build_clip_scenes_skips_unsung_sections_and_uses_only_sung_ones_prompt_material():
    """Design spec, task brief: "секции со start=None (не спеты) пропускаются" -- an unsung
    section contributes no scene and no lyric text to any other scene's prompt."""
    lyrics = "[intro]\nnever actually sung\n[verse]\nHello there my friend\n"
    sections = [{"name": "intro", "start": None, "end": None},
                {"name": "verse", "start": 0.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 8.0, lyrics))
    assert len(scenes) == 1
    assert "never actually sung" not in scenes[0]["prompt"]
    assert "Hello there my friend" in scenes[0]["prompt"]


def test_build_clip_scenes_refuses_a_track_with_no_measured_duration():
    with pytest.raises(web.ProjectSceneBuildError, match="no measured duration"):
        web.build_clip_scenes(_track([{"start": 0.0, "end": None}], None, _TWO_SECTION_LYRICS))


def test_build_clip_scenes_refuses_a_sung_section_with_no_lyric_lines():
    """Defensive: a section Whisper matched (`start` known) but whose own tag has no lyric lines at
    all under it should not happen (Task 2's own matching needs a line to match), but if the data
    is this shape anyway, this is a clear refusal, not a `KeyError`/`IndexError` reaching a 500.
    """
    with pytest.raises(web.ProjectSceneBuildError, match="no lyric lines"):
        web.build_clip_scenes(_track([{"start": 0.0, "end": None}], 10.0, ""))


def test_build_clip_scenes_glues_a_literal_style_block_onto_every_scene(monkeypatch):
    """The drift-fix minor (fix round 1, 2026-08-19 review): `style_block` -- by default, the
    first sentence of `caption`'s own Global Metadata section -- is glued verbatim onto every
    scene's own prompt, sung and gap-fallback alike (`docs/h3-prompt-system.md`'s "same words"
    rule for keeping a visual style from drifting scene to scene)."""
    sections = [{"name": "intro", "start": None, "end": None},
                {"name": "verse", "start": 8.0, "end": 16.0},
                {"name": "chorus", "start": 16.0, "end": None}]
    track = _track(sections, 24.0, _THREE_SECTION_LYRICS,
                   caption="Wistful synthwave ballad.\n\nVocal Details: breathy alto.")
    scenes = web.build_clip_scenes(track)
    assert len(scenes) >= 2
    for s in scenes:
        assert "Wistful synthwave ballad" in s["prompt"]

    # An explicit `style_block` overrides the caption-derived default, verbatim -- checked against
    # the style clause specifically, since `caption`'s first line also independently drives the
    # unrelated "mood" text `_clip_section_prompt` always includes (a coincidental overlap when
    # `style_block` is left at its caption-derived default, not true once it is overridden).
    scenes2 = web.build_clip_scenes(track, style_block="A hand-drawn watercolor music video")
    for s in scenes2:
        assert "Visual style, identical in every scene: A hand-drawn watercolor music video" \
            in s["prompt"]
        assert "Visual style, identical in every scene: Wistful synthwave ballad" \
            not in s["prompt"]


def test_build_clip_scenes_refuses_when_coverage_does_not_start_at_zero(monkeypatch):
    """M5 (fix round 1, 2026-08-19 review): the built timeline's own shape is validated, not only
    its total duration -- a hypothetical bug that shifts every scene's own boundaries by the same
    offset would leave the summed duration untouched while the timeline itself no longer starts at
    0. `_clip_raw_segments`'s own convention makes this unreachable through real track data (see
    its docstring), so this proves the new guard fires by patching the internal fold helper the
    same way a latent bug would corrupt its output.
    """
    real_fold = web._fold_short_segments
    calls = []

    def spied_fold(segments, *a, **kw):
        result = real_fold(segments, *a, **kw)
        calls.append(result)
        if len(calls) == 2:  # the SCENE_MIN_SECONDS pass -- the last one before split/expand
            shifted = [{**result[0], "start": result[0]["start"] + 1.0}] + list(result[1:])
            shifted[-1] = {**shifted[-1], "end": shifted[-1]["end"] + 1.0}
            return shifted
        return result

    monkeypatch.setattr(web, "_fold_short_segments", spied_fold)
    sections = [{"name": "verse", "start": 0.0, "end": None}]
    with pytest.raises(web.ProjectSceneBuildError, match="not 0.0s"):
        web.build_clip_scenes(_track(sections, 10.0, "[verse]\nHello there\n"))


def test_build_clip_scenes_refuses_a_seam_gap_between_scenes(monkeypatch):
    """M5's other half: a gap (or overlap) *between* two scenes that leaves the summed duration
    unchanged (the shift on one boundary is exactly cancelled by an equal shift on its own other
    boundary) must still be refused -- the total-only check alone cannot see it."""
    real_fold = web._fold_short_segments
    calls = []

    def spied_fold(segments, *a, **kw):
        result = real_fold(segments, *a, **kw)
        calls.append(result)
        if len(calls) == 2 and len(result) >= 2:
            shifted = [result[0],
                      {**result[1], "start": result[1]["start"] + 0.5,
                       "end": result[1]["end"] + 0.5}] + list(result[2:])
            return shifted
        return result

    monkeypatch.setattr(web, "_fold_short_segments", spied_fold)
    sections = [{"name": "verse", "start": 0.0, "end": None},
               {"name": "chorus", "start": 6.0, "end": None}]
    with pytest.raises(web.ProjectSceneBuildError, match="seam"):
        web.build_clip_scenes(_track(sections, 12.0, _TWO_SECTION_LYRICS))


# == C1 (final review): scene durations must land on H3's own 17n+5 frame grid ====================


def _uniform_track(duration: float, *, section_seconds: float = 7.0) -> dict:
    """A track whose `sections` are `n` equal-length sung sections tiling `[0, duration)`, each
    close to `section_seconds` long (well inside `[SCENE_MIN_SECONDS, SCENE_MAX_SECONDS]`, so
    `build_clip_scenes`' own fold/split passes are no-ops) -- realistic scene *counts* for a real
    track (a 295s track gets ~40 scenes) without needing real Whisper-timed section data, which is
    exactly the regime the drift in C1 (final review) was found in: the more scenes, the more a
    per-scene rounding error compounds.
    """
    n = max(1, round(duration / section_seconds))
    boundaries = [duration * i / n for i in range(n + 1)]
    tags = (["verse", "chorus"] * ((n // 2) + 1))[:n]
    sections = [{"name": tags[i], "start": boundaries[i], "end": boundaries[i + 1]}
               for i in range(n)]
    lyrics = "".join(f"[{tag}]\nline about {tag} number {i}\n" for i, tag in enumerate(tags))
    return _track(sections, duration, lyrics)


@pytest.mark.parametrize("duration", [295.0, 30.0, 60.0, 90.0])
def test_build_clip_scenes_snaps_every_duration_onto_the_h3_frame_grid(duration):
    """C1 (final review): the pipeline rounds a scene's own duration *up* to the next `17n + 5`
    frame count before generation starts (`align_num_frames`) -- a `build_clip_scenes` duration that
    is not already on that grid makes the real render longer than promised, and summed over a whole
    clip's scenes the drift reached +1.6..3.5s against `assemble.DURATION_TOLERANCE_SECONDS`'s 0.5s
    budget, so the assembled clip's own length check failed deterministically and `/assembly/retry`
    only repeated the same arithmetic. Reproduced on the real 295s track from the review, plus three
    synthetic ones spanning a realistic range of scene counts.
    """
    scenes = web.build_clip_scenes(_uniform_track(duration))
    _assert_scene_durations_on_h3_grid(scenes)
    _assert_scene_total_within_snap_tolerance(scenes, duration)
    for s in scenes:
        assert web.SCENE_MIN_SECONDS - 1e-6 <= s["duration"] <= web.SCENE_MAX_SECONDS + 1e-6


def test_h3_grid_points_within_scene_bounds_are_exactly_these_seven():
    """Pinned directly: the only durations `_snap_scene_duration` can ever return are H3's own
    `17n + 5` frame counts that also fall inside `[SCENE_MIN_SECONDS, SCENE_MAX_SECONDS]` -- seven
    of them (~5.17/5.88/6.58/7.29/8.0/8.71/9.42s). If H3's own frame grid or the scene bounds ever
    change, this is the test that notices.
    """
    points = []
    frames = web._grid_frames_at_or_above(round(web.SCENE_MIN_SECONDS * web._H3_FPS))
    while frames / web._H3_FPS <= web.SCENE_MAX_SECONDS + 1e-9:
        points.append(frames / web._H3_FPS)
        frames += web._H3_FRAMES_PER_CHUNK
    assert len(points) == 7
    for p in points:
        assert web.SCENE_MIN_SECONDS <= p <= web.SCENE_MAX_SECONDS


def test_snap_scene_duration_never_drops_below_scene_min_seconds():
    """The narrow trap zone (final review, C1): a raw duration just above `SCENE_MIN_SECONDS` whose
    plain floor-to-grid would land *below* it (5.02s floors to 107/24 ≈ 4.458s) must clamp up to the
    lowest in-range grid point instead of breaching the floor."""
    snapped, _carry = web._snap_scene_duration(5.02, 0.0)
    assert snapped >= web.SCENE_MIN_SECONDS
    assert snapped == pytest.approx(124 / 24, abs=1e-6)


def test_snap_scene_duration_never_exceeds_scene_max_seconds():
    """The symmetric edge: a raw duration near `SCENE_MAX_SECONDS` with enough carried-in remainder
    to push its target over 10s (9.9 + 0.7 = 10.6, which would floor to 243/24 ≈ 10.125s) must clamp
    down to the highest in-range grid point instead."""
    snapped, _carry = web._snap_scene_duration(9.9, 0.7)
    assert snapped <= web.SCENE_MAX_SECONDS
    assert snapped == pytest.approx(226 / 24, abs=1e-6)


# == Server fixtures ===============================================================================


def _project_body(kind="video", scenes=None, lyrics=None, caption=None):
    return {"kind": kind, "scenes": scenes, "lyrics": lyrics, "caption": caption}


def _patch_session_project(srv, sid: str, project: dict, *, kind=None) -> None:
    """Writes `session["project"]` (and `session["kind"]`, task 5's own mirror) directly onto a
    session file already created through `POST /api/chat` -- standing in for a chat turn that
    answered with a `project` object, without needing a real (or fake) LLM round trip for tests
    that only care about what `/api/projects` does with the result.
    """
    path = Path(srv.root) / "chat" / f"{sid}.json"
    session = json.loads(path.read_text(encoding="utf-8"))
    session["project"] = project
    if kind or project.get("kind"):
        session["kind"] = kind or project["kind"]
    path.write_text(json.dumps(session), encoding="utf-8")


def _new_session_with_project(srv, project: dict) -> str:
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    _patch_session_project(srv, sid, project)
    return sid


_VIDEO_SCENES = [
    {"prompt": "integrated_multimodal_description: [Shot 1] scene one\n\n"
              "overall_soundscape: quiet\n\nnon_diegetic_music: none",
     "duration": 6.0},
    {"prompt": "integrated_multimodal_description: [Shot 1] scene two\n\n"
              "overall_soundscape: quiet\n\nnon_diegetic_music: none",
     "duration": 7.0},
]


# == POST /api/projects: creation =================================================================


def test_create_project_with_only_a_kind_makes_an_empty_unapprovable_shell(_serve):
    srv = _serve()
    created = srv.post_json("/api/projects", {"kind": "song"})
    assert created["project"]["kind"] == "song"
    assert created["project"]["scenes"] == []
    assert created["project"]["stages"]["script"] == "draft"
    got = srv.get_json(f"/api/projects/{created['id']}")
    assert got["project"]["id"] == created["id"]


def test_create_project_without_a_kind_or_session_is_refused(_serve):
    srv = _serve()
    status, payload = srv.post_json_raw("/api/projects", {})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


def test_create_project_with_an_unknown_kind_is_refused(_serve):
    srv = _serve()
    status, payload = srv.post_json_raw("/api/projects", {"kind": "movie"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


def test_create_project_from_a_chat_sessions_video_scenario(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    created = srv.post_json("/api/projects", {"session_id": sid})
    proj = created["project"]
    assert proj["kind"] == "video"
    assert [s["prompt"] for s in proj["scenes"]] == [s["prompt"] for s in _VIDEO_SCENES]
    assert [s["duration"] for s in proj["scenes"]] == [s["duration"] for s in _VIDEO_SCENES]
    assert [s["idx"] for s in proj["scenes"]] == [0, 1]
    assert proj["stages"]["script"] == "awaiting_approval"


def test_create_project_from_a_chat_sessions_song_lyrics(_serve):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Mournful ballad."))
    created = srv.post_json("/api/projects", {"session_id": sid})
    proj = created["project"]
    assert proj["kind"] == "song"
    assert proj["track"]["lyrics"] == _TWO_SECTION_LYRICS
    assert proj["track"]["caption"] == "Mournful ballad."
    assert proj["track"]["source"] == "generate"
    assert proj["stages"]["script"] == "awaiting_approval"


def test_create_project_a_kind_in_the_body_overrides_the_sessions_own(_serve):
    """A "Новый проект" button (task 7) that already knows what it wants does not have to fabricate
    a matching chat session first."""
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    created = srv.post_json("/api/projects", {"session_id": sid, "kind": "song"})
    assert created["project"]["kind"] == "song"
    assert created["project"]["scenes"] == []  # video's own scenes are not read for kind=song


def test_create_project_from_an_unknown_session_is_refused(_serve):
    srv = _serve()
    status, payload = srv.post_json_raw(
        "/api/projects", {"session_id": "nosuchsession", "kind": "video"})
    assert (status, payload["error"]["code"]) == (404, "chat_not_found")


@pytest.mark.parametrize("bad_scenes", [
    "not a list",
    [{"prompt": "x"}],                          # missing duration
    [{"duration": 6.0}],                        # missing prompt
    [{"prompt": "", "duration": 6.0}],           # empty prompt
    [{"prompt": "x", "duration": -1.0}],         # non-positive duration
    [{"prompt": "x", "duration": "6"}],          # duration is a string
    [42],                                        # not even an object
    [{"prompt": "x", "duration": 4.99}],         # C3: below SCENE_MIN_SECONDS
    [{"prompt": "x", "duration": 10.01}],        # C3: above SCENE_MAX_SECONDS
    [{"prompt": "x", "duration": 60.0}],         # C3: a whole scene's worth of drift, unchecked
])
def test_create_project_rejects_garbage_scenes_from_the_model_honestly(_serve, bad_scenes):
    """Task 5 report, "сомнение 3": `session["project"]` is stored without validating its shape --
    this route is where that validation actually happens, with a clear `args_invalid`, never a 500.
    """
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=bad_scenes))
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


@pytest.mark.parametrize("field,bad_value", [("lyrics", 42), ("caption", [])])
def test_create_project_rejects_a_non_string_lyrics_or_caption(_serve, field, bad_value):
    srv = _serve()
    body = _project_body(kind="song", scenes=None, lyrics=None, caption=None)
    body[field] = bad_value
    sid = _new_session_with_project(srv, body)
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


# -- import track (design spec addendum) ----------------------------------------------------------


def test_upload_accepts_an_mp3_for_track_import(_serve):
    srv = _serve()
    status, answer = srv.upload_raw(_MP3_BYTES, "song.mp3")
    assert status == 200, answer
    assert answer["path"].endswith(".mp3")
    assert Path(answer["path"]).read_bytes() == _MP3_BYTES


def test_create_project_with_an_imported_track(_serve):
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    created = srv.post_json(
        "/api/projects",
        {"session_id": sid, "track_source": "import", "track_path": upload["path"]})
    proj = created["project"]
    assert proj["track"]["source"] == "import"
    assert proj["track"]["mp3"] == str(Path(upload["path"]).resolve())
    assert proj["stages"]["script"] == "awaiting_approval"


def test_create_project_with_an_imported_track_and_no_lyrics_is_valid(_serve):
    """Task 1 ("Сюжет клипа" wave, "авто-лирика"): a clip project imported without a session (so
    no lyrics were ever supplied) used to leave `stages.script` stuck at `"draft"` forever --
    `POST /api/projects` itself never refused it (no `args_invalid`), but the project was
    unapprovable, a dead end in every practical sense. `track_source="import"` on its own is now
    enough for `stages.script` to reach `"awaiting_approval"`, matching `kind="clip"`/lyrics-given
    imports (`test_create_project_with_an_imported_track` above).
    """
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    created = srv.post_json(
        "/api/projects",
        {"kind": "clip", "track_source": "import", "track_path": upload["path"]})
    proj = created["project"]
    assert proj["track"]["source"] == "import"
    assert proj["track"]["lyrics"] is None
    assert proj["stages"]["script"] == "awaiting_approval"


def test_create_project_import_requires_an_mp3_suffix(_serve):
    srv = _serve()
    upload = srv.upload_raw(_PNG_BYTES, "frame.png")[1]
    status, payload = srv.post_json_raw(
        "/api/projects",
        {"kind": "clip", "track_source": "import", "track_path": upload["path"]})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


def test_create_project_import_is_refused_without_a_track_path(_serve):
    srv = _serve()
    status, payload = srv.post_json_raw(
        "/api/projects", {"kind": "clip", "track_source": "import"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


def test_create_project_import_is_only_for_clip_projects(_serve):
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    status, payload = srv.post_json_raw(
        "/api/projects",
        {"kind": "song", "track_source": "import", "track_path": upload["path"]})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


# == GET /api/projects, GET /api/projects/<id> ====================================================


def test_list_projects_summarises_every_project(_serve):
    srv = _serve()
    a = srv.post_json("/api/projects", {"kind": "song"})["id"]
    b = srv.post_json("/api/projects", {"kind": "video"})["id"]
    listed = srv.get_json("/api/projects")["projects"]
    ids = {row["id"] for row in listed}
    assert {a, b} <= ids
    row = next(row for row in listed if row["id"] == a)
    assert row["kind"] == "song"
    assert row["scenes_total"] == 0
    assert row["active_job"] is None


def test_read_unknown_project_is_404(_serve):
    srv = _serve()
    status, payload = srv.get_json_raw("/api/projects/nosuchproject")
    assert (status, payload["error"]["code"]) == (404, "project_not_found")


def test_project_id_cannot_climb_out_of_the_projects_directory(_serve):
    srv = _serve()
    status, payload = srv.get_json_raw("/api/projects/..%2f..%2fetc%2fpasswd")
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")


def test_state_carries_a_project_summary(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "song"})["id"]
    state = srv.get_json("/api/state")
    ids = {row["id"] for row in state["projects"]}
    assert pid in ids


# == Gates: approve/script, approve/track =========================================================


def test_approve_script_before_content_exists_is_refused(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/script", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_approve_track_before_script_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/track", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_approve_unknown_stage_is_refused(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    for stage in ("scenes", "assembly", "whatever"):
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/{stage}", {})
        assert (status, payload["error"]["code"]) == (400, "args_invalid"), stage


def test_approve_script_twice_is_refused_the_second_time(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/script", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_approve_script_for_a_video_project_submits_scene_zero(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    approved = srv.post_json(f"/api/projects/{pid}/approve/script", {})
    assert approved["advance"]["action"] == "submitted_scene"
    assert approved["project"]["scenes"][0]["status"] == "running"

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].note == assemble_module.scene_note(pid, 0)
    assert pending[0].kind == q.KIND_GENERATE


def test_approve_script_for_a_clip_project_submits_a_song_job(_serve):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    approved = srv.post_json(f"/api/projects/{pid}/approve/script", {})
    assert "job_id" in approved["submit"]
    assert approved["project"]["stages"]["track"] == "draft", (
        "M6: stages.track must not claim 'running' while the song job is only queued")

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].kind == q.KIND_SONG
    assert pending[0].args == ["song", "--project", str(Path(srv.root) / "projects" / pid /
                                                        "project.json")]

    detail = srv.get_json(f"/api/projects/{pid}")
    assert detail["active_job"]["kind"] == "track"


def test_approve_script_for_an_imported_clip_with_no_lyrics_submits_a_song_job(_serve):
    """Task 1: the script gate for an imported clip with no lyrics behaves exactly like the
    lyrics-given case (`test_approve_script_for_a_clip_project_submits_a_song_job`) -- a
    `kind="song"` job is submitted either way; the worker (`h3_48gb.worker._run_song_job`, out of
    this route's own concern) is what tells "matched against known lyrics" and "auto-transcribed"
    apart once that job actually runs.
    """
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    pid = srv.post_json(
        "/api/projects",
        {"kind": "clip", "track_source": "import", "track_path": upload["path"]})["id"]
    approved = srv.post_json(f"/api/projects/{pid}/approve/script", {})
    assert "job_id" in approved["submit"]

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].kind == q.KIND_SONG


def test_approve_track_for_a_video_project_is_refused_explicitly(_serve):
    """M3 (fix round 1, 2026-08-19 review): `stage='track'` never legitimately applies to
    `kind='video'` -- script approval for a video project never submits a song job, so
    `stages.track` should never reach `awaiting_approval` in the first place. If it is coaxed
    there anyway (by hand, or by a future bug), the route must refuse it explicitly (409), not
    silently answer `ok: true` having done nothing -- the old behaviour before this fix."""
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    proj = project_module.load_project(Path(srv.root) / "projects" / pid / "project.json")
    proj.set_stage_status("track", "awaiting_approval")

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/track", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready"), payload

    # And the stage must not have been quietly approved before the refusal.
    stuck = srv.get_json(f"/api/projects/{pid}")["project"]
    assert stuck["stages"]["track"] == "awaiting_approval"


# == C1: a failed side effect must not leave a stage stuck "approved" =============================


def test_approve_track_survives_a_failed_scene_build_and_can_be_retried(_serve):
    """C1 (fix round 1, 2026-08-19 review): `approve_stage` must run *after* `build_clip_scenes`
    actually succeeds, not before it. Before this fix, a failed build left `stages.track` stuck
    `"approved"` forever with no scenes ever built and no way back in -- a repeat `approve/track`
    answered 409 (the stage was no longer `awaiting_approval`), and there is no separate "retry the
    track build" endpoint, only `scenes/<idx>/retry`, which needs a scene to already exist.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    proj = project_module.load_project(project_path)
    # No `duration` set yet -- `build_clip_scenes` must refuse (the worker normally sets it,
    # Task 3's I4; forcing the gate open by hand is the only way to reach this state through the
    # routes, same technique `test_approve_track_for_a_clip_project_with_an_unmeasured_track_is_
    # refused_honestly` already uses).
    proj.set_stage_status("track", "awaiting_approval")

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/track", {})
    assert (status, payload["error"]["code"]) == (400, "project_scene_build_failed"), payload

    stuck = srv.get_json(f"/api/projects/{pid}")["project"]
    assert stuck["stages"]["track"] == "awaiting_approval", (
        "a failed build must not leave the project stuck 'approved' with no scenes and no retry "
        "path -- the same gate must still be open")
    assert stuck["scenes"] == []

    # Fix the track (the worker's own I4 write, done by hand here) and retry the *same* gate.
    proj2 = project_module.load_project(project_path)
    proj2.update_track(duration=8.0, sections=[{"name": "verse", "start": 0.0, "end": None}])
    retried = srv.post_json(f"/api/projects/{pid}/approve/track", {})
    assert retried["project"]["stages"]["track"] == "approved"
    assert retried["advance"]["action"] == "submitted_scene"
    assert len(retried["project"]["scenes"]) >= 1


def test_approve_script_survives_a_failed_song_submit_and_can_be_retried(_serve):
    """C1's other half: a failed *submit* (not a failed build) must leave the same gate retryable
    too. `output_stem_conflict` is a real, deterministic way to make `q.submit` fail without
    monkeypatching internals -- a leftover artifact already claims the exact `output_stem`
    `_submit_project_song_job` always uses for this project's song job.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]

    project_path = Path(srv.root) / "projects" / pid / "project.json"
    track_dir = project_path.parent / "track"
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / "job-song.json").write_bytes(b"{}")  # claims the song job's own output_stem

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/script", {})
    assert (status, payload["error"]["code"]) == (400, "output_stem_conflict"), payload

    stuck = srv.get_json(f"/api/projects/{pid}")["project"]
    assert stuck["stages"]["script"] == "awaiting_approval", (
        "a failed submit must not leave the project stuck 'approved' with nothing queued")

    jobs, _broken = q.scan(srv.queue_root)
    assert not any(j.state in ("pending", "running") for j in jobs), (
        "nothing should have been queued by the failed attempt")

    (track_dir / "job-song.json").unlink()
    retried = srv.post_json(f"/api/projects/{pid}/approve/script", {})
    assert "job_id" in retried["submit"]
    assert retried["project"]["stages"]["script"] == "approved"


# == Full lifecycle: song project (mock queue + worker) ===========================================


_FAKE_SONG_SECTIONS = [{"name": "verse", "start": 0.0, "end": 15.0}]


def test_song_project_full_lifecycle(_serve, monkeypatch, tmp_path):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    project_path = Path(srv.root) / "projects" / pid / "project.json"
    track_dir = project_path.parent / "track"
    fake_result = sr.SongResult(
        wav=track_dir / "song.wav", mastered_wav=track_dir / "song.mastered.wav",
        mp3=track_dir / "song.mp3", mastered_mp3=track_dir / "song.mastered.mp3",
        duration=15.0, transcript="ла ла ла", sections=_FAKE_SONG_SECTIONS, undersung=False)

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)
    job = q.claim(srv.queue_root)
    assert job.kind == q.KIND_SONG
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    awaiting = srv.get_json(f"/api/projects/{pid}")["project"]
    assert awaiting["stages"]["track"] == "awaiting_approval"
    assert awaiting["track"]["mp3"] == str(fake_result.mp3)

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/script", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready"), (
        "script is already approved -- the gate must not be re-openable")

    approved_track = srv.post_json(f"/api/projects/{pid}/approve/track", {})
    assert approved_track["project"]["stages"]["track"] == "approved"
    # M2 (fix round 1, 2026-08-19 review): kind=song has no scenes/assembly of its own -- both
    # stages are set explicitly to the terminal "done" (not left at "draft" forever, which would
    # read as "not started" rather than "this project is finished").
    assert approved_track["project"]["stages"]["scenes"] == "done"
    assert approved_track["project"]["stages"]["assembly"] == "done"
    assert "advance" not in approved_track  # nothing further to submit for kind=song

    jobs, _broken = q.scan(srv.queue_root)
    assert not any(j.state in ("pending", "running") for j in jobs), (
        "a song project is done once its track is approved -- nothing should still be queued")


# == C2 (final review): /media must serve a project's own mp3 track ===============================


def test_media_serves_a_projects_own_track_mp3(_serve, monkeypatch):
    """C2 (final review): `MEDIA_SUFFIXES` did not include `.mp3` -- a track is always an mp3
    (`songrun.SongResult`), so `projectTrackStageHtml`'s own `<audio>` player built a `/media` URL
    that this route refused with `media_type_not_allowed`, and a track could never actually be
    listened to before approving it.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    project_path = Path(srv.root) / "projects" / pid / "project.json"
    track_dir = project_path.parent / "track"
    mastered_mp3 = track_dir / "song.mastered.mp3"
    fake_result = sr.SongResult(
        wav=track_dir / "song.wav", mastered_wav=track_dir / "song.mastered.wav",
        mp3=track_dir / "song.mp3", mastered_mp3=mastered_mp3,
        duration=15.0, transcript="ла ла ла", sections=_FAKE_SONG_SECTIONS, undersung=False)

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        mastered_mp3.parent.mkdir(parents=True, exist_ok=True)
        mastered_mp3.write_bytes(_MP3_BYTES)
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)
    job = q.claim(srv.queue_root)
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    awaiting = srv.get_json(f"/api/projects/{pid}")["project"]
    assert awaiting["track"]["mastered_mp3"] == str(mastered_mp3)
    relative = "/media/" + str(mastered_mp3.relative_to(Path(srv.root))).replace("\\", "/")
    status, _, body = _raw_request(srv, relative)
    assert status == 200, body
    assert body == _MP3_BYTES


# == I2 (final review): cache-buster -- track/assembly carry a `v` field off real file mtimes =====


def test_project_track_carries_a_cache_buster_v_once_the_mp3_exists(_serve, monkeypatch):
    """I2 (final review): before this, a recomputed track/final showed the *old* bytes if a
    browser's own cache still had the previous run under that exact `/media` URL -- "Пересчитать
    трек" writes back onto the same `track/song.mastered.mp3` path every time. `GET
    /api/projects/<id>` (and every route that hands a project back) must carry a version the page
    can turn into `?v=` -- present only once the file this measures actually exists on disk.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert "v" not in detail["track"], "no mp3 exists yet -- no version to fabricate"

    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    track_dir = project_path.parent / "track"
    mastered_mp3 = track_dir / "song.mastered.mp3"
    fake_result = sr.SongResult(
        wav=track_dir / "song.wav", mastered_wav=track_dir / "song.mastered.wav",
        mp3=track_dir / "song.mp3", mastered_mp3=mastered_mp3,
        duration=15.0, transcript="ла ла ла", sections=_FAKE_SONG_SECTIONS, undersung=False)

    def fake_run_song(track_dir_arg, lyrics, caption, duration, *, seed, **kw):
        mastered_mp3.parent.mkdir(parents=True, exist_ok=True)
        mastered_mp3.write_bytes(_MP3_BYTES)
        return fake_result

    monkeypatch.setattr(sr, "run_song", fake_run_song)
    job = q.claim(srv.queue_root)
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    awaiting = srv.get_json(f"/api/projects/{pid}")["project"]
    assert isinstance(awaiting["track"]["v"], int)
    assert awaiting["track"]["v"] == int(mastered_mp3.stat().st_mtime)


def test_project_assembly_carries_a_cache_buster_v_once_final_exists(_serve, monkeypatch):
    """The other half: `assembly.final_path` -- "Пересчитать сборку" writes back onto the same
    `assembly/final.mp4` every time, the exact case `MEDIA_MAX_AGE`'s own year-long cache cannot
    tell apart from a first render on its own.
    """
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    spawn = _scene_hook_spawn()
    for expected_idx in (0, 1):
        job = q.claim(srv.queue_root)
        _write_fake_clip(job)
        code = worker.run_job(srv.queue_root, job, spawn=spawn, outdir=srv.root)
        assert code == 0

    def fake_assemble_run(project_path, *, run=None):
        proj = project_module.load_project(project_path)
        final = proj.path.parent / "assembly" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"fake final mp4")
        proj.update_assembly(final_path=str(final))
        proj.set_stage_status("assembly", "done")
        return final

    monkeypatch.setattr(assemble_module, "run", fake_assemble_run)
    assemble_job = q.claim(srv.queue_root)
    code = worker.run_job(srv.queue_root, assemble_job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    done = srv.get_json(f"/api/projects/{pid}")["project"]
    final_path = Path(done["assembly"]["final_path"])
    assert isinstance(done["assembly"]["v"], int)
    assert done["assembly"]["v"] == int(final_path.stat().st_mtime)


# == Full lifecycle: video project (mock queue + worker) ==========================================


def _write_fake_clip(job) -> None:
    path = Path(f"{job.output_stem}.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp4 bytes")


def test_video_project_full_lifecycle(_serve, monkeypatch):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    spawn = _scene_hook_spawn()
    for expected_idx in (0, 1):
        job = q.claim(srv.queue_root)
        assert job.note == assemble_module.scene_note(pid, expected_idx)
        _write_fake_clip(job)
        code = worker.run_job(srv.queue_root, job, spawn=spawn, outdir=srv.root)
        assert code == 0

    reloaded = srv.get_json(f"/api/projects/{pid}")["project"]
    assert all(s["status"] == "done" for s in reloaded["scenes"])
    assert reloaded["stages"]["scenes"] == "done"
    assert reloaded["stages"]["assembly"] == "running"

    assembled = {}

    def fake_assemble_run(project_path, *, run=None):
        proj = project_module.load_project(project_path)
        final = proj.path.parent / "assembly" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"fake final mp4")
        proj.update_assembly(final_path=str(final))
        proj.set_stage_status("assembly", "done")
        assembled["path"] = final
        return final

    monkeypatch.setattr(assemble_module, "run", fake_assemble_run)
    assemble_job = q.claim(srv.queue_root)
    assert assemble_job.kind == q.KIND_ASSEMBLE
    code = worker.run_job(srv.queue_root, assemble_job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    done = srv.get_json(f"/api/projects/{pid}")["project"]
    assert done["stages"]["assembly"] == "done"
    assert done["assembly"]["final_path"] == str(assembled["path"])


# == Full lifecycle: clip project with an imported track ==========================================


def test_clip_project_with_an_imported_track_builds_scenes_after_track_approval(_serve,
                                                                                 monkeypatch):
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json(
        "/api/projects",
        {"session_id": sid, "track_source": "import", "track_path": upload["path"]})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    project_path = Path(srv.root) / "projects" / pid / "project.json"
    track_dir = project_path.parent / "track"
    imported_mp3 = Path(upload["path"])
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=16.0, transcript="hello there my friend sing along with me",
        sections=[{"name": "verse", "start": 0.0, "end": 8.0},
                 {"name": "chorus", "start": 8.0, "end": None}],
        undersung=False)

    def fake_align_track(track_dir_arg, mp3_path, lyrics, **kw):
        return fake_result

    monkeypatch.setattr(sr, "align_track", fake_align_track)
    job = q.claim(srv.queue_root)
    assert job.kind == q.KIND_SONG
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    approved_track = srv.post_json(f"/api/projects/{pid}/approve/track", {})
    assert approved_track["advance"]["action"] == "submitted_scene"
    scenes = approved_track["project"]["scenes"]
    assert len(scenes) >= 1
    _assert_scene_total_within_snap_tolerance(scenes, 16.0)
    _assert_scene_durations_on_h3_grid(scenes)

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].note == assemble_module.scene_note(pid, 0)


def test_approve_track_for_a_clip_project_with_an_unmeasured_track_is_refused_honestly(_serve):
    """Defensive: a track cannot reach `awaiting_approval` without `duration` being set (the
    worker always writes it, Task 3's I4), but if it somehow did, `build_clip_scenes` refuses with
    a named code rather than a 500 -- checked here directly against the project's own methods,
    since there is no ordinary way to reach this state through the routes alone.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    proj = project_module.load_project(Path(srv.root) / "projects" / pid / "project.json")
    proj.set_stage_status("track", "awaiting_approval")

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/track", {})
    assert (status, payload["error"]["code"]) == (400, "project_scene_build_failed"), payload


# == Retry: track (task 7's own small addition to the server, "Пересчитать трек") ================


def _run_fake_song_job(srv, monkeypatch, *, duration=15.0):
    """Claims and runs the one pending `kind="song"` job with a fake `songrun.run_song`, landing
    the project's track at `stages.track == "awaiting_approval"` -- the shared setup every retry
    test below starts from.
    """
    fake_result = sr.SongResult(
        wav=Path("song.wav"), mastered_wav=Path("song.mastered.wav"), mp3=Path("song.mp3"),
        mastered_mp3=Path("song.mastered.mp3"), duration=duration, transcript="ла ла ла",
        sections=_FAKE_SONG_SECTIONS, undersung=False)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)
    job = q.claim(srv.queue_root)
    assert job.kind == q.KIND_SONG
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0


def test_retry_track_before_script_approval_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_retry_track_for_a_video_project_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})  # scene 0 running, script approved

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_retry_track_while_the_first_song_job_is_still_queued_is_refused(_serve):
    """M6's own point, exercised for this route too: `stages.track` alone stays `"draft"` while
    the first song job is queued, so this route must join against the queue, not trust the
    stage."""
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})  # song job now pending

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {})
    assert (status, payload["error"]["code"]) == (409, "project_running")


def test_retry_track_after_approval_is_refused(_serve, monkeypatch):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    _run_fake_song_job(srv, monkeypatch)
    srv.post_json(f"/api/projects/{pid}/approve/track", {})

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


# -- I1 (final review): a failed song job can be retried, not stuck forever ----------------------


def test_retry_track_after_a_failed_song_job_is_allowed(_serve, monkeypatch):
    """Before I1's own worker fix, a crashing song job left `stages.track` at `"draft"` -- this
    route already allowed a retry from there, so the bug was invisible from this side alone; what
    proves the fix end to end is that `stages.track` genuinely reaches `"failed"` (not `"draft"`)
    after the crash, *and* this route still accepts a retry from it.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    def boom(*a, **kw):
        raise sr.SongRunError("Music3 exploded")

    monkeypatch.setattr(sr, "run_song", boom)
    job = q.claim(srv.queue_root)
    assert job.kind == q.KIND_SONG
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 1

    failed = srv.get_json(f"/api/projects/{pid}")["project"]
    assert failed["stages"]["track"] == "failed"

    retried = srv.post_json(f"/api/projects/{pid}/track/retry", {})
    assert "job_id" in retried["submit"]
    assert retried["project"]["stages"]["track"] == "running"

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].kind == q.KIND_SONG


def test_retry_track_resubmits_a_song_job_and_marks_the_stage_running(_serve, monkeypatch):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    _run_fake_song_job(srv, monkeypatch)

    awaiting = srv.get_json(f"/api/projects/{pid}")["project"]
    assert awaiting["stages"]["track"] == "awaiting_approval"

    retried = srv.post_json(f"/api/projects/{pid}/track/retry", {})
    assert "job_id" in retried["submit"]
    assert retried["project"]["stages"]["track"] == "running"

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].kind == q.KIND_SONG

    # A stale approve of the take being replaced must not slip through while the retry job is
    # still in flight: `stages.track` is "running", not the "awaiting_approval" it was a moment
    # ago, so the ordinary gate check refuses it on its own.
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/track", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_retry_track_with_a_seed_writes_it_before_resubmitting(_serve, monkeypatch):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    _run_fake_song_job(srv, monkeypatch)

    retried = srv.post_json(f"/api/projects/{pid}/track/retry", {"seed": 4242})
    assert retried["project"]["track"]["seed"] == 4242


def test_retry_track_seed_must_be_a_number(_serve, monkeypatch):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    _run_fake_song_job(srv, monkeypatch)

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {"seed": "loud"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


def test_retry_track_seed_is_refused_for_an_imported_track(_serve, monkeypatch):
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json(
        "/api/projects",
        {"session_id": sid, "track_source": "import", "track_path": upload["path"]})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    imported_mp3 = Path(upload["path"])
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=16.0, transcript="hello there my friend sing along with me",
        sections=[{"name": "verse", "start": 0.0, "end": 8.0},
                 {"name": "chorus", "start": 8.0, "end": None}],
        undersung=False)
    monkeypatch.setattr(sr, "align_track", lambda *a, **kw: fake_result)
    job = q.claim(srv.queue_root)
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/track/retry", {"seed": 1})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")

    # Without a seed, the retry itself is allowed for an imported track too -- it just re-runs the
    # Whisper alignment pass rather than a fresh generation.
    retried = srv.post_json(f"/api/projects/{pid}/track/retry", {})
    assert "job_id" in retried["submit"]


# == Retry: scenes, assembly =======================================================================


def test_retry_scene_invalidates_the_chain_and_resubmits(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    job = q.claim(srv.queue_root)
    _write_fake_clip(job)
    worker.run_job(srv.queue_root, job, spawn=_scene_hook_spawn(), outdir=srv.root)
    # Scene 1 is now "running" with its own pending job.
    before = srv.get_json(f"/api/projects/{pid}")["project"]
    assert before["scenes"][0]["status"] == "done"
    assert before["scenes"][1]["status"] == "running"

    retried = srv.post_json(f"/api/projects/{pid}/scenes/0/retry", {})
    assert retried["advance"]["action"] == "submitted_scene"
    assert retried["advance"]["idx"] == 0
    reloaded = retried["project"]
    assert reloaded["scenes"][0]["status"] == "running"
    assert reloaded["scenes"][1]["status"] == "pending", (
        "invalidate_scene_chain must reset every scene from idx onward")


def test_retry_scene_cancels_an_orphaned_pending_tail_job(_serve):
    """I1 (fix round 1, 2026-08-19 review): retrying scene 0 while scene 1's own job is still
    sitting in `pending/` (submitted, not yet claimed by a worker) must cancel that job, not leave
    it in the queue -- otherwise a worker could claim the orphaned scene-1 job *before* the fresh
    scene-0 resubmit even runs, burning GPU minutes on a clip built from a keyframe that is about
    to be replaced.
    """
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    job0 = q.claim(srv.queue_root)
    _write_fake_clip(job0)
    worker.run_job(srv.queue_root, job0, spawn=_scene_hook_spawn(), outdir=srv.root)

    before = srv.get_json(f"/api/projects/{pid}")["project"]
    assert before["scenes"][0]["status"] == "done"
    assert before["scenes"][1]["status"] == "running"
    orphan_job_id = before["scenes"][1]["job_id"]

    jobs, _broken = q.scan(srv.queue_root)
    assert any(j.id == orphan_job_id and j.state == "pending" for j in jobs), (
        "scene 1's own job must still be queued, not yet claimed by a worker")

    retried = srv.post_json(f"/api/projects/{pid}/scenes/0/retry", {})
    assert retried["advance"]["action"] == "submitted_scene"
    assert retried["advance"]["idx"] == 0

    jobs, _broken = q.scan(srv.queue_root)
    ids = {j.id for j in jobs}
    assert orphan_job_id not in ids, (
        "retrying scene 0 must cancel scene 1's now-orphaned pending job")
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1, "only the fresh scene 0 resubmit should be left in the queue"
    assert pending[0].note == assemble_module.scene_note(pid, 0)


def test_retry_an_unknown_scene_index_is_404(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenes/9/retry", {})
    assert (status, payload["error"]["code"]) == (404, "project_scene_not_found")


def test_retry_scene_index_must_be_an_integer(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenes/oops/retry", {})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


def test_retry_assembly_requires_a_failed_stage(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "video"})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/assembly/retry", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_retry_assembly_resets_stage_and_resubmits(_serve):
    srv = _serve()
    proj = project_module.create_project(srv.root, "video", "Мой ролик")
    proj.scenes = [{"idx": 0, "prompt": "x", "duration": 6.0, "status": "done", "job_id": "j-0",
                   "clip_path": "/tmp/clip.mp4", "keyframe_path": None}]
    proj.stages["scenes"] = "done"
    proj.stages["assembly"] = "failed"
    proj.save()

    retried = srv.post_json(f"/api/projects/{proj.id}/assembly/retry", {})
    assert retried["advance"]["action"] == "submitted_assembly"
    assert retried["project"]["stages"]["assembly"] == "running"


# == DELETE /api/projects/<id> =====================================================================


def test_delete_a_draft_project_removes_its_directory(_serve):
    srv = _serve()
    pid = srv.post_json("/api/projects", {"kind": "song"})["id"]
    directory = Path(srv.root) / "projects" / pid
    assert directory.is_dir()
    status, payload = srv.delete_json_raw(f"/api/projects/{pid}")
    assert status == 200, payload
    assert not directory.exists()


def test_delete_an_unknown_project_is_404(_serve):
    srv = _serve()
    status, payload = srv.delete_json_raw("/api/projects/nosuchproject")
    assert (status, payload["error"]["code"]) == (404, "project_not_found")


def test_delete_a_project_with_a_scene_running_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})  # scene 0 now running

    status, payload = srv.delete_json_raw(f"/api/projects/{pid}")
    assert (status, payload["error"]["code"]) == (409, "project_running")


def test_delete_a_project_with_a_song_job_queued_is_refused(_serve):
    """M6's own point, exercised here: `stages.track` alone stays `"draft"` while the song job is
    queued, so the delete gate must join against the queue, not trust the stage."""
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="song", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    status, payload = srv.delete_json_raw(f"/api/projects/{pid}")
    assert (status, payload["error"]["code"]) == (409, "project_running")


def test_delete_is_refused_while_an_orphaned_pending_scene_job_still_exists(_serve):
    """I1 (fix round 1, 2026-08-19 review), other half: `_project_active_job` must find a pending
    project-scene job even once nothing on `project.json` points at it any more
    (`invalidate_scene_chain` clears `scenes[i].job_id`) -- otherwise DELETE would remove the
    project's own directory while a job is still about to write a clip into it.

    Bypasses `_retry_project_scene`'s own new cancellation (I1's other half) on purpose, calling
    `invalidate_scene_chain` directly through `project_module` -- this isolates the race window
    that cancellation narrows but cannot close outright (a job already claimed by the time the
    route's own cancel attempt runs), by constructing the exact state that window can leave
    behind: a queued job with no scene pointing at it any more.
    """
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})

    job0 = q.claim(srv.queue_root)
    _write_fake_clip(job0)
    worker.run_job(srv.queue_root, job0, spawn=_scene_hook_spawn(), outdir=srv.root)
    # Scene 1's own job is now pending, not yet claimed by a worker.

    project_path = Path(srv.root) / "projects" / pid / "project.json"
    proj = project_module.load_project(project_path)
    proj.invalidate_scene_chain(0)

    jobs, _broken = q.scan(srv.queue_root)
    assert any(j.state == "pending" for j in jobs), "the orphaned job must still be sitting there"

    status, payload = srv.delete_json_raw(f"/api/projects/{pid}")
    assert (status, payload["error"]["code"]) == (409, "project_running"), payload


# == q.update / duplicate refuse a project scene's own job ========================================


def test_editing_a_project_scenes_job_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    jobs, _broken = q.scan(srv.queue_root)
    job_id = next(j.id for j in jobs if j.state == "pending")

    payload = {"args": ["generate", "something else"], "note": "not a scene any more"}
    connection_status, answer = srv._request("PUT", f"/api/jobs/{job_id}", payload)
    assert (connection_status, answer["error"]["code"]) == (409, "project_scene_locked"), answer


def test_duplicating_a_project_scenes_job_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    jobs, _broken = q.scan(srv.queue_root)
    job_id = next(j.id for j in jobs if j.state == "pending")

    status, answer = srv.post_json_raw(f"/api/jobs/{job_id}/duplicate", {})
    assert (status, answer["error"]["code"]) == (409, "project_scene_locked"), answer
