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
import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from h3_48gb import assemble as assemble_module
from h3_48gb import project as project_module
from h3_48gb import provider
from h3_48gb import queue as q
from h3_48gb import songrun as sr
from h3_48gb import web
from h3_48gb import worker
from _fake_llama import _FakeLlama
from test_chat_web import _external, _serve  # noqa: F401 -- reused, see test_chat_web's own docstring
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
    """Every scene's own `duration` already sits on the H3 frame grid *that scene actually renders
    on* (C1, final review; re-split by the latent-handoff wave, task 6) -- proving that the
    pipeline's own upward rounding (`align_num_frames`) is a no-op against what `build_clip_scenes`
    promised, not a silent stretch.

    Two grids, because a chained scene requests more than it delivers (`docs/FEASIBILITY-latent-
    handoff.md` §4.1):

    * **scene 0 and every `fresh_start` scene** render from text or a picture, so what they request
      *is* what they deliver: `round(duration * 24) % 17 == 5`, `align_num_frames`'s own fixed point.
    * **a chained scene** starts from the previous scene's own latent tail, requests
      `duration * 24 + web._SCENE_LATENT_OVERLAP_FRAMES` frames and hands the overlap back to
      `assemble._drop_head_frames`: the *delivered* count is a plain multiple of 17, and it is the
      *request* that must land on `17j + 5`. Both halves are asserted -- "delivered % 17 == 0"
      alone would also pass for a scene whose request fell off H3's grid entirely.
    """
    for s in scenes:
        frames = round(s["duration"] * 24)
        if s["idx"] > 0 and not s.get("fresh_start", False):
            assert frames % 17 == 0, (
                f"scene {s['idx']} is chained: it delivers {frames} frames, but a chained scene's "
                f"own grid is 17k (its request, {frames + 22}, must land on 17j+5)")
            assert (frames + web._SCENE_LATENT_OVERLAP_FRAMES - 5) % 17 == 0, (
                f"scene {s['idx']}: request {frames + web._SCENE_LATENT_OVERLAP_FRAMES} is not on "
                f"H3's own 17j+5 grid")
        else:
            assert frames % 17 == 5, (
                f"scene {s['idx']} is not chained: it delivers {frames} frames, but its own grid "
                f"is 17n+5")


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
    # A split section keeps one section prompt across its pieces, but every piece after the
    # first appends the continuation clause (night run 3, 2026-08-26: identical text + the
    # previous piece's keyframe rendered as the same scene looping).
    assert scenes[0]["prompt"].endswith(".")
    assert not scenes[0]["prompt"].endswith(web.SPLIT_CONTINUATION_CLAUSE)
    for s in scenes[1:]:
        assert s["prompt"] == scenes[0]["prompt"] + web.SPLIT_CONTINUATION_CLAUSE


def test_build_clip_scenes_merges_a_short_trailing_section_into_its_neighbour():
    sections = [{"name": "verse", "start": 0.0, "end": None},
                {"name": "chorus", "start": 10.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 12.0, _TWO_SECTION_LYRICS))
    _assert_scene_total_within_snap_tolerance(scenes, 12.0)
    _assert_scene_durations_on_h3_grid(scenes)
    # The 2s tail (chorus) is under SCENE_MIN_SECONDS -- merged backward into the verse, and the
    # merged 12s scene then re-splits (>10s) into two pieces: one section prompt, the later
    # piece carrying the continuation clause.
    assert len(scenes) == 2
    assert scenes[1]["prompt"] == scenes[0]["prompt"] + web.SPLIT_CONTINUATION_CLAUSE


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


# == Pure function: build_clip_scenes(scenario_scenes=...) (Task 3, "Сюжет клипа" wave) ===========


def _scenario_track(duration, caption="Warm pop ballad.\nSteady beat.\n"):
    """A track dict `scenario_scenes=` mode should never need to read `sections`/`lyrics` from --
    only `duration` (`caption` is not read either in this mode, unlike the procedural path's own
    caption-derived `style_block` default -- see
    `test_build_clip_scenes_from_scenario_ignores_track_sections_lyrics_and_caption`)."""
    return {"duration": duration, "caption": caption}


def _scenario_scene(tag, start, end, prompt, duration=None, **extra):
    scene = {"tag": tag, "start": start, "end": end, "prompt": prompt,
             "duration": duration if duration is not None else min(10.0, max(5.0, end - start))}
    scene.update(extra)
    return scene


def test_build_clip_scenes_from_scenario_covers_the_full_track():
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 8.0, "A lone figure walks a neon-lit city street."),
        _scenario_scene("chorus", 8.0, 16.0, "The figure looks up as fireworks bloom overhead."),
    ]
    scenes = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)
    _assert_scene_total_within_snap_tolerance(scenes, 16.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert [s["idx"] for s in scenes] == list(range(len(scenes)))
    assert "neon-lit city street" in scenes[0]["prompt"]
    assert "fireworks bloom" in scenes[1]["prompt"]
    for s in scenes:
        assert s["status"] == "pending"
        assert s["job_id"] is None and s["clip_path"] is None and s["keyframe_path"] is None


def test_build_clip_scenes_from_scenario_splits_a_section_longer_than_ten_seconds():
    scenario_scenes = [_scenario_scene("verse", 0.0, 23.0, "a single continuous shot of rain")]
    scenes = web.build_clip_scenes(_scenario_track(23.0), scenario_scenes=scenario_scenes)
    _assert_scene_total_within_snap_tolerance(scenes, 23.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert len(scenes) == 3  # ceil(23/10) == 3, same rule as the procedural path's own split
    for s in scenes:
        assert web.SCENE_MIN_SECONDS <= s["duration"] <= web.SCENE_MAX_SECONDS
    # one section prompt shared across pieces; pieces after the first say they continue the shot
    assert scenes[0]["prompt"] == "a single continuous shot of rain"
    for s in scenes[1:]:
        assert s["prompt"] == "a single continuous shot of rain" + web.SPLIT_CONTINUATION_CLAUSE


def test_build_clip_scenes_from_scenario_folds_a_short_trailing_section_into_its_neighbour():
    """Mirrors `test_build_clip_scenes_merges_a_short_trailing_section_into_its_neighbour` (the
    procedural path's own test) almost exactly -- proof this mode reuses the same
    `_fold_short_segments(..., SCENE_MIN_SECONDS)` call, not a reimplementation."""
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 10.0, "prompt A"),
        _scenario_scene("outro", 10.0, 12.0, "prompt B"),
    ]
    scenes = web.build_clip_scenes(_scenario_track(12.0), scenario_scenes=scenario_scenes)
    _assert_scene_total_within_snap_tolerance(scenes, 12.0)
    _assert_scene_durations_on_h3_grid(scenes)
    assert len(scenes) == 2  # the 2s outro folds into the verse, the merged 12s re-splits into two
    assert scenes[0]["prompt"] == "prompt A"
    assert scenes[1]["prompt"] == "prompt A" + web.SPLIT_CONTINUATION_CLAUSE


def test_build_clip_scenes_from_scenario_glues_style_block_verbatim_onto_every_scene():
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 8.0, "prompt A"),
        _scenario_scene("chorus", 8.0, 16.0, "prompt B"),
    ]
    scenes = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes,
                                    style_block="A hand-drawn watercolor music video")
    for s in scenes:
        assert ("Visual style, identical in every scene: A hand-drawn watercolor music video"
               in s["prompt"])


def test_build_clip_scenes_from_scenario_does_not_double_glue_a_style_block_the_model_already_copied():
    """I3, fix round 2 (2026-08-19 review): `docs/h3-prompt-system.md` tells the LLM to copy
    `style_block` verbatim into every `scene.prompt` itself. When a scene's prompt already carries
    it (the documented, intended shape of a real LLM reply), `_scenario_segments` must not glue
    `_style_clause` on top a second time -- checked by *counting occurrences* of the style text in
    the built prompt, not merely that the prompt is non-empty or "changed somehow", so a mutant that
    always glues (or never checks) is caught either way.
    """
    block = "A hand-drawn watercolor music video"
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 8.0,
                         f"A lone figure walks a neon street. {block}."),
        _scenario_scene("chorus", 8.0, 16.0,
                         f"Fireworks bloom overhead. {block}."),
    ]
    scenes = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes,
                                    style_block=block)
    for s in scenes:
        assert s["prompt"].count(block) == 1, s["prompt"]


def test_build_clip_scenes_from_scenario_still_glues_a_style_block_the_model_forgot():
    """The other half of I3: a scenario whose prompt does *not* already carry `style_block`
    verbatim (the model forgot, or this is the procedural fallback with a caller-supplied
    `style_block`) must still get the clause glued on -- the fix narrows the glue to a duplicate
    check, it does not remove the insurance `docs/h3-prompt-system.md`'s "same words" rule needs."""
    block = "A hand-drawn watercolor music video"
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "A lone figure walks a neon street.")]
    scenes = web.build_clip_scenes(_scenario_track(8.0), scenario_scenes=scenario_scenes,
                                    style_block=block)
    assert scenes[0]["prompt"].count(block) == 1
    assert f"Visual style, identical in every scene: {block}." in scenes[0]["prompt"]


def test_build_clip_scenes_from_scenario_without_a_style_block_adds_no_clause():
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "prompt A")]
    scenes = web.build_clip_scenes(_scenario_track(8.0), scenario_scenes=scenario_scenes)
    assert scenes[0]["prompt"] == "prompt A"


def test_build_clip_scenes_from_scenario_never_defaults_style_block_from_caption():
    """The procedural path's own `style_block is None -> _clip_style_block(caption)` default is
    specific to that path's caption-derived placeholder -- from_scenario mode must never silently
    pull that unrelated text in just because `style_block` was left `None`."""
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "prompt A")]
    scenes = web.build_clip_scenes(
        _scenario_track(8.0, caption="Wistful synthwave ballad."), scenario_scenes=scenario_scenes)
    assert "Wistful synthwave ballad" not in scenes[0]["prompt"]
    assert scenes[0]["prompt"] == "prompt A"


def test_build_clip_scenes_from_scenario_ignores_track_sections_lyrics_and_caption():
    """Proof this mode never reads `track["sections"]`/`track["lyrics"]` at all: garbage values
    that would crash the procedural path do not affect from_scenario mode."""
    track = {"duration": 8.0, "sections": "not even a list", "lyrics": 12345, "caption": None}
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "prompt A")]
    scenes = web.build_clip_scenes(track, scenario_scenes=scenario_scenes)
    assert scenes[0]["prompt"] == "prompt A"


# -- fresh_start (P0 fix, keyframe-chain defect 2026-08-25) --------------------------------------


def test_build_clip_scenes_from_scenario_carries_fresh_start_onto_the_built_scene():
    """`docs/h3-prompt-system.md`'s "Breaking the chain on a cast change" -- a section stamped
    `fresh_start: true` must reach `assemble._submit_next_scene` as `scenes[i]["fresh_start"] ==
    True`, and a section that leaves it `false` must reach it as `False`, not merely "truthy" --
    pinned per-scene so a mutant that ORs every scene's own flag together (or always returns the
    first section's value) is caught.
    """
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 8.0, "Aldred and the baby in the hall"),
        _scenario_scene("chorus", 8.0, 16.0, "Aldred alone, the baby gone", fresh_start=True),
    ]
    scenes = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)
    assert scenes[0]["fresh_start"] is False
    assert scenes[1]["fresh_start"] is True


def test_build_clip_scenes_from_scenario_defaults_fresh_start_to_false_when_absent():
    """A scenario section with no `fresh_start` key at all (every scenario written before this
    field existed, or a model turn that never mentions it) must build a scene with `fresh_start ==
    False` -- not `None`, not a missing key `assemble._submit_next_scene`'s own `.get()` would
    have tolerated anyway, but the exact value an explicit `false` would have produced.
    """
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "prompt A")]
    assert "fresh_start" not in scenario_scenes[0]
    scenes = web.build_clip_scenes(_scenario_track(8.0), scenario_scenes=scenario_scenes)
    assert scenes[0]["fresh_start"] is False


def test_build_clip_scenes_from_scenario_split_keeps_fresh_start_only_on_the_first_piece():
    """A section long enough to split (`_split_long_segment`) that also carries `fresh_start:
    true` must not repeat the chain break inside itself -- all pieces are the same section, the
    same composition, cut only because H3's own 10s ceiling forced it. Only the earliest piece
    (where the cast/location actually changed) keeps `fresh_start`; every later piece still chains
    an i2v keyframe off the piece immediately before it.
    """
    scenario_scenes = [_scenario_scene("verse", 0.0, 23.0, "a single continuous shot of rain",
                                       fresh_start=True)]
    scenes = web.build_clip_scenes(_scenario_track(23.0), scenario_scenes=scenario_scenes)
    assert len(scenes) == 3
    assert scenes[0]["fresh_start"] is True
    assert scenes[1]["fresh_start"] is False
    assert scenes[2]["fresh_start"] is False


def test_build_clip_scenes_from_scenario_fresh_start_survives_the_min_length_fold():
    """M1 (review round 2, 2026-08-26): a section between `SCENE_MIN_SECONDS -
    _COVERAGE_TOLERANCE_SECONDS` (4.95s) and `SCENE_MIN_SECONDS` (5.0s) clears `_validate_
    scenario_scenes`'s own refusal (it only rejects strictly under 4.95s) but is still short
    enough for `build_clip_scenes`'s own unconditional final `_fold_short_segments(...,
    SCENE_MIN_SECONDS)` pass to fold it into its neighbour -- see `_validate_scenario_scenes`'s
    own docstring for the honest account of that band. A section's own PROMPT is expected to be
    lost there (the fold keeps the survivor's own fields, same as any other fold) -- but its
    `fresh_start` must not be, or a cast change flagged on a section this short would silently
    resurrect the exact keyframe-chain defect this field exists to close.

    Three sections: 0 (survives untouched), 1 (survives, absorbs section 2), 2 (4.97s, `fresh_
    start: true`, folds backward into section 1). Section 2's own PROMPT must be gone (folded
    away); its `fresh_start` must have landed on the built scene section 1 folded into.
    """
    scenario_scenes = [
        _scenario_scene("zero", 0.0, 5.0, "prompt zero"),
        _scenario_scene("one", 5.0, 10.0, "prompt one"),
        _scenario_scene("two", 10.0, 14.97, "prompt two", duration=4.97, fresh_start=True),
    ]
    scenes = web.build_clip_scenes(_scenario_track(14.97), scenario_scenes=scenario_scenes)

    assert len(scenes) == 2, (
        "section two must have folded into section one, not survived as its own scene -- "
        f"got {len(scenes)} scenes")
    assert scenes[0]["fresh_start"] is False
    assert scenes[1]["fresh_start"] is True, (
        "section two's own fresh_start must survive the fold onto the scene it merged into")
    assert "prompt one" in scenes[1]["prompt"]
    assert "prompt two" not in scenes[1]["prompt"], (
        "the absorbed section's own prompt is expected to be lost -- only fresh_start must "
        "survive the fold, not the whole section")


def test_build_clip_scenes_from_scenario_fresh_start_survives_a_forward_carried_fold():
    """The other half of M1's fix: `_fold_short_segments`'s own forward-carry branch (a short,
    eligible segment folds INTO the next one when there is no earlier survivor to merge backward
    into yet -- the very first section(s) of a scenario). `carry_fresh_start` has to accumulate
    across that branch the same way `carry_start` already accumulates position, or a cast-change
    flag on a short opening section would be lost exactly like the backward-merge case this
    module's other test already covers. Not `idx`-realistic for `assemble.py`'s own `idx > 0`
    gate (the merged scene lands at idx 0 here) -- this test is about `_fold_short_segments`'s own
    mechanism in isolation, not about which scene index ends up carrying it.
    """
    scenario_scenes = [
        _scenario_scene("short", 0.0, 4.97, "prompt short", duration=4.97, fresh_start=True),
        _scenario_scene("long", 4.97, 10.0, "prompt long"),
    ]
    scenes = web.build_clip_scenes(_scenario_track(10.0), scenario_scenes=scenario_scenes)

    assert len(scenes) == 1, f"the short opening section must have folded forward -- got {scenes}"
    assert scenes[0]["fresh_start"] is True, (
        "the short section's own fresh_start must survive the forward-carry fold")
    assert "prompt long" in scenes[0]["prompt"]
    assert "prompt short" not in scenes[0]["prompt"]


def test_build_clip_scenes_procedural_path_never_sets_fresh_start():
    """The procedural path (`scenario_scenes=None`) has no concept of `fresh_start` at all -- every
    scene it builds must read `False`, the same as before this field existed anywhere in the
    codebase.
    """
    sections = [{"name": "verse", "start": 0.0, "end": 8.0},
                {"name": "chorus", "start": 8.0, "end": None}]
    scenes = web.build_clip_scenes(_track(sections, 16.0, _TWO_SECTION_LYRICS))
    assert scenes
    assert all(s["fresh_start"] is False for s in scenes)


def test_build_clip_scenes_from_scenario_refuses_a_track_with_no_measured_duration():
    scenario_scenes = [_scenario_scene("verse", 0.0, 8.0, "prompt A")]
    with pytest.raises(web.ProjectSceneBuildError, match="no measured duration"):
        web.build_clip_scenes(_scenario_track(None), scenario_scenes=scenario_scenes)


def test_build_clip_scenes_from_scenario_refuses_when_coverage_has_a_gap():
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 8.0, "prompt A"),
        _scenario_scene("chorus", 9.0, 16.0, "prompt B"),  # 1s gap between the two sections
    ]
    with pytest.raises(web.ProjectSceneBuildError, match="seam"):
        web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)


def test_build_clip_scenes_from_scenario_refuses_an_overlap_between_sections():
    scenario_scenes = [
        _scenario_scene("verse", 0.0, 9.0, "prompt A"),
        _scenario_scene("chorus", 8.0, 16.0, "prompt B"),  # overlaps the verse by 1s
    ]
    with pytest.raises(web.ProjectSceneBuildError, match="seam"):
        web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)


def test_build_clip_scenes_from_scenario_refuses_when_coverage_does_not_start_at_zero():
    scenario_scenes = [_scenario_scene("verse", 1.0, 16.0, "prompt A")]
    with pytest.raises(web.ProjectSceneBuildError, match="not 0.0s"):
        web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)


def test_build_clip_scenes_from_scenario_refuses_when_total_does_not_match_track_duration():
    scenario_scenes = [_scenario_scene("verse", 0.0, 10.0, "prompt A")]
    with pytest.raises(web.ProjectSceneBuildError, match=r"track is 16\.000s"):
        web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenario_scenes)


def test_build_clip_scenes_from_scenario_refuses_a_malformed_scene_entry():
    with pytest.raises(web.ProjectSceneBuildError, match="malformed"):
        web.build_clip_scenes(_scenario_track(8.0),
                              scenario_scenes=[{"tag": "verse", "prompt": "x"}])


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


# == Задача 6, волна «перенос латента»: у сцепленной сцены своя сетка длительностей ================
#
# `docs/FEASIBILITY-latent-handoff.md` §4.1: сцена, которая стартует с латентного хвоста
# предыдущей, ПОЛУЧАЕТ на `OVERLAP_PIXEL_FRAMES = 22` кадра больше, чем отдаёт в сборку --
# запрос `R = D + 22` обязан лежать на H3-сетке `17j + 5`, значит доставляемое
# `D = R - 22 = 17(j+1) - 17 - ... = 17k` кратно 17 БЕЗ остатка 5. Без этой правки каждая
# сцепленная сцена ночного прогона отдаёт на 0.917 с меньше обещанного (33 сцены * 0.917 =
# 30.3 с против `assemble.DURATION_TOLERANCE_SECONDS` = 0.5 с) -- сборка падает
# детерминированно.


def _night4_layout():
    """Реальная раскладка ночного прогона 4 -- 40 сцен, 7 из них `fresh_start`
    (`[0, 3, 5, 24, 25, 26, 28]`), трек 304.880 с. Геометрия снята с одобренного сценария
    (`tests/data/night4-scenario-layout.json`, см. его `_provenance`), промпты заменены
    заглушками: под тестом арифметика сетки, а не текст.
    """
    raw = json.loads((Path(__file__).resolve().parent / "data" /
                      "night4-scenario-layout.json").read_text(encoding="utf-8"))
    return raw["track_duration"], raw["scenario_scenes"]


def test_snap_scene_duration_puts_a_chained_scene_on_the_multiple_of_seventeen_grid():
    """(а) Прямой замер: одна и та же сырая длительность снапается на РАЗНЫЕ сетки в зависимости
    от того, стартует сцена с латентного хвоста или нет. 7.5 с сцепленной сцены -> 170 кадров
    (17*10), той же несцепленной -> 175 (17*10+5). Пинится именно значение, а не «оба на какой-то
    сетке»: сетка `17k+5` тоже «какая-то сетка», и мутация «сетка 17k+5 для всех» её бы прошла.
    """
    chained, chained_carry = web._snap_scene_duration(7.5, 0.0, chained=True)
    plain, plain_carry = web._snap_scene_duration(7.5, 0.0, chained=False)

    assert round(chained * 24) == 170
    assert round(chained * 24) % 17 == 0
    assert round(plain * 24) == 175
    assert round(plain * 24) % 17 == 5
    # carry -- остаток, который телескопится в следующую сцену; снап вниз, значит он ровно
    # «сколько эта сцена не добрала».
    assert chained_carry == pytest.approx(7.5 - 170 / 24, abs=1e-9)
    assert plain_carry == pytest.approx(7.5 - 175 / 24, abs=1e-9)


def test_build_clip_scenes_gives_a_chained_scenario_scene_the_multiple_of_seventeen_grid():
    """Тот же факт через публичную функцию: признак сцепленности (`idx > 0` И НЕ `fresh_start`)
    берётся на месте снапа в `build_clip_scenes`, а не приходит параметром снаружи.
    """
    duration = 21.0
    scenes = web.build_clip_scenes(_scenario_track(duration), scenario_scenes=[
        _scenario_scene("a", 0.0, 7.0, "первая сцена", fresh_start=True),
        _scenario_scene("b", 7.0, 14.0, "сцепленная сцена"),
        _scenario_scene("c", 14.0, 21.0, "разрыв цепочки", fresh_start=True),
    ])

    assert [s["fresh_start"] for s in scenes] == [True, False, True]
    assert round(scenes[1]["duration"] * 24) % 17 == 0, (
        f"сцена 1 сцеплена -- её доставляемая длительность обязана быть кратна 17, "
        f"получено {round(scenes[1]['duration'] * 24)} кадров")
    assert [round(s["duration"] * 24) for s in scenes] == [158, 170, 175]
    assert round(scenes[0]["duration"] * 24) % 17 == 5
    assert round(scenes[2]["duration"] * 24) % 17 == 5
    _assert_scene_durations_on_h3_grid(scenes)


def test_build_clip_scenes_covers_the_real_night_four_layout_within_tolerance():
    """(б) Покрытие на РЕАЛЬНОЙ раскладке ночи: 40 сцен, 33 сцепленных, 7 `fresh_start`, трек
    304.880 с. Ревью пересчитало недобор на этой раскладке в 0.255 с -- это ровно финальный
    `carry` телескопической суммы, и он обязан остаться внутри допусков (`web`
    `_SNAPPED_COVERAGE_SHORTFALL_SECONDS` = 1.0 + 0.05, `assemble.DURATION_TOLERANCE_SECONDS`
    = 0.5). Синтетика этого не ловит: провал появляется только на длинной цепочке, где ошибка
    сетки копится сценa за сценой.
    """
    duration, scenario_scenes = _night4_layout()

    scenes = web.build_clip_scenes(_scenario_track(duration), scenario_scenes=scenario_scenes)

    assert len(scenes) == 40, "раскладка ночи не должна ни складываться, ни резаться"
    assert [s["idx"] for s in scenes if s["fresh_start"]] == [0, 3, 5, 24, 25, 26, 28]
    _assert_scene_durations_on_h3_grid(scenes)
    shortfall = duration - sum(s["duration"] for s in scenes)
    assert shortfall == pytest.approx(0.255, abs=0.05), (
        f"недобор на ночной раскладке {shortfall:.3f} с -- ревью посчитало 0.255 с")
    assert shortfall < assemble_module.DURATION_TOLERANCE_SECONDS, (
        "недобор обязан лежать внутри допуска сборки, иначе ночь падает на "
        "_pad_with_freeze_frame")


def test_snap_scene_duration_clamps_a_short_chained_scene_up_onto_its_own_grid():
    """(в) КРАЙ: секция 5.0 с на сцепленной сетке. Ближайшая точка ВНИЗ -- 119 кадров (4.958 с),
    ниже `SCENE_MIN_SECONDS`, значит кламп толкает ВВЕРХ -- и обязан толкнуть на сцепленную
    сетку (136 = 17*8), а не на 141 (17*8+5) чужой сетки. `carry` при этом уходит в МИНУС: сцена
    забрала больше, чем ей было отпущено, и следующая обязана это увидеть.
    """
    snapped, carry = web._snap_scene_duration(5.0, 0.0, chained=True)

    assert round(snapped * 24) == 136, (
        f"кламп снизу обязан лечь на сцепленную сетку 17k, получено {round(snapped * 24)} кадров")
    assert snapped >= web.SCENE_MIN_SECONDS
    assert carry < 0, "перерасход обязан уехать в следующую сцену минусом, а не испариться"
    assert carry == pytest.approx(5.0 - 136 / 24, abs=1e-9)


def test_build_clip_scenes_absorbs_a_short_chained_scenes_negative_carry(tmp_path):
    """(в), продолжение: тот же край в живом построении. Утверждается ФАКТИЧЕСКОЕ поведение --
    сцены строятся, минусовой carry гасится следующей сценой, покрытие сходится ТОЧНО, а не
    молча портится.
    """
    duration = 25.0
    scenes = web.build_clip_scenes(_scenario_track(duration), scenario_scenes=[
        _scenario_scene("a", 0.0, 10.0, "первая сцена", fresh_start=True),
        _scenario_scene("b", 10.0, 15.0, "короткая сцепленная"),
        _scenario_scene("c", 15.0, 25.0, "длинная сцепленная"),
    ])

    assert [round(s["duration"] * 24) for s in scenes] == [226, 136, 238]
    _assert_scene_durations_on_h3_grid(scenes)
    assert sum(s["duration"] for s in scenes) == pytest.approx(duration, abs=1e-9), (
        "минусовой carry короткой сцены обязан быть списан со следующей, а не добавлен к треку")


def test_build_clip_scenes_refuses_when_a_clamped_chained_scene_overshoots_the_track():
    """(в), вторая половина: если гасить перерасход нечем (сцепленная сцена под клампом стоит
    последней), суммарная длительность вылезает ЗА трек -- и это честный отказ
    `ProjectSceneBuildError`, а не молча отгруженный таймлайн, который сборка потом не сможет
    подрезать (`assemble.run` умеет только доклеивать freeze-frame).
    """
    with pytest.raises(web.ProjectSceneBuildError, match="frame grid"):
        web.build_clip_scenes(_scenario_track(15.0), scenario_scenes=[
            _scenario_scene("a", 0.0, 10.0, "первая сцена", fresh_start=True),
            _scenario_scene("b", 10.0, 15.0, "короткая сцепленная"),
        ])


def test_web_does_not_import_mlx_or_the_pipeline_for_the_chained_grid():
    """Контракт модуля (`web.py`'s own docstring: "No `mlx` import, ever"): константы сетки
    сцепленной сцены обязаны быть СВОИ, а не импортированные из `h3_48gb.pipeline`, который на
    уровне модуля тянет `mlx.core`. Смотрятся именно строки импорта (в комментариях и докстрингах
    `h3_48gb.pipeline` упоминается законно и часто).
    """
    imports = [line.strip() for line in
               (Path(web.__file__)).read_text(encoding="utf-8").splitlines()
               if line.strip().startswith(("import ", "from "))]
    assert not [ln for ln in imports if "mlx" in ln], imports
    assert not [ln for ln in imports if "pipeline" in ln], imports
    assert web._SCENE_LATENT_OVERLAP_FRAMES == 22
    assert web._CHAINED_GRID_REMAINDER == 0


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


def _clip_project_with_approved_track(srv, monkeypatch, *, duration=16.0) -> str:
    """A `kind="clip"` project (lyrics given, `track_source="generate"`) taken all the way through
    `approve/script` and a faked `songrun.run_song` to `stages.track == "approved"` -- the shared
    starting point every scenario-gate test below needs (task 4, "Сюжет клипа" wave): `stages.
    scenario` sits at `"draft"`, `scenario_scenes` is `[]`, and `track.duration == duration`, ready
    for `/scenario/generate`/`PUT /scenario`/`approve/scenario`.
    """
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    fake_result = sr.SongResult(
        wav=Path("song.wav"), mastered_wav=Path("song.mastered.wav"), mp3=Path("song.mp3"),
        mastered_mp3=Path("song.mastered.mp3"), duration=duration, transcript="ла ла ла",
        sections=[{"name": "verse", "start": 0.0, "end": duration}], undersung=False)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)
    job = q.claim(srv.queue_root)
    assert job.kind == q.KIND_SONG
    code = worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    assert code == 0
    srv.post_json(f"/api/projects/{pid}/approve/track", {})
    return pid


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


def test_approve_script_for_an_imported_clip_estimates_the_song_job_from_the_files_own_duration(
        _serve, tmp_path):
    """Task 4 ("Сюжет клипа" wave): an imported track's own song job only ever runs Whisper
    transcription (`songrun.align_track`), never Music3 generation -- pricing it with `song_job_
    wallclock_estimate_seconds` (task 1 report, "сомнение 2": a flat 15s no matter how long the
    file actually runs) was dishonest. `worker.align_job_wallclock_estimate_seconds` reads the
    uploaded file's own real `ffprobe` duration instead -- a real ffmpeg-encoded mp3, not the
    garbage `_MP3_BYTES` placeholder every other test here uploads, since a real duration is
    exactly the thing under test.
    """
    srv = _serve()
    wav = tmp_path / "sine.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=45", str(wav)], check=True,
                   capture_output=True)
    mp3 = tmp_path / "sine.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(mp3)], check=True,
                   capture_output=True)
    upload = srv.upload_raw(mp3.read_bytes(), "song.mp3")[1]
    pid = srv.post_json(
        "/api/projects",
        {"kind": "clip", "track_source": "import", "track_path": upload["path"]})["id"]
    approved = srv.post_json(f"/api/projects/{pid}/approve/script", {})
    assert "job_id" in approved["submit"]

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    seconds = pending[0].estimate["seconds"]
    # ~45s * ALIGN_WALLCLOCK_FACTOR (~1/15) + a 5s pad = ~8s -- nowhere near the flat 15.0s the
    # old lyrics-based formula gave an empty-lyric import, and nowhere near a *generated* take's
    # own price either (song_job_wallclock_estimate_seconds("") alone would answer exactly 15.0).
    assert 5.0 < seconds < 15.0


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


def test_approve_track_for_a_migrated_project_survives_a_failed_scene_build_and_can_be_retried(
        _serve):
    """C1 (fix round 1, 2026-08-19 review), still exercised at `approve/track` **for the
    migration case only** (task 4, "Сюжет клипа" wave): `approve_stage` must run *after*
    `build_clip_scenes` actually succeeds, not before it, for the one clip project shape that
    still builds scenes straight off `approve/track` -- a `project.json` written before the
    scenario stage existed (`stages.scenario` migrated to `"approved"` on load, `scenario_scenes`
    still empty; see `_approve_project_stage`'s own docstring). Before the original C1 fix, a
    failed build left `stages.track` stuck `"approved"` forever with no scenes ever built and no
    way back in -- a repeat `approve/track` answered 409 (the stage was no longer `awaiting_
    approval`), and there is no separate "retry the track build" endpoint, only `scenes/<idx>/
    retry`, which needs a scene to already exist.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    proj = project_module.load_project(project_path)
    # Force the migration shape by hand: a fresh project's own `stages.scenario` starts "draft",
    # never "approved" this early -- only a `project.json` written before the scenario stage
    # existed reaches `approve/track`'s old procedural branch, and there is no ordinary route that
    # produces one in a fresh test project.
    proj.set_stage_status("scenario", "approved")
    # No `duration` set yet -- `build_clip_scenes` must refuse (the worker normally sets it,
    # Task 3's I4; forcing the gate open by hand is the only way to reach this state through the
    # routes, same technique `test_approve_scenario_for_a_clip_project_with_an_unmeasured_track_
    # is_refused_honestly` already uses).
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


def test_approve_track_for_a_fresh_clip_project_never_builds_scenes(_serve):
    """Task 4, "Сюжет клипа" wave: the *ordinary* (non-migrated) case, the mirror image of the
    test above -- a fresh clip project's `stages.scenario` starts `"draft"`, so `approve/track`
    takes the other branch entirely: no `build_clip_scenes` call at all, no `advance`, `scenes`
    stays `[]`. Approving `track` succeeds even with **no measured `duration`**, because nothing
    here reads it any more -- that check moved to the scenario gate (`_scenario_gate_project`,
    `approve/scenario`).
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    proj = project_module.load_project(Path(srv.root) / "projects" / pid / "project.json")
    proj.set_stage_status("track", "awaiting_approval")

    approved = srv.post_json(f"/api/projects/{pid}/approve/track", {})
    assert "advance" not in approved
    assert approved["project"]["stages"]["track"] == "approved"
    assert approved["project"]["stages"]["scenario"] == "draft"
    assert approved["project"]["scenes"] == []


def test_approve_scenario_survives_a_failed_scene_build_and_can_be_retried(_serve, monkeypatch):
    """C1's own new home (task 4, "Сюжет клипа" wave): scene-building moved from `approve/track`
    to `approve/scenario` for every ordinary (non-migrated) clip project, so the same "side effect
    before `approve_stage`" discipline has to hold there now. A scenario whose sections do not
    actually tile the track (forced by hand -- the only way to reach this state without a real
    LLM reply that disagrees with its own gate's later validation) must leave `stages.scenario`
    retryable, not stuck `"approved"` with no scenes and no way back in.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    proj = project_module.load_project(project_path)
    # A scenario that covers only half the track -- `_validate_scenario_scenes` (the honest
    # routes) would refuse this outright, so reaching `approve/scenario` with it on file needs a
    # direct write, bypassing `PUT /scenario`'s own validation entirely.
    proj.update_scenario(scenario_scenes=[
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "prompt A", "duration": 8.0}])
    proj.set_stage_status("scenario", "awaiting_approval")

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/scenario", {})
    assert (status, payload["error"]["code"]) == (400, "project_scene_build_failed"), payload

    stuck = srv.get_json(f"/api/projects/{pid}")["project"]
    assert stuck["stages"]["scenario"] == "awaiting_approval", (
        "a failed build must not leave the project stuck 'approved' with no scenes and no retry "
        "path -- the same gate must still be open")
    assert stuck["scenes"] == []

    # Fix the scenario (a full-coverage one this time) and retry the *same* gate.
    proj2 = project_module.load_project(project_path)
    proj2.update_scenario(scenario_scenes=[
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "prompt A", "duration": 8.0},
        {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "prompt B", "duration": 8.0}])
    retried = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    assert retried["project"]["stages"]["scenario"] == "approved"
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

    def fake_assemble_run(project_path, *, run=None, log=None):
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

    def fake_assemble_run(project_path, *, run=None, log=None):
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


# == Full lifecycle: clip project with an imported track, through the scenario gate ===============


def test_clip_project_with_an_imported_track_goes_through_the_scenario_gate(_serve, monkeypatch):
    """Task 4 ("Сюжет клипа" wave)'s own rewrite of this lifecycle test: `approve/track` no longer
    builds scenes for an ordinary clip project -- it only unblocks the scenario gate. Scenes are
    built at `approve/scenario` instead, once a scenario (here, the `{"procedural": true}`
    fallback -- no LLM needed) has been generated and approved.
    """
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
    assert "advance" not in approved_track, (
        "task 4: approve/track for an ordinary clip project no longer builds scenes")
    assert approved_track["project"]["scenes"] == []
    assert approved_track["project"]["stages"]["scenario"] == "draft"

    generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    assert generated["project"]["stages"]["scenario"] == "awaiting_approval"
    scenario_scenes = generated["project"]["scenario_scenes"]
    assert scenario_scenes
    assert scenario_scenes[0]["start"] == pytest.approx(0.0)
    assert scenario_scenes[-1]["end"] == pytest.approx(16.0)
    assert generated["project"]["scenario_style_block"] is None, (
        "the procedural fallback bakes its style clause into each prompt directly -- a separate "
        "style_block here would glue the clause on a second time at approve/scenario")

    approved_scenario = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    assert approved_scenario["advance"]["action"] == "submitted_scene"
    scenes = approved_scenario["project"]["scenes"]
    assert len(scenes) >= 1
    _assert_scene_total_within_snap_tolerance(scenes, 16.0)
    _assert_scene_durations_on_h3_grid(scenes)

    jobs, _broken = q.scan(srv.queue_root)
    pending = [j for j in jobs if j.state == "pending"]
    assert len(pending) == 1
    assert pending[0].note == assemble_module.scene_note(pid, 0)


def test_approve_scenario_for_a_clip_project_with_an_unmeasured_track_is_refused_honestly(_serve):
    """Defensive: a track cannot reach `stages.track == "approved"` without `duration` being set
    (the worker always writes it, Task 3's I4), but if it somehow did, `build_clip_scenes` refuses
    with a named code rather than a 500 -- checked here directly against the project's own methods
    (task 4 moved this check from `approve/track` to `approve/scenario`, along with scene
    building itself), since there is no ordinary way to reach this state through the routes alone.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    proj = project_module.load_project(Path(srv.root) / "projects" / pid / "project.json")
    proj.update_scenario(scenario_scenes=[
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "x", "duration": 8.0}])
    proj.set_stage_status("scenario", "awaiting_approval")

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/approve/scenario", {})
    assert (status, payload["error"]["code"]) == (400, "project_scene_build_failed"), payload


# == Task 4 ("Сюжет клипа" wave): the scenario gate itself =========================================
#
# `POST .../scenario/generate` (LLM or `{"procedural": true}`), `PUT .../scenario` (hand edits)
# and `approve/scenario` (its full-lifecycle happy path already covered above, plus its own C1 and
# "unmeasured track" tests) -- gates, validation, the LLM round trip through the same `_FakeLlama`
# mock `tests/test_chat_web.py` already uses, and the migration path for a `project.json` written
# before this stage existed.


def _scenario_section(tag, start, end, prompt, duration=8):
    return {"tag": tag, "start": start, "end": end, "scene": {"prompt": prompt, "duration": duration}}


def _scenario_turn_payload(sections, style_block="A neon-lit stage, warm haze, handheld camera."):
    scenario = {"sections": sections, "style_block": style_block}
    return {"choices": [{"message": {"content": json.dumps(
        {"reply": "вот сюжет", "scenario": scenario})}}]}


# -- gates: kind, track approval, scenario's own current status ----------------------------------


def test_generate_scenario_for_a_video_project_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_generate_scenario_before_track_is_approved_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_generate_scenario_after_the_scenario_gate_is_approved_is_refused(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    srv.post_json(f"/api/projects/{pid}/approve/scenario", {})

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_edit_scenario_for_a_video_project_is_refused(_serve):
    srv = _serve()
    sid = _new_session_with_project(srv, _project_body(kind="video", scenes=_VIDEO_SCENES))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {"scenario_scenes": []})
    assert (status, payload["error"]["code"]) == (409, "project_stage_not_ready")


def test_edit_scenario_after_approval_is_refused_with_its_own_code(_serve, monkeypatch):
    """The one refusal `PUT /scenario` names differently from `/scenario/generate`'s own
    `project_stage_not_ready` for the identical state -- task 4 brief, verbatim: "409
    scenario_already_approved после утверждения"."""
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    srv.post_json(f"/api/projects/{pid}/approve/scenario", {})

    status, payload = srv._request(
        "PUT", f"/api/projects/{pid}/scenario",
        {"scenario_scenes": [{"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "x",
                              "duration": 8.0}]})
    assert (status, payload["error"]["code"]) == (409, "scenario_already_approved"), payload


# -- procedural fallback ("сюжет без LLM") --------------------------------------------------------


def test_generate_scenario_procedural_needs_no_provider_at_all(_serve, monkeypatch):
    """`{"procedural": true}` never touches `provider`/`ensure_up` -- an empty roster (no
    `providers.json` at all) must not stand in its way, unlike the LLM path."""
    srv = _serve(roster=False)
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    assert generated["project"]["stages"]["scenario"] == "awaiting_approval"
    scenes = generated["project"]["scenario_scenes"]
    assert scenes[0]["start"] == pytest.approx(0.0)
    assert scenes[-1]["end"] == pytest.approx(16.0)
    for scene in scenes:
        assert scene["end"] - scene["start"] >= web.SCENE_MIN_SECONDS - 0.01


def test_generate_scenario_procedural_rejects_a_bad_procedural_type(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate",
                                        {"procedural": "yes"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


# -- the LLM round trip ----------------------------------------------------------------------------


def test_generate_scenario_from_an_llm_reply_opens_the_gate(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 8.0, "[Shot 1] wide shot, dusk street."),
        _scenario_section("chorus", 8.0, 16.0, "[Shot 1] rooftop, fireworks."),
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()

    assert generated["project"]["stages"]["scenario"] == "awaiting_approval"
    scenes = generated["project"]["scenario_scenes"]
    assert len(scenes) == 2
    # `state_in`/`state_out` land as empty strings for a reply that carried no passport at all
    # (this fixture's own sections do not) -- the retro contract: a scenario written before the
    # passport existed is stored, built and rendered byte for byte as it was.
    assert scenes[0] == {"tag": "verse", "start": 0.0, "end": 8.0,
                         "prompt": "[Shot 1] wide shot, dusk street.", "duration": 8.0,
                         "fresh_start": False, "state_in": "", "state_out": ""}
    assert generated["project"]["scenario_style_block"].startswith("A neon-lit stage")

    (req,) = fake.requests
    assert req["body"]["response_format"]["json_schema"] == provider.SCENARIO_SCHEMA


def test_generate_scenario_sends_lyrics_when_they_are_non_empty(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "a"),
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        srv.post_json(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    (req,) = fake.requests
    user_message = req["body"]["messages"][-1]["content"]
    assert "lyrics:" in user_message
    assert _TWO_SECTION_LYRICS.strip() in user_message
    assert "raw transcript" not in user_message


def test_generate_scenario_sends_the_auto_transcript_when_there_are_no_lyrics(_serve, monkeypatch):
    """Task 4 brief, verbatim: "lyrics непустая -> она; иначе lyrics_auto" -- an imported track
    with no reference lyrics sends its raw Whisper segments instead."""
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("scene-0", 0.0, 10.0, "a"),
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
        pid = srv.post_json(
            "/api/projects",
            {"kind": "clip", "track_source": "import", "track_path": upload["path"]})["id"]
        srv.post_json(f"/api/projects/{pid}/approve/script", {})
        imported_mp3 = Path(upload["path"])
        fake_result = sr.SongResult(
            wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3,
            mastered_mp3=imported_mp3, duration=10.0,
            transcript="hello there my friend", sections=[], undersung=False,
            raw_segments=[{"start": 0.0, "end": 4.0, "text": "hello there"},
                         {"start": 4.0, "end": 10.0, "text": "my friend"}])
        monkeypatch.setattr(sr, "align_track", lambda *a, **kw: fake_result)
        job = q.claim(srv.queue_root)
        worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
        srv.post_json(f"/api/projects/{pid}/approve/track", {})

        srv.post_json(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    (req,) = fake.requests
    user_message = req["body"]["messages"][-1]["content"]
    assert "raw transcript with timestamps" in user_message
    assert "hello there" in user_message
    assert "lyrics:" not in user_message


def test_generate_scenario_with_nothing_to_write_from_is_refused(_serve, monkeypatch):
    """An instrumental import: Whisper transcribed nothing, and there was never any reference
    lyrics either -- `scenario_no_lyrics`, not a 502 from a provider that was never even called."""
    srv = _serve()
    upload = srv.upload_raw(_MP3_BYTES, "song.mp3")[1]
    pid = srv.post_json(
        "/api/projects",
        {"kind": "clip", "track_source": "import", "track_path": upload["path"]})["id"]
    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    imported_mp3 = Path(upload["path"])
    fake_result = sr.SongResult(
        wav=imported_mp3, mastered_wav=imported_mp3, mp3=imported_mp3, mastered_mp3=imported_mp3,
        duration=10.0, transcript="", sections=[], undersung=False, raw_segments=[])
    monkeypatch.setattr(sr, "align_track", lambda *a, **kw: fake_result)
    job = q.claim(srv.queue_root)
    worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)
    srv.post_json(f"/api/projects/{pid}/approve/track", {})

    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (400, "scenario_no_lyrics"), payload


def test_generate_scenario_provider_unavailable(_serve, monkeypatch):
    srv = _serve(roster=False)
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable"), payload


# == Task 2 ("выбор провайдера для сценария"): `provider` in the body ==============================


def _two_provider_roster(active_port, other_port):
    """Two `openai` entries, no `api_key_env` (both `available`), on two distinct fake ports --
    `active` is the roster's default, `other` is the one a request can ask for explicitly."""
    return {
        "active": {"type": "openai", "base_url": f"http://127.0.0.1:{active_port}", "model": "m"},
        "other": {"type": "openai", "base_url": f"http://127.0.0.1:{other_port}", "model": "m"},
    }


def test_generate_scenario_with_an_explicit_provider_goes_to_it_not_the_active_one(_serve,
                                                                                    monkeypatch):
    """Checked by what actually answered, not by the HTTP status: the two fakes write two
    different scene prompts, and only the one named in the body may be the source of the scene
    that comes back -- the active fake must not have seen a single request."""
    fake_active = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "active must never be asked"),
    ]))
    fake_other = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "the explicitly chosen provider wrote this"),
    ]))
    try:
        srv = _serve(providers=_two_provider_roster(fake_active.port, fake_other.port),
                     active="active")
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"provider": "other"})
    finally:
        fake_active.close()
        fake_other.close()
    assert fake_active.requests == [], "активный провайдер получил запрос, хотя выбрали другого"
    (req,) = fake_other.requests
    assert generated["project"]["scenario_scenes"][0]["prompt"] == (
        "the explicitly chosen provider wrote this")


def test_generate_scenario_without_a_provider_still_uses_the_active_one(_serve, monkeypatch):
    """The mirror of the test above: no `provider` in the body, and the *other* fake -- present
    in the roster but not active -- must stay untouched."""
    fake_active = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "active answered as usual"),
    ]))
    fake_other = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "other must never be asked"),
    ]))
    try:
        srv = _serve(providers=_two_provider_roster(fake_active.port, fake_other.port),
                     active="active")
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake_active.close()
        fake_other.close()
    assert fake_other.requests == [], "провайдер, который не выбирали, получил запрос"
    assert generated["project"]["scenario_scenes"][0]["prompt"] == "active answered as usual"


def test_generate_scenario_with_an_unknown_provider_name_is_refused_before_any_side_effect(
        _serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    status, payload = srv.post_json_raw(
        f"/api/projects/{pid}/scenario/generate", {"provider": "does-not-exist"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload
    assert payload["error"]["detail"]["provider"] == "does-not-exist"
    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert detail["stages"]["scenario"] == "draft", "a rejected name must not open the gate"
    assert detail["scenario_scenes"] == [], "a rejected name must not write any scenes"


def test_generate_scenario_with_an_explicit_but_token_less_provider_is_refused_before_the_model(
        _serve, monkeypatch):
    """Named explicitly, known to the roster, but `available is False` (no token) -- a 409 with
    its own reason before any network call, and the active fake (present and reachable) must not
    have been asked either: the human picked a *specific* unusable provider, not "whatever is
    active"."""
    fake_active = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "must not be asked"),
    ]))
    try:
        providers = {
            "active": {"type": "openai", "base_url": f"http://127.0.0.1:{fake_active.port}",
                      "model": "m"},
            "needs-token": {"type": "openai", "base_url": "http://127.0.0.1:1", "model": "m",
                           "api_key_env": "OPENROUTER_API_KEY"},
        }
        srv = _serve(providers=providers, active="active")
        pid = _clip_project_with_approved_track(srv, monkeypatch)
        status, payload = srv.post_json_raw(
            f"/api/projects/{pid}/scenario/generate", {"provider": "needs-token"})
    finally:
        fake_active.close()
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable"), payload
    assert "OPENROUTER_API_KEY" in payload["error"]["message"]
    assert fake_active.requests == [], "неверно выбранный провайдер не должен трогать активного"


def test_generate_scenario_ignores_a_provider_named_alongside_procedural(_serve, monkeypatch):
    """`{"procedural": true, "provider": "..."}`: no model is ever called in the procedural
    branch, so the name is accepted (not `args_invalid`) and simply never used -- proven here by
    naming a provider the roster does not even contain, which would be `args_invalid` on the LLM
    path (see the test above) but must succeed here."""
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    status, payload = srv.post_json_raw(
        f"/api/projects/{pid}/scenario/generate",
        {"procedural": True, "provider": "does-not-exist-either"})
    assert status == 200, payload
    assert payload["project"]["stages"]["scenario"] == "awaiting_approval"


def test_generate_scenario_with_no_provider_listening_answers_chat_unreachable(_serve,
                                                                                monkeypatch):
    """An external (`type: "openai"`) provider on a closed port -- not a `llama-local` one, whose
    `ensure_up` would spend up to 90s spawning and polling before giving up with a *different*
    code (`llama_did_not_start`); this is `test_chat_web.py`'s own
    `test_a_provider_that_does_not_answer_keeps_its_own_code_at_the_http_boundary` shape, reused
    here for the same fast, deterministic `chat_unreachable`."""
    srv = _serve(providers=_external(1), active="openrouter")  # port 1: nobody ever listens there
    pid = _clip_project_with_approved_track(srv, monkeypatch)
    status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    assert (status, payload["error"]["code"]) == (502, "chat_unreachable"), payload


def test_generate_scenario_refuses_a_malformed_llm_reply_as_bad_model_json(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": json.dumps(
        {"reply": "не смог", "scenario": None})}}]})
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch)
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (502, "bad_model_json"), payload
    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert detail["stages"]["scenario"] == "draft", "a bad reply must not open the gate"


def test_generate_scenario_refuses_a_reply_cut_by_the_providers_own_output_limit(_serve,
                                                                                  monkeypatch):
    """The live bug this fix round closes: caila.io's `claude-opus-5`, with no `max_tokens` sent,
    spent its whole (small, provider-default) output budget reasoning and answered
    `finish_reason: "length"` with an empty `content` -- a full 19-section scenario is exactly the
    shape of reply big enough to hit this. That must reach the page as `chat_truncated`, not
    `bad_model_json` (a genuinely different failure this same empty-content shape used to be
    mistaken for), and must leave the scenario gate exactly where the malformed-reply case above
    does: `draft`, no scenes written.
    """
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch)
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (502, "chat_truncated"), payload
    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert detail["stages"]["scenario"] == "draft", "a truncated reply must not open the gate"
    assert detail["scenario_scenes"] == []


# -- the python validation jsonschema/grammar-constrained decoding cannot express -----------------


def test_generate_scenario_refuses_a_gap_in_coverage(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 7.0, "a"),
        _scenario_section("chorus", 8.0, 16.0, "b"),  # 1s gap between the two
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (400, "scenario_invalid"), payload
    assert payload["error"]["detail"]["reason"] == "gap_or_overlap"
    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert detail["stages"]["scenario"] == "draft", "invalid content must not open the gate"


def test_generate_scenario_refuses_an_overlap_in_coverage(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 9.0, "a"),
        _scenario_section("chorus", 8.0, 16.0, "b"),  # overlaps the verse by 1s
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (400, "scenario_invalid"), payload
    assert payload["error"]["detail"]["reason"] == "gap_or_overlap"


def test_generate_scenario_refuses_a_section_shorter_than_five_seconds(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 3.0, "a"),
        _scenario_section("chorus", 3.0, 16.0, "b"),
    ]))
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        status, payload = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (400, "scenario_invalid"), payload
    assert payload["error"]["detail"]["reason"] == "section_too_short"
    assert payload["error"]["detail"]["index"] == 0


# -- PUT /scenario: hand edits, the same validation on every call ---------------------------------


def test_edit_scenario_updates_prompts_durations_and_the_style_block(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "hand-edited prompt A",
             "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "hand-edited prompt B",
             "duration": 7.0},
        ],
        "style_block": "A single lantern-lit room.",
    })
    assert status == 200, edited
    scenes = edited["project"]["scenario_scenes"]
    assert scenes[0]["prompt"] == "hand-edited prompt A"
    assert scenes[0]["duration"] == 6.0
    assert scenes[1]["prompt"] == "hand-edited prompt B"
    assert edited["project"]["scenario_style_block"] == "A single lantern-lit room."
    # editing does not itself approve anything
    assert edited["project"]["stages"]["scenario"] == "awaiting_approval"


def test_edit_scenario_null_style_block_clears_it(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [{"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a",
                             "duration": 8.0}],
        "style_block": "not empty",
    })

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [{"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a",
                             "duration": 8.0}],
        "style_block": None,
    })
    assert status == 200, edited
    assert edited["project"]["scenario_style_block"] is None


def test_edit_scenario_omitting_style_block_leaves_it_unchanged(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [{"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a",
                             "duration": 8.0}],
        "style_block": "keep me",
    })

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [{"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "b",
                             "duration": 8.0}],
    })
    assert status == 200, edited
    assert edited["project"]["scenario_style_block"] == "keep me"


def test_edit_scenario_refuses_a_gap_the_same_way_generate_does(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 7.0, "prompt": "a", "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "b", "duration": 7.0},
        ],
    })
    assert (status, payload["error"]["code"]) == (400, "scenario_invalid"), payload


def test_edit_scenario_refuses_a_duration_outside_five_to_ten_seconds(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a", "duration": 30.0},
        ],
    })
    assert (status, payload["error"]["code"]) == (400, "scenario_invalid"), payload
    assert payload["error"]["detail"]["reason"] == "duration_out_of_range"


def test_edit_scenario_rejects_a_malformed_entry_as_args_invalid(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, payload = srv._request(
        "PUT", f"/api/projects/{pid}/scenario",
        {"scenario_scenes": [{"tag": "verse", "start": 0.0, "prompt": "a", "duration": 8.0}]})
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


# -- fresh_start (P0 fix, keyframe-chain defect 2026-08-25): PUT /scenario -------------------------


def test_edit_scenario_accepts_and_stores_fresh_start_true(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a", "duration": 6.0,
             "fresh_start": False},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "b", "duration": 7.0,
             "fresh_start": True},
        ],
    })
    assert status == 200, edited
    scenes = edited["project"]["scenario_scenes"]
    assert scenes[0]["fresh_start"] is False
    assert scenes[1]["fresh_start"] is True


def test_edit_scenario_omitting_fresh_start_defaults_to_false(_serve, monkeypatch):
    """No `fresh_start` key at all on a `PUT` entry -- an editor built before this field existed,
    or a human who never touched the new checkbox -- must store `False`, not omit the key or
    raise, so every scene this route has ever accepted keeps working unchanged.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a", "duration": 8.0},
        ],
    })
    assert status == 200, edited
    assert edited["project"]["scenario_scenes"][0]["fresh_start"] is False


def test_edit_scenario_rejects_a_non_boolean_fresh_start(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a", "duration": 8.0,
             "fresh_start": "true"},
        ],
    })
    assert (status, payload["error"]["code"]) == (400, "args_invalid"), payload


# == Full path (P0 fix, keyframe-chain defect 2026-08-25): flag survives generate -> PUT -> ========
# == approve -> the submitted scene's own args (no --image/no i2v line for the flagged scene) ======


def test_fresh_start_flag_reaches_the_submitted_scenes_own_generate_args(_serve, monkeypatch):
    """The whole path the task exists for: a scenario with a cast change on its second section,
    `PUT` through the gate, `approve/scenario` (which calls `build_clip_scenes` and `assemble.
    advance_project` for real -- `worker.run_job`'s own fakes stand in for the GPU, not for any
    of the code between the `PUT` and the job this test inspects), scene 0's own job faked
    `done`, and the *second* scene's own submitted `generate` job checked directly: no `--image`,
    no `SCENE_I2V_INSTRUCTION` line -- while a sibling project whose scenario never sets the flag
    gets both, on the identical two-section shape. Both assertions live in one test so a change
    that breaks either the "on" or the "off" path is caught by the same run.
    """
    srv = _serve()

    def _run_scene_zero_and_return_scene_ones_args(pid):
        approved = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
        assert approved["project"]["stages"]["scenario"] == "approved"
        scene0_job_id = approved["project"]["scenes"][0]["job_id"]
        job = q.claim(srv.queue_root)
        assert job.id == scene0_job_id
        _write_fake_clip(job)
        code = worker.run_job(srv.queue_root, job, spawn=_scene_hook_spawn(), outdir=srv.root)
        assert code == 0
        # the post-job hook already submitted scene 1 -- read it back off the queue rather than
        # off `project.json` (whose own `scenes[i]` this route never records `args` onto). Two
        # projects' own jobs share one queue in this test, so the lookup is by `note` (`scene_
        # note(pid, 1)`, unique per project+scene), not merely "not scene 0's own id" -- the other
        # project's still-pending scene 1 job would otherwise match that too loosely.
        jobs, _broken = q.scan(srv.queue_root)
        second_job = next(j for j in jobs if j.note == assemble_module.scene_note(pid, 1))
        return second_job.args

    pid_flagged = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put = srv._request("PUT", f"/api/projects/{pid_flagged}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0,
             "prompt": "Aldred carries the baby through the hall", "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0,
             "prompt": "Aldred alone in the empty hall, the baby gone", "duration": 7.0,
             "fresh_start": True},
        ],
    })
    assert status == 200, put
    flagged_args = _run_scene_zero_and_return_scene_ones_args(pid_flagged)
    assert "--image" not in flagged_args
    assert "is fully referenced" not in flagged_args[1]

    pid_plain = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put = srv._request("PUT", f"/api/projects/{pid_plain}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a quiet room", "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "the same room, later",
             "duration": 7.0},
        ],
    })
    assert status == 200, put
    plain_args = _run_scene_zero_and_return_scene_ones_args(pid_plain)
    assert "--image" in plain_args
    assert "is fully referenced" in plain_args[1]


def test_edit_scenario_from_draft_opens_the_gate(_serve, monkeypatch):
    """Ревью, фикс-раунд 1, I1: a scenario written entirely by hand, with no `/scenario/generate`
    call at all (`stages.scenario` still `"draft"` -- the instrumental-track path the design spec
    names), must not be a dead end. Before this fix the only route that ever flipped `stages.
    scenario` to `"awaiting_approval"` was `/scenario/generate`, so a hand-written scenario could
    never reach `approve/scenario` without first calling `/scenario/generate` and overwriting it.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    project_before = srv.get_json(f"/api/projects/{pid}")["project"]
    assert project_before["stages"]["scenario"] == "draft"
    assert project_before["scenario_scenes"] == []

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "hand-written from scratch",
             "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "hand-written second half",
             "duration": 7.0},
        ],
        "style_block": "Hand-written style, no LLM involved.",
    })
    assert status == 200, edited
    assert edited["project"]["stages"]["scenario"] == "awaiting_approval"
    assert edited["project"]["scenario_scenes"][0]["prompt"] == "hand-written from scratch"

    approved_scenario = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    assert approved_scenario["advance"]["action"] == "submitted_scene"
    scenes = approved_scenario["project"]["scenes"]
    assert len(scenes) >= 1
    assert approved_scenario["project"]["stages"]["scenario"] == "approved"


def test_edit_scenario_while_awaiting_approval_stays_awaiting_approval(_serve, monkeypatch):
    """A `PUT` on a scenario that already opened the gate (via a prior `/scenario/generate` or
    `PUT`) is a no-op re-write of the same status, not a new transition -- editing before approval
    must not do anything surprising to `stages.scenario` beyond leaving it exactly where it already
    was (ревью, фикс-раунд 1, I1: "PUT в статусе awaiting_approval ведёт себя как раньше").
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    assert generated["project"]["stages"]["scenario"] == "awaiting_approval"

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "edited once more",
             "duration": 8.0},
        ],
    })
    assert status == 200, edited
    assert edited["project"]["stages"]["scenario"] == "awaiting_approval"

    # a second PUT still leaves it at awaiting_approval, and approve/scenario still works
    status, edited_again = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "edited twice",
             "duration": 8.0},
        ],
    })
    assert status == 200, edited_again
    assert edited_again["project"]["stages"]["scenario"] == "awaiting_approval"

    approved_scenario = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    assert approved_scenario["project"]["stages"]["scenario"] == "approved"


# -- migration: a project.json written before the scenario stage existed -------------------------


def test_a_project_json_without_a_scenario_stage_migrates_to_approved_on_load(_serve):
    """Task 3's own migration (`Project._apply`), exercised through the web layer: a `project.json`
    hand-truncated to look like it predates this feature reads `stages.scenario == "approved"` --
    never `"draft"` -- the moment it is loaded, in memory, with nothing written back to disk."""
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    data = json.loads(project_path.read_text(encoding="utf-8"))
    del data["stages"]["scenario"]
    on_disk_before = project_path.read_text(encoding="utf-8")
    project_path.write_text(json.dumps(data), encoding="utf-8")

    detail = srv.get_json(f"/api/projects/{pid}")["project"]
    assert detail["stages"]["scenario"] == "approved"
    # nothing was written back just from loading it
    assert json.loads(project_path.read_text(encoding="utf-8"))["stages"].get("scenario") is None


def test_a_migrated_clip_project_still_builds_scenes_straight_off_approve_track(_serve,
                                                                                monkeypatch):
    """The compatibility path task 4's own brief asks for by name: an old clip project (`scenario`
    migrated to `"approved"`, `scenario_scenes` still empty) must not get stuck forever with no
    route left that ever builds its scenes -- `approve/track` keeps the old procedural behaviour
    for exactly this shape.
    """
    srv = _serve()
    sid = _new_session_with_project(
        srv, _project_body(kind="clip", scenes=None, lyrics=_TWO_SECTION_LYRICS,
                           caption="Warm pop."))
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    project_path = Path(srv.root) / "projects" / pid / "project.json"
    data = json.loads(project_path.read_text(encoding="utf-8"))
    del data["stages"]["scenario"]
    project_path.write_text(json.dumps(data), encoding="utf-8")

    srv.post_json(f"/api/projects/{pid}/approve/script", {})
    fake_result = sr.SongResult(
        wav=Path("song.wav"), mastered_wav=Path("song.mastered.wav"), mp3=Path("song.mp3"),
        mastered_mp3=Path("song.mastered.mp3"), duration=16.0, transcript="ла ла ла",
        sections=[{"name": "verse", "start": 0.0, "end": 16.0}], undersung=False)
    monkeypatch.setattr(sr, "run_song", lambda *a, **kw: fake_result)
    job = q.claim(srv.queue_root)
    worker.run_job(srv.queue_root, job, spawn=_caffeinate_spy([]), outdir=srv.root)

    approved_track = srv.post_json(f"/api/projects/{pid}/approve/track", {})
    assert approved_track["advance"]["action"] == "submitted_scene"
    scenes = approved_track["project"]["scenes"]
    assert len(scenes) >= 1
    _assert_scene_total_within_snap_tolerance(scenes, 16.0)
    # the scenario gate stays "approved" (the migration) throughout -- no scenario route involved
    assert approved_track["project"]["stages"]["scenario"] == "approved"


# == I1 (fix round 2, 2026-08-19 review): a scene's own `duration` is a hint, never a promise =====


def test_edit_scenario_duration_does_not_change_the_built_scenes_own_length(_serve, monkeypatch):
    """The controller's own decision (I1, fix round 2): `scenario_scenes[i]["duration"]` stays
    editable and validated (the schema and `_validate_scenario_scenes` both require `5..10`), but a
    hand edit to it must never change the *built* scene's own duration -- `_scenario_segments`
    drops it entirely, and `end - start` (the section's own approved span) is what actually drives
    the built scene's length. Reproduces exactly the live-gate finding this fix responds to: editing
    `duration` `7.0 -> 6.0` still produced a built scene of `7.29s` -- the built length tracks the
    section span, not the edited field, whatever it says.

    Both sections here span exactly `8.0s`; both built durations are therefore *exact*, not merely
    "close to 8, not 6/9", which is what makes these assertions pin the actual mechanism rather than
    a tolerance band both the edited and the derived value could fall inside. The two exact values
    differ because the two scenes render on different grids (latent-handoff wave, task 6,
    `_assert_scene_durations_on_h3_grid`'s own docstring): scene 0 is not chained and `8.0 * 24 ==
    192 == 17*11 + 5` is already a grid point, so it comes back as exactly `8.0`; scene 1 is chained
    and delivers the largest multiple of 17 at or below 192 -- `187/24 ≈ 7.7917s`, with the request
    it actually renders (`187 + 22 == 209 == 17*12 + 5`) back on H3's own grid. Neither is the
    edited `duration` field, which is the whole point.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, edited = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "prompt A", "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "prompt B", "duration": 9.0},
        ],
    })
    assert status == 200, edited
    assert edited["project"]["scenario_scenes"][0]["duration"] == 6.0, (
        "the hand edit itself must be accepted and stored -- this route validates and keeps it")

    approved = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    built = approved["project"]["scenes"]
    assert len(built) == 2
    assert built[0]["duration"] == pytest.approx(8.0), (
        "built duration must come from the section span (8.0s), not the edited `duration` field "
        f"(6.0s): got {built[0]['duration']}")
    assert built[1]["duration"] == pytest.approx(187 / 24), (
        "built duration must come from the section span (8.0s, snapped onto the chained scene's "
        f"own 17k grid), not the edited `duration` field (9.0s): got {built[1]['duration']}")
    _assert_scene_durations_on_h3_grid(built)


# == I2 (fix round 2, 2026-08-19 review): approve-scenario must wait for a pending blur-save =======


_NODE = shutil.which("node")
_needs_node_for_scenario_race = pytest.mark.skipif(
    _NODE is None,
    reason="`node` is not in PATH; the client-side scenario-save race (I2) is checked by actually "
           "running `h3_48gb/webui/app.js`'s own event handlers, which needs node outside a "
           "browser")

_SCENARIO_RACE_SCRIPT = Path(__file__).resolve().parent / "_scenario_race_check.mjs"
_APP_JS_URL = (Path(__file__).resolve().parent.parent / "h3_48gb" / "webui" / "app.js").as_uri()


def _run_scenario_race_check(base_url: str, pid: str, edited_prompt: str, timeout=30) -> dict:
    """Runs `_scenario_race_check.mjs` (see its own module docstring) against a real, already
    running server -- drives the *real* `app.js`, not a reimplementation, through a `focusout`
    immediately followed by a click on "Утвердить сюжет" (the exact order a browser delivers them
    in when the button is clicked while the field it edited still has focus), and reports the two
    requests' own timing plus the server's final state.
    """
    encoded = base64.b64encode(edited_prompt.encode("utf-8")).decode("ascii")
    result = subprocess.run(
        [_NODE, str(_SCENARIO_RACE_SCRIPT), _APP_JS_URL, base_url, pid, encoded],
        capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"_scenario_race_check.mjs failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(result.stdout)


@_needs_node_for_scenario_race
def test_approving_the_scenario_right_after_a_blurred_edit_does_not_lose_it(_serve, monkeypatch):
    """I2 (fix round 2, 2026-08-19 review): clicking "Утвердить сюжет" immediately after a
    scenario field's own `focusout` used to race `PUT .../scenario` against `POST .../approve/
    scenario` -- if the `PUT` landed after `approve/scenario`'s own `_load_project` but before its
    own (unconditional, inherited-from-`approve/track`) `proj.save()`, the edit disappeared from
    both the saved scenario *and* the scenes built from it, with no 409 and no error banner (the
    review's own finding: `web.py:3893-3894`).

    **The server-side race this reproduces is real and deliberately left alone** -- the controller's
    own scope decision: `Project.save()`'s blind-overwrite pattern is inherited from `approve/
    track` and out of this wave's scope (see the fix report). What closes it is `app.js` now
    awaiting the pending save (`pendingScenarioSave`) before `approve-scenario` sends its own
    request at all. This test forces the race window open from the *server* side
    (`web.build_clip_scenes`, the call inside that window, delayed 300ms -- deterministic, and
    monkeypatched only for this one test) so the outcome does not depend on true network-arrival
    timing between two `fetch` calls a few microseconds apart: without the client fix, the delay
    alone reliably reproduces the loss (see the mutation check in the fix report); with the fix,
    `approve-scenario`'s own request is never even *sent* until the `PUT` has fully round-tripped,
    so the delay changes nothing about the outcome.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=12.0)
    status, put_first = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 12.0, "prompt": "prompt A", "duration": 8.0}],
    })
    assert status == 200, put_first

    real_build_clip_scenes = web.build_clip_scenes

    def _delayed_build_clip_scenes(*args, **kwargs):
        time.sleep(0.3)
        return real_build_clip_scenes(*args, **kwargs)

    monkeypatch.setattr(web, "build_clip_scenes", _delayed_build_clip_scenes)

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    edited_prompt = "prompt A -- edited during the race"
    result = _run_scenario_race_check(base_url, pid, edited_prompt)

    assert result["putStatus"] == 200, result
    assert result["postStatus"] == 200, result
    # The sequencing the fix actually guarantees: the PUT fully finishes before the POST is even
    # sent -- not merely "before it finishes", the stronger claim the fix's own `await` makes.
    assert result["putFinishedAt"] <= result["postStartedAt"], (
        "approve-scenario's own POST must not be sent before the pending PUT has finished -- "
        f"PUT finished at {result['putFinishedAt']}, POST started at {result['postStartedAt']}")

    final = result["finalProject"]
    assert final["stages"]["scenario"] == "approved"
    assert final["scenario_scenes"][0]["prompt"] == edited_prompt, (
        "the edit must survive on disk, not revert to the stale pre-edit prompt -- got "
        f"{final['scenario_scenes'][0]['prompt']!r}")
    # A split piece may carry `web.SPLIT_CONTINUATION_CLAUSE` on top -- what this test pins is
    # that every built scene starts from the EDITED prompt, not a stale one.
    assert all(scene["prompt"].startswith(edited_prompt) for scene in final["scenes"]), (
        "the built scene(s) must be built from the edited prompt, not a stale one -- got "
        f"{[s['prompt'] for s in final['scenes']]!r}")


# == Находка 1 (волна ux-фиксов 2026-08-24): the scenario buttons must not fire twice =============


_SCENARIO_BUSY_SCRIPT = Path(__file__).resolve().parent / "_scenario_busy_check.mjs"


def _run_scenario_busy_check(base_url: str, pid: str, timeout=30) -> dict:
    """Runs `_scenario_busy_check.mjs` (see its own module docstring) against a real, already
    running server -- drives the *real* `app.js` through two clicks on "Сюжет без LLM" fired back
    to back, and reports whether the second click's own listener ran at all and how many `POST
    .../scenario/generate` requests actually went out.
    """
    result = subprocess.run(
        [_NODE, str(_SCENARIO_BUSY_SCRIPT), _APP_JS_URL, base_url, pid],
        capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"_scenario_busy_check.mjs failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(result.stdout)


@_needs_node_for_scenario_race
def test_a_fast_double_click_on_a_scenario_button_sends_only_one_request(_serve, monkeypatch):
    """Находка 1 (волна ux-фиксов 2026-08-24, `ux-syuzhet-report.md`): before this fix, `projectBusy`
    was set but never read by any button's own `disabled` state -- a second click on "Сгенерировать
    сюжет"/"Сюжет без LLM"/"Утвердить сюжет" while the first request was still in flight (an
    external LLM provider can take 2-7 minutes; nothing on the page said anything was happening at
    all) sent a second, independent request, and whichever one's response landed second silently
    overwrote the first's scenario with no 409 and no banner.

    `app.js`'s own fix is `setScenarioBusyUi` -- called synchronously, from inside each scenario
    button's own click handler, the instant that handler runs (so it can never suppress the very
    click that triggered it, only a later one -- see its own docstring for why this is not done
    through `renderProjectModal()`'s `innerHTML` rebuild instead). This test proves the guard
    actually stops the second request from ever being sent, not merely that the *first* one
    behaves -- `_scenario_busy_check.mjs` fires two clicks with no `await` between them (the exact
    gesture a fast real double-click produces) and models the one piece of real browser behaviour
    that makes the guard work at all: a disabled form control never receives a `click` event.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=12.0)

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    result = _run_scenario_busy_check(base_url, pid)

    assert result["firstDelivered"] is True, (
        "the first click must reach app.js's own click listener -- nothing was disabled yet")
    assert result["disabledRightAfterFirstClick"] is True, (
        "setScenarioBusyUi must disable the scenario buttons synchronously, before the first "
        f"click's own request is even sent -- got {result}")
    assert result["secondDelivered"] is False, (
        "the second click must never reach app.js's click listener at all -- the button is "
        f"already disabled by the time it fires -- got {result}")
    assert result["generateCallCount"] == 1, (
        "exactly one POST /scenario/generate must have been sent -- a second one would mean the "
        f"double-click guard let a duplicate (potentially paid) request through -- got {result}")

    final = srv._request("GET", f"/api/projects/{pid}")[1]["project"]
    assert final["stages"]["scenario"] == "awaiting_approval"
    assert len(final["scenario_scenes"]) >= 1, (
        "the one request that *did* go out must still have actually written a scenario -- got "
        f"{final}")


# == Находка 2 (волна ux-фиксов 2026-08-24): editing a scene's tail must not lose/duplicate ========
# == the common visual-bible prefix on save ========================================================


_SCENARIO_PREFIX_SCRIPT = Path(__file__).resolve().parent / "_scenario_prefix_check.mjs"

# `_PREFIX_BIBLE` is a real slice of scene 0's own prompt in `scenario_prompts_kolybelnaya.json`
# (the same fixture `test_web.py`'s `scenarioCommonPrefix` regression test reads), not a hand-typed
# string -- a hand-typed bible with a convenient early "\n" was exactly the round-1 blind spot (see
# that test's own docstring): it snapped fine on synthetic strings and silently kept almost nothing
# of a real, single-paragraph visual bible. Cut to the last full sentence within the first 400
# characters, so this fixture also carries a real embedded "\n" long before the cut, the same shape
# that broke round 1.
_KOLYBELNAYA_FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "scenario_prompts_kolybelnaya.json")
    .read_text(encoding="utf-8"))
_REAL_BIBLE_SOURCE = _KOLYBELNAYA_FIXTURE["prompts"][0]
_PREFIX_BIBLE = _REAL_BIBLE_SOURCE[:_REAL_BIBLE_SOURCE.rfind(". ", 0, 400) + 2]
assert len(_PREFIX_BIBLE) >= 300, (
    "sanity check on the fixture -- this must stay well above SCENARIO_COMMON_PREFIX_MIN_LEN "
    f"(200) for the test below to mean anything, got {len(_PREFIX_BIBLE)} chars")
assert "\n" in _PREFIX_BIBLE, (
    "must keep the fixture's own early newline -- that positioning is the whole point")

# The two seed scenes diverge on the very first character after the bible (Alpha/Beta) -- no
# shared label like "Scene: " in between, so the raw common prefix of these two prompts is exactly
# `_PREFIX_BIBLE`, and `expected_edited` below needs no separate accounting for it.
_PREFIX_SCENES = [
    {"tag": "verse", "start": 0.0, "end": 8.0,
     "prompt": _PREFIX_BIBLE + "Alpha walks by the shore.", "duration": 7.0},
    {"tag": "verse", "start": 8.0, "end": 16.0,
     "prompt": _PREFIX_BIBLE + "Beta runs along the shore.", "duration": 7.0},
]


def _run_scenario_prefix_check(base_url: str, pid: str, edit_idx: int, edited_tail: str,
                                timeout=30) -> dict:
    """Runs `_scenario_prefix_check.mjs` (see its own module docstring) against a real,
    already-running server -- drives the *real* `app.js` through editing one scene's TAIL field
    (what `.scenario-prompt` now holds, `projectScenarioSceneHtml`'s own docstring) and reports
    exactly what `collectScenarioScenes` sent as that scene's full `prompt` in the `PUT` body.
    """
    encoded = base64.b64encode(edited_tail.encode("utf-8")).decode("ascii")
    result = subprocess.run(
        [_NODE, str(_SCENARIO_PREFIX_SCRIPT), _APP_JS_URL, base_url, pid, str(edit_idx), encoded],
        capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"_scenario_prefix_check.mjs failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(result.stdout)


@_needs_node_for_scenario_race
def test_editing_a_scenes_tail_reconstructs_the_full_prompt_without_losing_or_duplicating_the_prefix(
        _serve, monkeypatch):
    """Находка 2 (живой проход 2026-08-24, `ux-syuzhet-report.md`): before this fix, every scene's
    `.scenario-prompt` field held the FULL promt, including the visual bible every scene repeats
    verbatim (`docs/h3-prompt-system.md`) -- on a real scenario the bible is the longest passage in
    the field, and the three lines visible without scrolling were identical across all 16-38 scene
    cards, while the one thing that actually differs (what happens in this scene) sat below the
    fold.

    The fix slices the shared prefix out of what the textarea shows (`scenarioCommonPrefix` +
    `projectScenarioSceneHtml`), and shows it once, read-only, above the list. That makes
    `collectScenarioScenes` (`app.js`) responsible for gluing `prefix + tail` back together before
    `PUT` -- get it wrong (drop the prefix, or paste it twice) and the edit that reaches disk is
    silently corrupted, not merely displayed oddly: `PUT .../scenario` replaces the whole scene
    list at once (`web._edit_project_scenario`'s own docstring), so there is no server-side check
    that would catch a client that reconstructed the wrong string.

    This drives the real `app.js`, edits scene 0's tail only, and checks three things a fabricated
    unit test of `scenarioCommonPrefix` alone cannot: (1) the actual `PUT` body carries the bible
    back on the edited scene, exactly once; (2) the untouched scene (index 1) is sent with its
    original, whole prompt unchanged -- `collectScenarioScenes`'s own on-disk fallback for a scene
    not present in the DOM; (3) what actually lands on disk matches.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put_first = srv._request(
        "PUT", f"/api/projects/{pid}/scenario", {"scenario_scenes": _PREFIX_SCENES})
    assert status == 200, put_first

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    edited_tail = "Alice dances under fireworks tonight."
    result = _run_scenario_prefix_check(base_url, pid, 0, edited_tail)

    assert result["putStatus"] == 200, result
    sent = result["putBody"]["scenario_scenes"]
    assert len(sent) == 2, f"both scenes must be sent -- PUT replaces the whole list: {sent}"

    expected_edited = _PREFIX_BIBLE + edited_tail
    assert sent[0]["prompt"] == expected_edited, (
        "the edited scene's prompt must be exactly prefix+tail -- not missing the bible, and not "
        f"carrying it twice: got {sent[0]['prompt']!r}, expected {expected_edited!r}")
    assert sent[0]["prompt"].count(_PREFIX_BIBLE.strip()) == 1, (
        f"the bible must appear exactly once in the edited scene's prompt: {sent[0]['prompt']!r}")

    assert sent[1]["prompt"] == _PREFIX_SCENES[1]["prompt"], (
        "the untouched scene must be sent with its own on-disk prompt unchanged, byte for byte -- "
        f"got {sent[1]['prompt']!r}")

    final_scenes = result["finalProject"]["scenario_scenes"]
    assert final_scenes[0]["prompt"] == expected_edited, (
        f"what actually landed on disk must match what was sent: {final_scenes[0]['prompt']!r}")
    assert final_scenes[1]["prompt"] == _PREFIX_SCENES[1]["prompt"]


# == P0 fix (keyframe-chain defect, 2026-08-25): the fresh_start checkbox saves through PUT ========


_SCENARIO_FRESH_START_SCRIPT = Path(__file__).resolve().parent / "_scenario_fresh_start_check.mjs"


def _run_scenario_fresh_start_check(base_url: str, pid: str, toggle_idx: int, *,
                                     expect_no_put: bool = False, timeout=30) -> dict:
    """Runs `_scenario_fresh_start_check.mjs` (see its own module docstring) against a real,
    already-running server -- drives the *real* `app.js` through checking one scene's own
    `.scenario-fresh-start` box and reports exactly what `collectScenarioScenes` sent in the `PUT`
    body, plus the server's own final state. `expect_no_put=True` (M6) drives a `toggle_idx` the
    script's own `change` listener must refuse -- the script then reports only `{"putSent": bool}`
    instead of waiting for a `PUT` that must never arrive.
    """
    args = [_NODE, str(_SCENARIO_FRESH_START_SCRIPT), _APP_JS_URL, base_url, pid, str(toggle_idx)]
    if expect_no_put:
        args.append("1")
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"_scenario_fresh_start_check.mjs failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(result.stdout)


@_needs_node_for_scenario_race
def test_checking_the_fresh_start_box_saves_it_through_put_and_leaves_other_scenes_alone(
        _serve, monkeypatch):
    """Drives the real `app.js`'s own delegated `change` listener for `.scenario-fresh-start`
    (`docs/h3-prompt-system.md`, "Breaking the chain on a cast change") -- checking scene 1's own
    box must fire `PUT /api/projects/<id>/scenario` with `scenario_scenes[1]["fresh_start"] ==
    True`, on `change` (no blur needed, unlike the text/duration fields `_scenario_prefix_check.
    mjs` already covers), while scene 0 -- never touched in this script's own fake DOM -- must be
    sent with its on-disk `fresh_start` (`False`) unchanged, `collectScenarioScenes`'s own
    fallback for a field this test's "DOM" never populates.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put_first = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a quiet room",
             "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "the same room, later",
             "duration": 7.0},
        ],
    })
    assert status == 200, put_first

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    result = _run_scenario_fresh_start_check(base_url, pid, 1)

    assert result["putStatus"] == 200, result
    sent = result["putBody"]["scenario_scenes"]
    assert len(sent) == 2, f"both scenes must be sent -- PUT replaces the whole list: {sent}"
    assert sent[1]["fresh_start"] is True, sent
    assert sent[0]["fresh_start"] is False, (
        "the untouched scene must be sent with its own on-disk fresh_start unchanged -- "
        f"got {sent[0]!r}")
    assert sent[1]["prompt"] == "the same room, later", (
        "checking the box must not disturb the prompt this script never touched -- "
        f"got {sent[1]['prompt']!r}")

    final = result["finalProject"]
    assert final["scenario_scenes"][1]["fresh_start"] is True
    assert final["scenario_scenes"][0]["fresh_start"] is False

    # M3 (review round 2, 2026-08-26): scene 0's own checkbox must not even be rendered -- the
    # flag does nothing on scene 0 (`assemble._submit_next_scene`'s own `idx > 0` gate).
    rendered = result["renderedHtml"]
    assert 'class="scenario-fresh-start" data-idx="1"' in rendered, rendered
    assert 'class="scenario-fresh-start" data-idx="0"' not in rendered, rendered


@_needs_node_for_scenario_race
def test_checking_a_stale_fresh_start_box_does_not_put(_serve, monkeypatch):
    """M6 (review round 2, 2026-08-26): the `focusout` listener for `.scenario-prompt`/
    `.scenario-duration` already refuses to save a field whose own `data-idx` no longer names a
    real scene (`(project.project.scenario_scenes || [])[idx]` missing -- a panel render that is
    stale by the time the event fires, e.g. a scenario just regenerated with fewer scenes). The
    `change` listener for `.scenario-fresh-start` needs the identical guard -- this drives it with
    `toggle_idx=5` against a two-scene project (indices 0-1 only) and proves no `PUT` is ever sent
    for it.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put_first = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a quiet room",
             "duration": 6.0},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "the same room, later",
             "duration": 7.0},
        ],
    })
    assert status == 200, put_first

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    result = _run_scenario_fresh_start_check(base_url, pid, 5, expect_no_put=True)

    assert result["putSent"] is False, (
        "a change event on a `data-idx` outside the current scenario_scenes must not PUT at all")


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


# == Task 2 ("выбор провайдера для сценария"): `POST /api/providers/<name>/test` ===================
#
# The cheap probe, before the scenario route's own long turn -- `GET {base_url}/v1/models` for
# `openai`, `port_alive` for `llama-local`. `_FakeLlama`'s `do_GET` had to grow a `/v1/models`
# branch for these (`tests/_fake_llama.py`): unpatched, it 404s everything but `/health` and never
# records a GET into `.requests` at all, so a test against the unpatched mock could not tell "the
# route actually hit `/v1/models`" from "the route hit nothing and the mock's blanket 404 happened
# to look like a failure branch" -- see that file's own docstring on `models_payload`.


def test_provider_test_route_succeeds_and_names_the_exact_path_it_asked(_serve):
    fake = _FakeLlama(models_payload={"data": [{"id": "gpt-strong"}, {"id": "gpt-fast"}]})
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m"}},
                     active="ext")
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["reachable"] is True
    assert payload["models"] == ["gpt-strong", "gpt-fast"]
    (req,) = fake.requests
    assert req["path"] == "/v1/models", (
        "проба обязана ходить именно на /v1/models -- _base_url() уже без /v1, "
        "путь /models даст 404 на любом реальном провайдере")


def test_provider_test_route_reports_a_missing_token_without_any_network_call(_serve):
    fake = _FakeLlama(models_payload={"data": [{"id": "should-never-be-seen"}]})
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
                     active="ext")  # no .env -> the token is missing
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is False
    assert "OPENROUTER_API_KEY" in payload["detail"]
    assert fake.requests == [], "нет токена -- пробовать сеть незачем и не следовало"


def test_provider_test_route_reports_an_unreachable_provider_honestly(_serve):
    srv = _serve(providers={"ext": {"type": "openai", "base_url": "http://127.0.0.1:1",
                                    "model": "m"}},
                 active="ext")  # port 1: nobody is listening
    status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    assert status == 200, payload
    assert payload["ok"] is False
    assert payload["reachable"] is False
    assert "недоступен" in payload["detail"]


def test_provider_test_route_reports_a_response_that_is_not_a_models_list(_serve):
    """`ok=False` here -- but `reachable=True`, not the same failure as no answer at all: the
    provider is up, it just did not answer with what a models probe expects."""
    fake = _FakeLlama(models_payload={"error": "not a models list"})
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m"}},
                     active="ext")
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is False
    assert payload["reachable"] is True


def test_provider_test_route_never_echoes_the_raw_body_of_a_bad_models_reply(_serve):
    """M3 (ревью): the "not a models list" branch used to put up to 200 chars of the raw
    response body straight into `detail` -- honest, but a misconfigured proxy in front of a
    real provider can echo request headers (the bearer token among them) back in an error body
    it still answers 200 with, and that would have put the token one hop from the UI. Simulated
    here by baking the token into the fake's own "bad" response body -- proves `detail` never
    carries it, whatever the provider chose to send back."""
    fake = _FakeLlama(models_payload={"upstream_echo":
                                      "Authorization: Bearer sk-very-secret"})
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
                     active="ext", env="OPENROUTER_API_KEY=sk-very-secret\n")
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is False
    assert "sk-very-secret" not in json.dumps(payload, ensure_ascii=False)


def test_provider_test_route_reports_a_response_that_is_not_json_at_all(_serve):
    """`ok=False`, `reachable=True` -- same family as "answered, but not a models list" above,
    but the response is not even parseable JSON (an HTML error page from a proxy in front of the
    real provider, say). `json.loads` must be given a chance to fail on its own bytes, which is
    why this needs `_FakeLlama`'s own `models_raw` (a `dict` passed through `models_payload`
    always round-trips through `json.dumps`, so it can never produce this branch)."""
    fake = _FakeLlama(models_raw="<html>502 Bad Gateway</html>")
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m"}},
                     active="ext")
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is False
    assert payload["reachable"] is True
    assert "JSON" in payload["detail"]


def test_provider_test_route_for_a_local_port_that_is_not_up_is_not_shown_as_an_error(_serve):
    """A `llama-local` provider whose port nobody answers on is an ordinary state (the model
    raises itself, on `ensure_up`, at generation time) -- `ok` must stay `True`."""
    srv = _serve(providers={"local": {"type": "llama-local", "port": 1}}, active="local")
    status, payload = srv.post_json_raw("/api/providers/local/test", {})
    assert status == 200, payload
    assert payload["ok"] is True, "неподнятый порт локальной модели -- не отказ пробы"
    assert payload["reachable"] is False


def test_provider_test_route_for_a_local_port_that_is_up(_serve):
    fake = _FakeLlama()  # /health answers 200 by default
    try:
        srv = _serve(providers={"local": {"type": "llama-local", "port": fake.port}},
                     active="local")
        status, payload = srv.post_json_raw("/api/providers/local/test", {})
    finally:
        fake.close()
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["reachable"] is True


def test_provider_test_route_never_leaks_the_token(_serve):
    fake = _FakeLlama(models_payload={"data": [{"id": "m1"}]})
    try:
        srv = _serve(providers={"ext": {"type": "openai",
                                        "base_url": f"http://127.0.0.1:{fake.port}",
                                        "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
                     active="ext", env="OPENROUTER_API_KEY=sk-very-secret\n")
        status, payload = srv.post_json_raw("/api/providers/ext/test", {})
        assert status == 200, payload
        assert "sk-very-secret" not in json.dumps(payload, ensure_ascii=False)
        (req,) = fake.requests
        assert req["headers"].get("Authorization") == "Bearer sk-very-secret", (
            "провайдер обязан получить токен -- проверяется, что он ушёл наружу правильно, "
            "а не то, что заголовок пуст")
    finally:
        fake.close()


def test_build_clip_scenes_split_pieces_after_the_first_carry_a_continuation_clause():
    """Night run 3 (2026-08-26): a section spanning past `SCENE_MAX_SECONDS` is mechanically cut
    into pieces that all shared the section's single prompt verbatim -- and each piece chains
    i2v off the previous piece's last frame, so identical text + same start frame rendered as
    the same scene looping two or three times in a row (16 twin groups out of 40 scenes; the
    user saw the loops with the naked eye). The system prompt now tells the model to keep
    sections inside 10s, but the splitter stays as the fallback for a model that overruns -- and
    its later pieces must now say they CONTINUE the shot rather than restate it: the first piece
    keeps the section's own prompt untouched, every later piece appends
    `web.SPLIT_CONTINUATION_CLAUSE` so the render moves forward instead of resetting.
    """
    base = "a single continuous shot of rain"
    scenario_scenes = [_scenario_scene("verse", 0.0, 23.0, base)]
    scenes = web.build_clip_scenes(_scenario_track(23.0), scenario_scenes=scenario_scenes)
    assert len(scenes) == 3
    assert scenes[0]["prompt"] == base, "the first piece must keep the section prompt untouched"
    for piece in scenes[1:]:
        assert piece["prompt"] == base + web.SPLIT_CONTINUATION_CLAUSE, (
            "a later piece must append the continuation clause, once, at the end")
    assert web.SPLIT_CONTINUATION_CLAUSE.startswith(" "), (
        "the clause glues onto prompt text and must carry its own separating space")


def test_edit_scenario_accepts_a_null_fresh_start_as_false(_serve, monkeypatch):
    """The nullable-but-required schema convention (2026-08-26, OpenAI strict mode rejects a
    property outside `required` -- see test_provider's schema tests): a model with no chain
    break to declare answers `fresh_start: null`, and `_typed_scenario_scene` must store that
    as plain `False` -- not refuse it, and not store a `None` that would poison the JSON shape
    `Project.scenario_scenes` fixes. Strings are still refused (the test right above)."""
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})

    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a", "duration": 8.0,
             "fresh_start": None},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "b", "duration": 8.0,
             "fresh_start": True},
        ],
    })
    assert status == 200, payload
    stored = payload["project"]["scenario_scenes"]
    assert stored[0]["fresh_start"] is False
    assert stored[1]["fresh_start"] is True


# == Passport wave (2026-08-27): `state_in`/`state_out` ==========================================
#
# SPEC-scene-prompt-structure.md §5. Night 4's own defect class: an object or a character changes
# position between two scenes with no action anywhere that moved it (the sword put down on the
# table and back in his hand one scene later; the baby handed over outside and the mother suddenly
# inside by the door). The world's state lived implicitly, in two prompts written as independent
# descriptions, so nothing ever had to agree.
#
# **The chain is built by the code, not by the model** (S1-3 review). The model writes `state_out`
# for every scene, and `state_in` for exactly two kinds of scene: scene 0 and every `fresh_start`
# scene -- the two that have no previous frame to inherit from. Every other scene's `state_in` is
# OVERWRITTEN, unconditionally, with the previous scene's own `state_out`. That makes SPEC §5's
# "state_in of N+1 == state_out of N, verbatim" true by construction, and it is deliberately NOT
# validated as a pair anywhere on the `generate` path: a whitespace difference re-rolling a 20 000
# token reply is an unacceptable refusal, and this module's own precedent (`_style_clause`) already
# fixes rather than refuses.


def _scenario_turn(*scenes, style_block="warm light"):
    """A `provider.chat_scenario` reply, in the exact shape `_scenario_turn_to_scenes` parses --
    `scenes` given as `(tag, start, end, prompt, extra_scene_fields)` tuples.
    """
    sections = []
    for i, (tag, start, end, prompt, extra) in enumerate(scenes):
        scene = {"prompt": prompt, "duration": min(10.0, max(5.0, end - start))}
        scene.update(extra)
        sections.append({"tag": tag, "start": start, "end": end, "scene": scene})
    return {"reply": "вот сюжет", "scenario": {"sections": sections, "style_block": style_block}}


def test_typed_scenario_scene_keeps_a_written_passport_exactly_as_given():
    scene = web._typed_scenario_scene(
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "p", "duration": 8.0,
         "state_in": "she stands at the door, empty-handed",
         "state_out": "the sword lies flat on the oak table"}, 0)
    assert scene["state_in"] == "she stands at the door, empty-handed"
    assert scene["state_out"] == "the sword lies flat on the oak table"


def test_typed_scenario_scene_folds_an_absent_or_null_passport_to_an_empty_string():
    """Two sources produce a passport-less entry and neither is an error. **Absent:** every
    scenario written before this field existed, and every entry the procedural path builds
    (`_procedural_scenario_scenes` puts exactly five keys on a scene and knows nothing about a
    passport). **`null`:** the nullable-but-required schema convention (`SCENARIO_SCHEMA`, and
    `fresh_start`'s own 2026-08-26 fix) -- a model with nothing to declare answers `null`.

    Both must land as `""`, never `None`: everything downstream reads the passport as a string
    (`if state_in:`, `.strip()`, string concatenation into a prompt), and a `None` leaking into
    `Project.scenario_scenes` would be a JSON null on disk that every one of those readers would
    then have to defend against separately.
    """
    absent = web._typed_scenario_scene(
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "p", "duration": 8.0}, 0)
    assert absent["state_in"] == "" and absent["state_out"] == ""

    nulls = web._typed_scenario_scene(
        {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "p", "duration": 8.0,
         "state_in": None, "state_out": None}, 0)
    assert nulls["state_in"] == "" and nulls["state_out"] == ""


@pytest.mark.parametrize("field", ["state_in", "state_out"])
@pytest.mark.parametrize("bad", [7, 1.5, True, ["a"], {"a": 1}])
def test_typed_scenario_scene_refuses_a_non_string_passport(field, bad):
    """Wrong type is refused, not coerced -- the same discipline every other field on this entry
    already gets. `str(7)` would silently write "7" into a video prompt as the world's state.
    """
    raw = {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "p", "duration": 8.0, field: bad}
    with pytest.raises(ValueError) as err:
        web._typed_scenario_scene(raw, 3)
    assert f"entry 3: `{field}` must be a string" in str(err.value)


def test_scenario_turn_carries_the_models_own_state_out_onto_every_scene():
    scenes, _ = web._scenario_turn_to_scenes(_scenario_turn(
        ("verse", 0.0, 8.0, "p0", {"state_in": "she is at the door", "state_out": "она у стола"}),
        ("chorus", 8.0, 16.0, "p1", {"state_out": "меч лежит на столе"}),
    ))
    assert [s["state_out"] for s in scenes] == ["она у стола", "меч лежит на столе"]


def test_scenario_turn_overwrites_a_chained_scenes_state_in_with_the_previous_state_out():
    """**The derivation, and the whole architectural decision behind this wave.** A chained scene's
    `state_in` is written by the CODE, not by the model: `scenes[i]["state_in"] =
    scenes[i-1]["state_out"]`, unconditionally, for every `i > 0` that is not a `fresh_start`.

    Unconditionally is the load-bearing word. The model here writes a `state_in` on scene 1 that
    *contradicts* scene 0's own `state_out` -- exactly the drift SPEC §5's verbatim-match rule was
    written to catch -- and the pipeline must simply replace it, not merge it, not prefer it, and
    not refuse the whole reply. A `!=`-style refusal on this path would re-roll a 20 000 token
    scenario over a stray space; overwriting makes the invariant true by construction instead.
    """
    scenes, _ = web._scenario_turn_to_scenes(_scenario_turn(
        ("verse", 0.0, 8.0, "p0", {"state_in": "она у двери", "state_out": "меч лежит на столе"}),
        ("chorus", 8.0, 16.0, "p1", {"state_in": "меч у него в руке",
                                     "state_out": "он выходит в коридор"}),
        ("bridge", 16.0, 24.0, "p2", {"state_in": "", "state_out": "коридор пуст"}),
    ))
    assert scenes[1]["state_in"] == "меч лежит на столе", (
        "a chained scene's own state_in must be REPLACED by the previous scene's state_out, "
        f"not kept: got {scenes[1]['state_in']!r}")
    assert scenes[2]["state_in"] == "он выходит в коридор", (
        "the derivation must run down the whole chain, not only onto scene 1")


def test_scenario_turn_keeps_the_models_own_state_in_on_scene_zero_and_on_a_fresh_start():
    """The two scenes the model IS responsible for: scene 0 (nothing before it) and every
    `fresh_start` scene (renders from text alone, no reference frame -- so its passport is the
    only thing that describes the new world at all). Deriving either one from a neighbour would
    throw away the only exhaustive description in the reply.
    """
    scenes, _ = web._scenario_turn_to_scenes(_scenario_turn(
        ("verse", 0.0, 8.0, "p0", {"state_in": "она у двери, руки пусты",
                                   "state_out": "меч лежит на столе"}),
        ("chorus", 8.0, 16.0, "p1", {"state_in": "зал: он один у стола",
                                     "state_out": "он берёт меч", "fresh_start": True}),
    ))
    assert scenes[0]["state_in"] == "она у двери, руки пусты", \
        "scene 0 has no previous scene to derive from -- its own state_in must survive"
    assert scenes[1]["state_in"] == "зал: он один у стола", (
        "a fresh_start scene's own exhaustive state_in must survive -- it has no reference frame "
        f"to inherit a composition from: got {scenes[1]['state_in']!r}")


def test_scenario_turn_warns_but_does_not_refuse_an_empty_passport_at_a_chain_break(caplog):
    """A `fresh_start` scene (or scene 0) that came back with no `state_in` at all is a real gap --
    that scene renders from text alone and its passport is the only description of the new world
    there is. It is still **not** a refusal: `bad_model_json` costs a full re-roll of a 20 000
    token reply, and a retry does not fix a model that simply did not write the field. The scene
    keeps an empty passport (the glue step then simply pastes nothing) and the gap is logged for
    the human who is about to read the whole scenario at the gate anyway.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="h3_48gb.web"):
        scenes, _ = web._scenario_turn_to_scenes(_scenario_turn(
            ("verse", 0.0, 8.0, "p0", {"state_in": "", "state_out": "меч лежит на столе"}),
            ("chorus", 8.0, 16.0, "p1", {"state_in": "", "state_out": "он берёт меч",
                                         "fresh_start": True}),
            ("bridge", 16.0, 24.0, "p2", {"state_in": "", "state_out": "коридор пуст"}),
        ))

    assert scenes[0]["state_in"] == "" and scenes[1]["state_in"] == "", \
        "an empty passport at a chain break is kept as empty, not invented and not refused"
    assert scenes[2]["state_in"] == "он берёт меч", \
        "a chained scene is still derived normally alongside the warning"

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2, f"one warning per gap, got {warned}"
    assert any("0" in m for m in warned) and any("1" in m for m in warned), warned
    assert all("state_in" in m for m in warned), warned


def test_scenario_turn_does_not_warn_when_every_chain_break_carries_its_passport(caplog):
    """The other side of the warning: a well-formed reply must be silent. A warning that fires on
    every scenario is a warning nobody reads by the third run.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="h3_48gb.web"):
        web._scenario_turn_to_scenes(_scenario_turn(
            ("verse", 0.0, 8.0, "p0", {"state_in": "она у двери", "state_out": "меч на столе"}),
            ("chorus", 8.0, 16.0, "p1", {"state_in": "", "state_out": "он берёт меч"}),
        ))
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_edit_scenario_stores_the_passport_through_put(_serve, monkeypatch):
    """`PUT /scenario` is the path a human's own hand edit takes (and the path `app.js` sends the
    whole list back on). The passport has to survive it -- the gate is the only place a person
    ever fixes a passport the model got wrong.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a", "duration": 8.0,
             "state_in": "она у двери, руки пусты", "state_out": "меч лежит на столе"},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "b", "duration": 8.0,
             "state_in": "меч лежит на столе", "state_out": None},
        ],
    })
    assert status == 200, payload
    stored = payload["project"]["scenario_scenes"]
    assert stored[0]["state_in"] == "она у двери, руки пусты"
    assert stored[0]["state_out"] == "меч лежит на столе"
    assert stored[1]["state_in"] == "меч лежит на столе"
    assert stored[1]["state_out"] == "", "a null passport is stored as an empty string, not null"


def test_edit_scenario_rejects_a_non_string_passport(_serve, monkeypatch):
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, payload = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a", "duration": 8.0,
             "state_out": 7},
        ],
    })
    assert (status, payload["error"]["code"]) == (400, "args_invalid")
    assert "`state_out` must be a string" in payload["error"]["message"]


def test_the_procedural_path_builds_scenes_with_no_passport_keys_and_nothing_downstream_breaks(
        _serve, monkeypatch):
    """S2-3: `_procedural_scenario_scenes` puts exactly five keys on a scene (`tag`, `start`,
    `end`, `prompt`, `duration`) and never goes through `_typed_scenario_scene` at all -- the
    `/scenario/generate?procedural` route writes its output straight to disk. Every new passport
    reader must therefore read through `.get(..., "")`, never `scene["state_in"]`, or the whole
    LLM-free branch dies with a `KeyError` the moment somebody presses "сюжет без LLM".
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    payload = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    scenes = payload["project"]["scenario_scenes"]
    assert scenes and all("state_in" not in s and "state_out" not in s for s in scenes), (
        "the procedural path is expected to keep writing passport-less scenes -- if that ever "
        f"changes this test's premise is gone, not the rule: {scenes[0]}")

    built = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenes)
    assert len(built) >= 1, "building scenes from a passport-less scenario must not raise"


_SCENARIO_PASSPORT_SCRIPT = Path(__file__).resolve().parent / "_scenario_passport_check.mjs"


def _run_scenario_passport_check(base_url: str, pid: str, edit_idx: int, edited_tail: str,
                                 timeout=30) -> dict:
    """Runs `_scenario_passport_check.mjs` (see its own module docstring) against a real,
    already-running server -- drives the *real* `app.js` through one scene's own prompt edit and
    reports exactly what `collectScenarioScenes` put in the `PUT` body, plus the server's own
    final state.
    """
    encoded = base64.b64encode(edited_tail.encode("utf-8")).decode("ascii")
    result = subprocess.run(
        [_NODE, str(_SCENARIO_PASSPORT_SCRIPT), _APP_JS_URL, base_url, pid, str(edit_idx),
         encoded],
        capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"_scenario_passport_check.mjs failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return json.loads(result.stdout)


@_needs_node_for_scenario_race
def test_editing_one_scene_does_not_erase_every_scenes_passport(_serve, monkeypatch):
    """Мутация (е) из брифа. `state_in`/`state_out` have no UI control on this page at all -- they
    reach the server only through `collectScenarioScenes`'s own on-disk fallback. Drop the two
    lines that carry them and the page looks and behaves identically, the `PUT` still returns 200,
    and the server (which folds an absent passport to `""`) quietly erases every passport in the
    scenario the first time anyone fixes a typo at the gate. Nothing else in this suite would go
    red, because nothing else drives the real `app.js`.

    So: two scenes with passports on disk, a person edits scene 1's prompt, and BOTH scenes must
    come back out of the `PUT` body with their passports intact -- the edited one and the one the
    fake DOM never even rendered.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, put_first = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 8.0, "prompt": "a quiet room",
             "duration": 6.0, "state_in": "она у двери, руки пусты",
             "state_out": "меч лежит на столе, она у окна"},
            {"tag": "chorus", "start": 8.0, "end": 16.0, "prompt": "the same room, later",
             "duration": 7.0, "state_in": "меч лежит на столе, она у окна",
             "state_out": "она выходит в коридор, меч остаётся на столе"},
        ],
    })
    assert status == 200, put_first

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    result = _run_scenario_passport_check(base_url, pid, 1, "the same room, much later")

    assert result["putStatus"] == 200, result
    sent = result["putBody"]["scenario_scenes"]
    assert len(sent) == 2, (
        f"the whole list is replaced by a PUT -- both scenes must be sent: {sent}")
    assert sent[1]["prompt"] == "the same room, much later", (
        "the edit itself must still reach the server -- otherwise this test proves nothing about "
        f"the passport riding alongside it: {sent[1]}")
    assert sent[0]["state_in"] == "она у двери, руки пусты", sent[0]
    assert sent[0]["state_out"] == "меч лежит на столе, она у окна", sent[0]
    assert sent[1]["state_in"] == "меч лежит на столе, она у окна", (
        "the EDITED scene's own passport must ride along with the edit, not be dropped: "
        f"{sent[1]!r}")
    assert sent[1]["state_out"] == "она выходит в коридор, меч остаётся на столе", sent[1]

    final = result["finalProject"]["scenario_scenes"]
    assert final[0]["state_in"] == "она у двери, руки пусты", final[0]
    assert final[1]["state_out"] == "она выходит в коридор, меч остаётся на столе", final[1]


@_needs_node_for_scenario_race
def test_the_gate_grows_no_passport_widget(_serve, monkeypatch):
    """The other half of the decision: this wave deliberately adds NO UI control for the passport
    (the read-only display was cut by review -- "ВЫРЕЗАНО этой волной"). `collectScenarioScenes`
    passes the fields through, and that is all. Pinned so a later wave adding a widget has to come
    back here and say so on purpose rather than by accident -- the fields are carried by the
    on-disk fallback precisely *because* nothing renders them, and a half-added widget that renders
    a passport without collecting it would silently erase every one it displayed.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    status, _ = srv._request("PUT", f"/api/projects/{pid}/scenario", {
        "scenario_scenes": [
            {"tag": "verse", "start": 0.0, "end": 16.0, "prompt": "a quiet room", "duration": 8.0,
             "state_in": "она у двери", "state_out": "меч лежит на столе"},
        ],
    })
    assert status == 200

    base_url = f"http://{web.LOOPBACK}:{srv.port}"
    result = _run_scenario_passport_check(base_url, pid, 0, "a quiet room, edited")
    rendered = result["renderedHtml"]
    assert "scenario-state-in" not in rendered and "scenario-state-out" not in rendered, \
        "a passport widget appeared in the gate -- collectScenarioScenes must be taught to read it"


# == Passport wave, part 3: gluing `state_in` into the built prompt ==============================
#
# The passport is only worth writing if the video model actually reads it. SPEC §5: "`state_in`
# вклеивается в промпт сцены (как библия): видеомодель получает стартовое состояние текстом, а не
# догадкой."
#
# **Position, not just presence** (S2-1 review). An H3 prompt is three labelled fields in one
# string, `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`. A
# state block appended to the *end* of that string lands inside `non_diegetic_music` -- a
# description of the world's furniture read as an instruction about the score. It belongs at the
# end of the description field instead, which is why the glue is a helper that cuts the prompt at
# its first `"\n\noverall_soundscape:"` rather than a `+`.


def _built_prompts(scenario_scenes, duration):
    return [s["prompt"] for s in web.build_clip_scenes(
        _scenario_track(duration), scenario_scenes=scenario_scenes)]


def _h3_prompt(description, *, marker=True, sep="\n"):
    """`sep` defaults to a SINGLE newline -- the form the live model actually writes (night-4:
    80/80 field labels separated by one `\n`; night-3 used `\n\n` -- 61/61). The glue helper
    must catch both, so tests exercise the live form by default and the double form explicitly
    (review of the passport wave, B1: a `\n\n`-only marker missed 40/40 real prompts and the
    fixture's own `\n\n` hid it)."""
    tail = (f"{sep}overall_soundscape: distant rain on stone.{sep}"
            "non_diegetic_music: a low sustained cello.")
    return f"integrated_multimodal_description: {description}" + (tail if marker else "")


def test_state_in_is_glued_at_the_end_of_the_description_not_onto_the_end_of_the_prompt():
    """S2-1: a tail concat puts the world's state inside `non_diegetic_music`, where H3 reads it
    as a note about the score. The block has to sit at the END of
    `integrated_multimodal_description` -- after everything the scene's own description says, and
    before the `overall_soundscape:` label that starts the next field.
    """
    prompt = _h3_prompt("[Shot 1] Aldred crosses the empty hall.")
    (built,) = _built_prompts(
        [_scenario_scene("verse", 0.0, 8.0, prompt,
                         state_in="the sword lies flat on the oak table")], 8.0)

    block = " State at the first frame: the sword lies flat on the oak table."
    assert block in built, f"the passport never reached the prompt at all: {built!r}"
    assert built.index(block) < built.index("overall_soundscape:"), (
        "the state block landed AFTER the description field -- a tail concat puts it inside "
        f"non_diegetic_music, which is exactly the defect this helper exists to avoid: {built!r}")
    assert built.startswith("integrated_multimodal_description: [Shot 1] Aldred crosses the "
                            "empty hall." + block + "\n"), (
        f"the block must sit at the very end of the description, not spliced mid-sentence: "
        f"{built!r}")
    assert built.endswith("non_diegetic_music: a low sustained cello."), (
        f"gluing the passport must not disturb the two sound fields at all: {built!r}")


def test_a_prompt_with_no_soundscape_marker_takes_the_state_block_as_a_tail():
    """A hand-written or malformed scene prompt with no `overall_soundscape:` label at all has no
    description field to end -- the whole string is the description. The block goes on the end,
    which is the correct place for that shape, rather than the glue silently doing nothing.
    """
    (built,) = _built_prompts(
        [_scenario_scene("verse", 0.0, 8.0, _h3_prompt("a bare room", marker=False),
                         state_in="she stands at the door")], 8.0)
    assert built.endswith(" State at the first frame: she stands at the door."), built


def test_an_empty_state_in_glues_nothing_at_all():
    """The retro contract for every scenario written before this wave: with no passport, the built
    prompt is byte for byte what it always was. An empty passport that still glued " State at the
    first frame: ." would change all 40 prompts of an existing project.
    """
    prompt = _h3_prompt("[Shot 1] a bare room")
    without_key = _scenario_scene("verse", 0.0, 8.0, prompt)
    empty = _scenario_scene("verse", 0.0, 8.0, prompt, state_in="", state_out="меч на столе")
    assert _built_prompts([without_key], 8.0) == [prompt]
    assert _built_prompts([empty], 8.0) == [prompt]
    assert "State at the first frame" not in _built_prompts([empty], 8.0)[0]


def test_only_the_first_piece_of_a_split_section_carries_the_state_block():
    """`_split_long_segment` cuts an over-length section into consecutive clips that all share one
    prompt. They are one shot, played through: only the first piece actually starts at the state
    the passport describes. Pieces 2+ start mid-action, and telling H3 that the sword is back on
    the table at the top of piece 3 would re-stage the shot -- the same reasoning `fresh_start`
    already gets ("true on at most the first piece") and the same reason those pieces carry the
    continuation clause instead.
    """
    prompt = _h3_prompt("[Shot 1] a single continuous shot of rain")
    built = _built_prompts(
        [_scenario_scene("verse", 0.0, 23.0, prompt, state_in="the sword lies on the table")],
        23.0)
    assert len(built) == 3
    assert "State at the first frame: the sword lies on the table." in built[0]
    for piece in built[1:]:
        assert "State at the first frame" not in piece, (
            f"a later piece of a split section must carry no state block: {piece!r}")
        assert piece.endswith(web.SPLIT_CONTINUATION_CLAUSE), (
            "the continuation clause is the tail those pieces DO carry -- it must not have been "
            f"disturbed by the passport glue: {piece!r}")


def test_a_forward_carried_fold_moves_the_state_block_onto_the_surviving_scene():
    """`_fold_short_segments` folds a run of short segments **forward** when there is no earlier
    survivor to absorb them -- i.e. at the very START of the timeline. The survivor then begins
    where the first absorbed segment began, so the world's state at its first frame is the
    absorbed segment's own `state_in`, not its own. Without `carry_state_in` the built scene would
    open with a passport describing a moment several seconds later than the frame it labels --
    worse than no passport at all, because it is confidently wrong.
    """
    short = _scenario_scene("intro", 0.0, 4.97, _h3_prompt("a short intro"), duration=4.97,
                            state_in="she stands in the doorway, the infant in her arms")
    long = _scenario_scene("verse", 4.97, 10.0, _h3_prompt("the hall"), duration=8.0,
                           state_in="she is already seated by the cradle")
    built = _built_prompts([short, long], 10.0)
    assert len(built) == 1, f"the short opening section folds forward into one scene: {built}"
    assert "State at the first frame: she stands in the doorway, the infant in her arms." \
        in built[0], (
        "the folded-away section's own state_in must ride onto the survivor -- the scene now "
        f"STARTS where that section started: {built[0]!r}")
    assert "she is already seated by the cradle" not in built[0], (
        "the survivor's own state_in describes a frame that is no longer the first one")


def test_a_backward_fold_keeps_the_surviving_scenes_own_state_block():
    """The other fold direction: a short section absorbed into the segment BEFORE it only moves
    that segment's `end`. Its first frame does not move, so its own `state_in` is still the right
    one and must not be replaced by the absorbed section's.
    """
    first = _scenario_scene("verse", 0.0, 5.0, _h3_prompt("the hall"), duration=5.0,
                            state_in="she stands in the doorway")
    short = _scenario_scene("tail", 5.0, 9.97, _h3_prompt("a short tail"), duration=4.97,
                            state_in="she is seated by the cradle")
    built = _built_prompts([first, short], 9.97)
    assert len(built) == 1, built
    assert "State at the first frame: she stands in the doorway." in built[0], built[0]
    assert "she is seated by the cradle" not in built[0], (
        "a backward fold does not move the survivor's own first frame -- its passport stands")


def test_state_out_never_reaches_a_built_scene():
    """`state_out` is the model's half of the chain and the pipeline's input for deriving the next
    scene's `state_in`. It describes the LAST frame, so gluing it into a prompt would tell H3 to
    open on the shot's own ending. It stops at the scenario list and goes no further.
    """
    built = _built_prompts(
        [_scenario_scene("verse", 0.0, 8.0, _h3_prompt("the hall"),
                         state_in="she stands at the door",
                         state_out="ЭТОГО В ПРОМПТЕ БЫТЬ НЕ ДОЛЖНО")], 8.0)
    assert "ЭТОГО В ПРОМПТЕ БЫТЬ НЕ ДОЛЖНО" not in built[0], built[0]


def test_the_procedural_path_still_builds_prompts_byte_for_byte(_serve, monkeypatch):
    """S2-3 again, this time through the glue: the procedural path's segments have no `state_in`
    key at all, and its built prompts must be exactly what they were before this wave.
    """
    srv = _serve()
    pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
    scenes = srv.post_json(f"/api/projects/{pid}/scenario/generate",
                           {"procedural": True})["project"]["scenario_scenes"]
    built = web.build_clip_scenes(_scenario_track(16.0), scenario_scenes=scenes)
    assert built and all("State at the first frame" not in s["prompt"] for s in built), built


def test_the_whole_passport_path_from_a_model_reply_to_the_built_prompt(_serve, monkeypatch):
    """End to end, with nothing simulated between the steps: an LLM reply carrying passports ->
    `/scenario/generate` (which derives the chain) -> a `PUT` whose body carries *exactly* the
    fields `app.js`'s own `collectScenarioScenes` sends, and nothing else -> `approve/scenario`,
    which really runs `build_clip_scenes` -> the prompts actually written into `project.json`.

    The `PUT` body is the load-bearing part: it is the only representation of the page in this
    test, and every field the page forgets to send is a field the server folds to `""` and the
    scenario loses. If `collectScenarioScenes` ever stops carrying the passport, this test goes
    red at the last assertion even though every route involved answered 200.
    """
    reply = _scenario_turn_payload([
        {"tag": "verse", "start": 0.0, "end": 8.0,
         "scene": {"prompt": _h3_prompt("[Shot 1] Aldred carries the baby through the hall"),
                   "duration": 6.0, "fresh_start": None,
                   "state_in": "Aldred stands at the door, the infant in his arms",
                   "state_out": "the sword lies flat on the oak table, Aldred by the window"}},
        {"tag": "chorus", "start": 8.0, "end": 16.0,
         "scene": {"prompt": _h3_prompt("[Shot 1] Aldred alone in the empty hall"),
                   "duration": 7.0, "fresh_start": None,
                   "state_in": "ЭТО МОДЕЛЬ ПРИДУМАЛА, ДЕРИВАТ ДОЛЖЕН ЭТО ЗАТЕРЕТЬ",
                   "state_out": "the hall is empty, the sword still on the table"}},
    ])
    fake = _FakeLlama(chat_payload=reply)
    try:
        srv = _serve(providers_port=fake.port)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        generated = srv.post_json(f"/api/projects/{pid}/scenario/generate", {})
    finally:
        fake.close()

    stored = generated["project"]["scenario_scenes"]
    assert stored[1]["state_in"] == "the sword lies flat on the oak table, Aldred by the window", (
        "the derivation must have replaced the model's own second state_in already at generate "
        f"time: {stored[1]}")

    # exactly the shape `collectScenarioScenes` builds -- same keys, same order, nothing more
    body = [{"tag": s["tag"], "start": s["start"], "end": s["end"], "prompt": s["prompt"],
             "duration": s["duration"], "fresh_start": s["fresh_start"],
             "state_in": s["state_in"], "state_out": s["state_out"]} for s in stored]
    status, put = srv._request("PUT", f"/api/projects/{pid}/scenario",
                               {"scenario_scenes": body})
    assert status == 200, put

    approved = srv.post_json(f"/api/projects/{pid}/approve/scenario", {})
    prompts = [s["prompt"] for s in approved["project"]["scenes"]]
    assert len(prompts) == 2, prompts
    for i, expected in enumerate([
            "Aldred stands at the door, the infant in his arms",
            "the sword lies flat on the oak table, Aldred by the window"]):
        block = f" State at the first frame: {expected}."
        assert block in prompts[i], f"scene {i} carries no passport: {prompts[i]!r}"
        assert prompts[i].index(block) < prompts[i].index("overall_soundscape:"), (
            f"scene {i}'s passport landed outside the description field: {prompts[i]!r}")
    assert "ЭТО МОДЕЛЬ ПРИДУМАЛА" not in " ".join(prompts)


def test_state_block_lands_before_the_soundscape_label_in_both_live_separator_forms():
    """B1 of the passport-wave review: night-4's model wrote `\ndescription-label:` with a SINGLE
    newline (80/80 labels), night-3 with a double one (61/61) -- the glue must catch both, or the
    passport lands inside `non_diegetic_music` on every scene of a whole night run.
    """
    for sep in ("\n", "\n\n"):
        prompt = _h3_prompt("[Shot 1] the hall", sep=sep)
        (built,) = _built_prompts(
            [_scenario_scene("verse", 0.0, 8.0, prompt, state_in="the sword lies on the table")],
            8.0)
        block = " State at the first frame: the sword lies on the table."
        assert block in built, f"sep={sep!r}: the passport never reached the prompt: {built!r}"
        assert built.index(block) < built.index("overall_soundscape:"), (
            f"sep={sep!r}: the block landed after the description field: {built!r}")


def test_a_state_in_containing_a_field_label_cannot_forge_a_second_sound_field():
    """Injection guard (review, low): the model writes the passport as free text, so a state_in
    that happens to contain a newline plus `overall_soundscape:` would otherwise produce a prompt
    with TWO pairs of sound fields inside the description. Newlines in the glued block are
    flattened to spaces -- the passport is one sentence of the description, never new lines.
    """
    (built,) = _built_prompts(
        [_scenario_scene("verse", 0.0, 8.0, _h3_prompt("the hall"),
                         state_in="a candle burns\noverall_soundscape: forged silence")], 8.0)
    # Нейтрализация — это схлопывание \n: инжектированная метка остаётся словами внутри
    # описания (метка поля обязана стоять в начале строки, см. _SOUNDSCAPE_MARKER_RX).
    line_start_labels = re.findall(r"(?m)^[ \t]*overall_soundscape[ \t]*:", built)
    assert len(line_start_labels) == 1, (
        f"the injected text forged a second line-start sound field: {built!r}")
    assert "forged silence" in built.split("\noverall_soundscape:")[0], (
        f"the flattened injection must sit inside the description, before the real label: "
        f"{built!r}")


def test_a_mid_chain_gap_in_the_passport_is_logged_not_swallowed(caplog):
    """Review M1: the model forgot scene 0's `state_out` but wrote a meaningful `state_in` on
    scene 1 -- the derivation rightly overwrites it with the empty string (the chain is the
    pipeline's, not the model's), but silently: no warning fired, the human at the gate sees a
    hole in the passport and the log says nothing. A gap ANYWHERE in the chain deserves the same
    warning the scene-0/fresh_start branch already gives.
    """
    import logging
    turn = {"reply": "ok", "scenario": {"style_block": "warm light", "sections": [
        {"tag": "a", "start": 0, "end": 8,
         "scene": {"prompt": "p0", "duration": 8, "fresh_start": True,
                   "state_in": "she is by the cradle", "state_out": None}},
        {"tag": "b", "start": 8, "end": 16,
         "scene": {"prompt": "p1", "duration": 8, "fresh_start": False,
                   "state_in": "модель это придумала", "state_out": "the hall is empty"}},
    ]}}
    with caplog.at_level(logging.WARNING, logger="h3_48gb.web"):
        scenes, _ = web._scenario_turn_to_scenes(turn)
    assert scenes[1]["state_in"] == "", "the derivation must still overwrite with the empty chain"
    gap_warnings = [r for r in caplog.records if "state_out" in r.getMessage()]
    assert gap_warnings, (
        "an empty `state_out` feeding a chained scene must be logged -- the gate human needs to "
        "know the hole came from the model, not from the pipeline")
