"""Storage for one project: layout, durable writes, the project lock, and the stage/scene/track
mutations the worker, the web API and the UI (tasks 3-7) will read and write through.

Mirrors `test_queue.py`'s discipline: a lock test that never actually contends the lock proves
nothing, so `test_the_project_lock_actually_blocks_a_second_exclusive_holder` drives `flock` from a
*separate process* via `test_queue`'s own `_external_lock`/`_answer_within` helpers -- a thread in
this process would silently re-acquire its own process's lock.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from h3_48gb import project as p
from test_queue import _external_lock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -- create_project: path shape, id claim, kind validation --------------------------------------


def test_create_project_returns_a_project_with_the_documented_path_shape(tmp_path):
    project = p.create_project(tmp_path, "video", "My First Video")
    assert project.path.name == "project.json"
    assert project.path.parent.parent == tmp_path / "projects"
    assert project.path.is_file()
    # <YYYYMMDD-HHMM>-<слаг>
    assert __import__("re").fullmatch(r"\d{8}-\d{4}-[a-z0-9-]+", project.path.parent.name)
    assert "my-first-video" in project.path.parent.name


def test_create_project_rejects_an_unknown_kind(tmp_path):
    with pytest.raises(p.ProjectError):
        p.create_project(tmp_path, "movie", "x")


@pytest.mark.parametrize("kind", ["video", "clip", "song"])
def test_create_project_initializes_the_documented_model(tmp_path, kind):
    project = p.create_project(tmp_path, kind, "Title")
    assert project.kind == kind
    assert project.title == "Title"
    assert project.scenes == []
    assert set(project.stages) == {"script", "track", "scenes", "assembly"}
    assert all(status == "draft" for status in project.stages.values())
    assert project.track["sections"] == []
    assert project.track["status"] == "draft"
    assert project.assembly["audio_mode"] in p.ASSEMBLY_AUDIO_MODES
    assert project.assembly["final_path"] is None


def test_create_project_disambiguates_a_colliding_directory_name(tmp_path):
    now = lambda: "2026-08-18T12:00:00"
    first = p.create_project(tmp_path, "video", "dup", now=now)
    second = p.create_project(tmp_path, "video", "dup", now=now)
    assert first.path != second.path
    assert first.path.parent.name != second.path.parent.name


# -- round-trip write/read -----------------------------------------------------------------------


def test_round_trip_write_and_read(tmp_path):
    """Populate every field the model documents, save, reload from a fresh `Project` instance
    (not the one that wrote it), and check every field survives -- this is the test that would
    fail if `save`/`load_project` silently dropped or renamed a field.
    """
    project = p.create_project(tmp_path, "clip", "Song Clip")
    project.scenes = [
        {"idx": 0, "prompt": "a", "duration": 5.0, "status": "pending",
         "job_id": None, "clip_path": None, "keyframe_path": None},
        {"idx": 1, "prompt": "b", "duration": 4.5, "status": "pending",
         "job_id": None, "clip_path": None, "keyframe_path": None},
    ]
    project.track = {
        "lyrics": "la la la", "caption": "upbeat", "wav": "/x/track.wav",
        "mp3": "/x/track.mp3", "mastered_mp3": "/x/track.mastered.mp3",
        "sections": [{"tag": "verse", "start": 0.0, "end": 12.0}],
        "status": "approved",
    }
    project.assembly = {"audio_mode": "song", "final_path": "/x/final.mp4"}
    project.save()

    loaded = p.load_project(project.path)
    assert loaded.kind == project.kind
    assert loaded.title == project.title
    assert loaded.created_at == project.created_at
    assert loaded.stages == project.stages
    assert loaded.scenes == project.scenes
    assert loaded.track == project.track
    assert loaded.assembly == project.assembly


def test_load_project_accepts_the_project_directory_too(tmp_path):
    project = p.create_project(tmp_path, "song", "mp3 only")
    loaded = p.load_project(project.path.parent)
    assert loaded.path == project.path


def test_load_project_raises_on_a_missing_file(tmp_path):
    with pytest.raises(p.ProjectError):
        p.load_project(tmp_path / "projects" / "nope")


def test_save_is_atomic_tmp_plus_rename(tmp_path, monkeypatch):
    """Not "no temp file is left" -- that passes with a plain write_text. The invariant is that a
    crash between the temp write and the rename leaves the OLD content, never a mixture, and never
    a torn file. Mirrors `test_durable_write_survives_a_crash_after_the_temp_file` in
    `test_queue.py`, against `Project.save` this time rather than `write_json_durably` directly.
    """
    project = p.create_project(tmp_path, "video", "atomic")
    before = project.path.read_text(encoding="utf-8")

    import os
    real_replace = os.replace

    def _boom(*a, **kw):
        raise OSError("simulated crash between temp write and rename")

    monkeypatch.setattr(os, "replace", _boom)
    project.title = "changed but never lands"
    with pytest.raises(OSError):
        project.save()
    monkeypatch.setattr(os, "replace", real_replace)

    assert project.path.read_text(encoding="utf-8") == before
    leftovers = list(project.path.parent.glob(".project.json.tmp-*"))
    assert leftovers == [], f"a failed save must not leave a temp file behind: {leftovers}"


# -- the project lock actually blocks --------------------------------------------------------


def test_the_project_lock_actually_blocks_a_second_exclusive_holder(tmp_path):
    """Not "a lock is taken" in the abstract -- driven from a *separate process*
    (`_external_lock`), because a thread in this process would silently re-acquire its own
    process's flock and prove nothing (flock is per open-file-description, but two `os.open`
    calls in the same process still contend for real -- the point is a naive re-implementation
    using a plain `threading.Lock` instead of `flock` would also pass a same-process test).
    """
    project = p.create_project(tmp_path, "video", "locked")
    with _external_lock(project.path.parent, "LOCK_EX", name="project.lock"):
        done = threading.Event()
        threading.Thread(target=lambda: (project.approve_stage("script"), done.set()),
                          daemon=True).start()
        assert not done.wait(0.5), "approve_stage returned while the project lock was held externally"
    assert done.wait(5), "approve_stage never returned after the external lock was released"
    assert project.stages["script"] == "approved"


def test_list_projects_waits_for_an_externally_held_project_lock(tmp_path):
    project = p.create_project(tmp_path, "video", "locked")
    with _external_lock(project.path.parent, "LOCK_EX", name="project.lock"):
        done = threading.Event()
        threading.Thread(target=lambda: (p.list_projects(tmp_path), done.set()),
                          daemon=True).start()
        assert not done.wait(0.5), "list_projects did not wait for the project lock"
    assert done.wait(5), "list_projects never returned after the external lock was released"


# -- approve_stage --------------------------------------------------------------------------------


def test_approve_stage_sets_that_stage_to_approved(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    project.approve_stage("script")
    assert project.stages["script"] == "approved"
    assert project.stages["scenes"] == "draft"  # untouched
    reloaded = p.load_project(project.path)
    assert reloaded.stages["script"] == "approved"


def test_approve_stage_rejects_an_unknown_stage(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    with pytest.raises(p.ProjectError):
        project.approve_stage("nope")


# -- set_scene_status -----------------------------------------------------------------------------


def _project_with_scenes(tmp_path, kind="video", n=3):
    project = p.create_project(tmp_path, kind, "scenes")
    project.scenes = [
        {"idx": i, "prompt": f"scene {i}", "duration": 5.0, "status": "pending",
         "job_id": None, "clip_path": None, "keyframe_path": None}
        for i in range(n)
    ]
    project.save()
    return project


def test_set_scene_status_updates_status_and_optional_fields(tmp_path):
    project = _project_with_scenes(tmp_path)
    project.set_scene_status(0, "running", job_id="job-1")
    assert project.scenes[0]["status"] == "running"
    assert project.scenes[0]["job_id"] == "job-1"
    assert project.scenes[0]["clip_path"] is None

    project.set_scene_status(0, "done", clip_path="/x/0.mp4", keyframe_path="/x/0.png")
    assert project.scenes[0]["status"] == "done"
    assert project.scenes[0]["job_id"] == "job-1"  # left alone, not cleared
    assert project.scenes[0]["clip_path"] == "/x/0.mp4"
    assert project.scenes[0]["keyframe_path"] == "/x/0.png"

    # unrelated scenes untouched
    assert project.scenes[1]["status"] == "pending"

    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["status"] == "done"


def test_set_scene_status_job_id_none_explicitly_clears_it(tmp_path):
    """C1 (fix round 1, 2026-08-18 review): unlike `clip_path`/`keyframe_path`, `job_id` treats an
    *explicit* `None` as "clear it", not "leave it as it was" -- `assemble._submit_next_scene`'s
    own rollback needs this to actually undo a claim's placeholder job id when `submit()` fails.
    Omitting the keyword entirely (the previous test, above) must still mean "unchanged".
    """
    project = _project_with_scenes(tmp_path)
    project.set_scene_status(0, "running", job_id="job-1")

    project.set_scene_status(0, "pending", job_id=None)

    assert project.scenes[0]["status"] == "pending"
    assert project.scenes[0]["job_id"] is None
    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["job_id"] is None


def test_set_scene_status_rejects_an_unknown_idx(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)
    with pytest.raises(p.ProjectError):
        project.set_scene_status(99, "done")


def test_set_scene_status_rejects_an_unknown_status(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)
    with pytest.raises(p.ProjectError):
        project.set_scene_status(0, "sleeping")


# -- next_pending_scene: the sequential-dependency contract ---------------------------------------


def test_next_pending_scene_returns_scene_zero_first(tmp_path):
    project = _project_with_scenes(tmp_path, n=3)
    scene = project.next_pending_scene()
    assert scene["idx"] == 0


def test_next_pending_scene_returns_none_while_scene_zero_is_running(tmp_path):
    """The whole point of the method: a worker must not be handed scene 1 while scene 0's clip is
    still in flight -- automatic keyframes need scene 0's actual last frame, not a guess.
    """
    project = _project_with_scenes(tmp_path, n=3)
    project.set_scene_status(0, "running", job_id="job-1")
    assert project.next_pending_scene() is None


def test_next_pending_scene_advances_once_the_previous_scene_is_done(tmp_path):
    project = _project_with_scenes(tmp_path, n=3)
    project.set_scene_status(0, "done", clip_path="/x/0.mp4")
    scene = project.next_pending_scene()
    assert scene["idx"] == 1


def test_next_pending_scene_stays_blocked_after_a_failure(tmp_path):
    project = _project_with_scenes(tmp_path, n=3)
    project.set_scene_status(0, "failed")
    assert project.next_pending_scene() is None


def test_next_pending_scene_returns_none_once_every_scene_is_done(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)
    project.set_scene_status(0, "done")
    project.set_scene_status(1, "done")
    assert project.next_pending_scene() is None


def test_next_pending_scene_returns_none_for_a_project_with_no_scenes(tmp_path):
    project = p.create_project(tmp_path, "song", "mp3 only")
    assert project.next_pending_scene() is None


# -- invalidate_scene_chain: idx and everything after it -------------------------------------------


def test_invalidate_scene_chain_resets_idx_and_every_later_scene(tmp_path):
    project = _project_with_scenes(tmp_path, n=4)
    for i in range(4):
        project.set_scene_status(i, "done", job_id=f"job-{i}", clip_path=f"/x/{i}.mp4",
                                  keyframe_path=f"/x/{i}.png")

    project.invalidate_scene_chain(2)

    assert project.scenes[0]["status"] == "done"
    assert project.scenes[0]["job_id"] == "job-0"
    assert project.scenes[1]["status"] == "done"
    assert project.scenes[1]["job_id"] == "job-1"

    for i in (2, 3):
        assert project.scenes[i]["status"] == "pending"
        assert project.scenes[i]["job_id"] is None
        assert project.scenes[i]["clip_path"] is None
        assert project.scenes[i]["keyframe_path"] is None

    reloaded = p.load_project(project.path)
    assert reloaded.scenes[2]["status"] == "pending"
    assert reloaded.scenes[3]["status"] == "pending"


def test_invalidate_scene_chain_rejects_an_unknown_idx(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)
    with pytest.raises(p.ProjectError):
        project.invalidate_scene_chain(99)


def test_invalidate_scene_chain_lets_next_pending_scene_pick_it_back_up(tmp_path):
    project = _project_with_scenes(tmp_path, n=3)
    project.set_scene_status(0, "done")
    project.set_scene_status(1, "done")
    project.set_scene_status(2, "done")
    assert project.next_pending_scene() is None

    project.invalidate_scene_chain(1)
    scene = project.next_pending_scene()
    assert scene["idx"] == 1


# -- list_projects: survives a broken project.json --------------------------------------------


def test_list_projects_returns_every_valid_project(tmp_path):
    a = p.create_project(tmp_path, "video", "a")
    b = p.create_project(tmp_path, "song", "b")
    found = {proj.path for proj in p.list_projects(tmp_path)}
    assert found == {a.path, b.path}


def test_list_projects_with_no_projects_directory_returns_empty(tmp_path):
    assert p.list_projects(tmp_path) == []


def test_list_projects_skips_a_broken_project_json_with_a_warning_not_a_crash(tmp_path):
    good = p.create_project(tmp_path, "video", "good")
    broken_dir = tmp_path / "projects" / "20260101-0000-broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "project.json").write_text("{not json", encoding="utf-8")

    with pytest.warns(UserWarning):
        found = p.list_projects(tmp_path)

    assert [proj.path for proj in found] == [good.path]


def test_list_projects_skips_a_project_json_missing_required_fields_with_a_warning(tmp_path):
    good = p.create_project(tmp_path, "video", "good")
    broken_dir = tmp_path / "projects" / "20260101-0000-shapeless"
    broken_dir.mkdir(parents=True)
    (broken_dir / "project.json").write_text(json.dumps({"kind": "video"}), encoding="utf-8")

    with pytest.warns(UserWarning):
        found = p.list_projects(tmp_path)

    assert [proj.path for proj in found] == [good.path]


def test_list_projects_ignores_a_project_directory_that_has_no_project_json_yet(tmp_path):
    """Not every subdirectory under `projects/` is necessarily a finished claim -- a directory
    mid-`create_project` (mkdir landed, project.json has not been written yet) must not be
    reported as broken.
    """
    (tmp_path / "projects" / "20260101-0000-inflight").mkdir(parents=True)
    assert p.list_projects(tmp_path) == []


# -- no mlx dependency ------------------------------------------------------------------------


# -- C1: list_projects/load_project on type-corrupt project.json (not just missing/unparseable) --


_TYPE_CORRUPTIONS = [
    pytest.param({"scenes": "notalist"}, id="scenes-not-a-list"),
    pytest.param({"stages": ["a"]}, id="stages-not-an-object"),
    pytest.param({"track": 5}, id="track-not-an-object"),
    pytest.param({"assembly": None}, id="assembly-not-an-object"),
    pytest.param({"scenes": [1, 2]}, id="scenes-elements-not-objects"),
]


def _corrupt(good: p.Project, overrides: dict) -> dict:
    data = good.as_dict()
    data.update(overrides)
    return data


@pytest.mark.parametrize("overrides", _TYPE_CORRUPTIONS)
def test_list_projects_skips_a_project_json_with_a_wrong_field_type(tmp_path, overrides):
    good = p.create_project(tmp_path, "video", "good")
    broken_dir = tmp_path / "projects" / "20260101-0000-broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "project.json").write_text(json.dumps(_corrupt(good, overrides)),
                                              encoding="utf-8")

    with pytest.warns(UserWarning):
        found = p.list_projects(tmp_path)

    assert [proj.path for proj in found] == [good.path]


@pytest.mark.parametrize("overrides", _TYPE_CORRUPTIONS)
def test_load_project_raises_on_a_project_json_with_a_wrong_field_type(tmp_path, overrides):
    good = p.create_project(tmp_path, "video", "good")
    good.path.write_text(json.dumps(_corrupt(good, overrides)), encoding="utf-8")
    with pytest.raises(p.ProjectError):
        p.load_project(good.path)


def test_load_project_does_not_create_a_directory_for_a_missing_project(tmp_path):
    """A probe for a project that was never created (a stale id, a typo'd path) must not create
    the directory or a `project.lock` file as a side effect of failing -- `_project_lock` itself
    `mkdir`s its target, which is only safe once a mutator already knows the project directory
    exists.
    """
    missing = tmp_path / "projects" / "nope"
    with pytest.raises(p.ProjectError):
        p.load_project(missing)
    assert not missing.exists()
    assert not (tmp_path / "projects").exists()


# -- C2: set_stage_status / update_track / update_assembly ---------------------------------------


def test_set_stage_status_sets_the_given_status(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    project.set_stage_status("track", "running")
    assert project.stages["track"] == "running"
    assert project.stages["script"] == "draft"  # untouched
    reloaded = p.load_project(project.path)
    assert reloaded.stages["track"] == "running"


def test_set_stage_status_rejects_an_unknown_stage(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    with pytest.raises(p.ProjectError):
        project.set_stage_status("nope", "running")


def test_set_stage_status_rejects_an_unknown_status(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    with pytest.raises(p.ProjectError):
        project.set_stage_status("track", "sleeping")


def test_update_track_partially_updates_and_leaves_other_fields_alone(tmp_path):
    project = p.create_project(tmp_path, "song", "song")
    project.update_track(lyrics="la la la", status="awaiting_approval")
    assert project.track["lyrics"] == "la la la"
    assert project.track["status"] == "awaiting_approval"
    assert project.track["caption"] is None  # untouched

    project.update_track(caption="upbeat")
    assert project.track["lyrics"] == "la la la"  # still there, not clobbered
    assert project.track["caption"] == "upbeat"

    reloaded = p.load_project(project.path)
    assert reloaded.track["lyrics"] == "la la la"
    assert reloaded.track["caption"] == "upbeat"


def test_update_track_can_explicitly_clear_a_field_to_none(tmp_path):
    project = p.create_project(tmp_path, "song", "song")
    project.update_track(lyrics="la la la")
    project.update_track(lyrics=None)
    assert project.track["lyrics"] is None


def test_update_track_rejects_an_unknown_field(tmp_path):
    project = p.create_project(tmp_path, "song", "song")
    with pytest.raises(p.ProjectError):
        project.update_track(not_a_real_field="x")


def test_a_fresh_track_defaults_source_to_generate_and_undersung_to_false(tmp_path):
    """Task 3 (design spec addendum, "импортированный трек"): every track before this addendum was
    a Music3 generation, so a project that never sets `source` explicitly must still read as one --
    the default has to be `"generate"`, not `None` or a missing key a caller has to special-case.
    `undersung` defaults `False` for the same reason: nothing has flagged a warning on a track that
    has not been through a song job yet.
    """
    project = p.create_project(tmp_path, "song", "song")
    assert project.track["source"] == "generate"
    assert project.track["undersung"] is False


def test_update_track_accepts_source_and_undersung(tmp_path):
    """`_TRACK_FIELDS` grew these two fields for task 3's worker glue
    (`h3_48gb.worker._run_song_job`), which writes both through `update_track` once a song job
    finishes -- `source` when a project is set up for import (task 6, out of this module's scope)
    and `undersung` from `songrun.SongResult.undersung` after every song job, generated or
    imported.
    """
    project = p.create_project(tmp_path, "song", "song")
    project.update_track(source="import", undersung=True)
    assert project.track["source"] == "import"
    assert project.track["undersung"] is True

    reloaded = p.load_project(project.path)
    assert reloaded.track["source"] == "import"
    assert reloaded.track["undersung"] is True


def test_a_fresh_track_defaults_seed_and_duration_to_none(tmp_path):
    """Fix round 1, I1/I4 (2026-08-18 review): `seed` is `None` until a song job actually rolls one
    (the worker fills it in, see `h3_48gb.worker._run_song_job`) and `duration` is `None` until a
    song job actually produces one -- neither is a fact a fresh, never-run track has.
    """
    project = p.create_project(tmp_path, "song", "song")
    assert project.track["seed"] is None
    assert project.track["duration"] is None


def test_update_track_accepts_seed_and_duration(tmp_path):
    """`_TRACK_FIELDS` grew `seed`/`duration` for the worker's song-job dispatch
    (`h3_48gb.worker._run_song_job`), which records the seed it actually used (I1, so a duplicate
    take is reproducible) and the track's actual length (I4, from `songrun.SongResult.duration`)
    through the same locked `update_track` call as everything else a song job produces.
    """
    project = p.create_project(tmp_path, "song", "song")
    project.update_track(seed=12345, duration=87.5)
    assert project.track["seed"] == 12345
    assert project.track["duration"] == pytest.approx(87.5)

    reloaded = p.load_project(project.path)
    assert reloaded.track["seed"] == 12345
    assert reloaded.track["duration"] == pytest.approx(87.5)


def test_update_assembly_partially_updates_and_leaves_other_fields_alone(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    project.update_assembly(final_path="/x/final.mp4")
    assert project.assembly["final_path"] == "/x/final.mp4"
    assert project.assembly["audio_mode"] in p.ASSEMBLY_AUDIO_MODES  # untouched

    reloaded = p.load_project(project.path)
    assert reloaded.assembly["final_path"] == "/x/final.mp4"


def test_update_assembly_rejects_an_unknown_audio_mode(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    with pytest.raises(p.ProjectError):
        project.update_assembly(audio_mode="nonsense")


def test_update_assembly_rejects_an_unknown_field(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    with pytest.raises(p.ProjectError):
        project.update_assembly(not_a_real_field="x")


def test_two_project_objects_do_not_clobber_each_others_writes(tmp_path):
    """The whole point of the locked mutators' re-read-under-lock: a caller holding a `Project`
    object loaded before another caller's write must not overwrite that write with its own stale
    in-memory copy of the rest of the file when it writes back. Two independently loaded `Project`
    objects for the *same* project.json, each mutating a different part of the model
    (`set_scene_status` vs `update_track`) -- both changes must be present on disk afterward.
    """
    created = _project_with_scenes(tmp_path, n=2)
    a = p.load_project(created.path)
    b = p.load_project(created.path)

    a.set_scene_status(0, "done", clip_path="/x/0.mp4")
    b.update_track(lyrics="la la la", status="approved")

    reloaded = p.load_project(created.path)
    assert reloaded.scenes[0]["status"] == "done"
    assert reloaded.scenes[0]["clip_path"] == "/x/0.mp4"
    assert reloaded.track["lyrics"] == "la la la"
    assert reloaded.track["status"] == "approved"


# -- I1: invalidate_scene_chain also resets the scenes/assembly stages and final_path ------------


def test_invalidate_scene_chain_resets_scenes_and_assembly_stages_and_final_path(tmp_path):
    project = _project_with_scenes(tmp_path, n=3)
    for i in range(3):
        project.set_scene_status(i, "done", clip_path=f"/x/{i}.mp4")
    project.set_stage_status("scenes", "done")
    project.set_stage_status("assembly", "done")
    project.update_assembly(final_path="/x/final.mp4")

    project.invalidate_scene_chain(1)

    assert project.stages["scenes"] == "draft"
    assert project.stages["assembly"] == "draft"
    assert project.assembly["final_path"] is None
    # untouched
    assert project.stages["script"] == "draft"

    reloaded = p.load_project(project.path)
    assert reloaded.stages["scenes"] == "draft"
    assert reloaded.stages["assembly"] == "draft"
    assert reloaded.assembly["final_path"] is None


# -- I2: claim_next_scene -- atomic find-and-claim in one lock hold ------------------------------


def test_claim_next_scene_claims_and_returns_the_next_ready_scene(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)
    scene = project.claim_next_scene("job-1")
    assert scene["idx"] == 0
    assert scene["status"] == "running"
    assert scene["job_id"] == "job-1"
    assert project.scenes[0]["status"] == "running"
    assert project.scenes[0]["job_id"] == "job-1"

    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["status"] == "running"
    assert reloaded.scenes[0]["job_id"] == "job-1"


def test_claim_next_scene_returns_none_when_nothing_is_ready(tmp_path):
    project = _project_with_scenes(tmp_path, n=1)
    project.set_scene_status(0, "running", job_id="job-1")
    assert project.claim_next_scene("job-2") is None


def test_claim_next_scene_is_atomic_under_a_race(tmp_path):
    """A barrier forces both threads into `claim_next_scene` as close to simultaneously as
    Python's scheduler allows, on a project with exactly one ready scene. `claim_next_scene`'s
    single exclusive-lock hold (read -> pick -> claim -> write) is what guarantees exactly one of
    the two threads is handed the scene and the other gets `None` -- calling `next_pending_scene`
    then `set_scene_status` as two separate operations would leave a window where both threads
    could read the scene as still `pending` before either claims it.
    """
    project = _project_with_scenes(tmp_path, n=1)
    barrier = threading.Barrier(2)
    results = [None, None]

    def worker(slot, job_id):
        barrier.wait()
        results[slot] = project.claim_next_scene(job_id)

    threads = [threading.Thread(target=worker, args=(0, "job-a")),
               threading.Thread(target=worker, args=(1, "job-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    claimed = [r for r in results if r is not None]
    none_results = [r for r in results if r is None]
    assert len(claimed) == 1, f"expected exactly one claim, got {results}"
    assert len(none_results) == 1, f"expected exactly one None, got {results}"

    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["status"] == "running"
    assert reloaded.scenes[0]["job_id"] in ("job-a", "job-b")
    assert reloaded.scenes[0]["job_id"] == claimed[0]["job_id"]


# -- C1 (fix round 1, 2026-08-18 review): claim_next_scene(expected_idx=...) ---------------------


def test_claim_next_scene_refuses_and_writes_nothing_when_expected_idx_does_not_match(tmp_path):
    """The exact check C1 needed: a caller that built a job for scene 1 must be able to refuse the
    claim, inside the lock, before writing anything, if the scene that is actually next-ready by
    the time the lock is acquired is scene 0 instead (an invalidation raced in between). Before
    this parameter existed, `claim_next_scene` would claim scene 0 under whatever `job_id` the
    caller passed (a job that was actually built for scene 1) and only let the caller notice
    afterward -- by which point scene 0 was already corrupted.
    """
    project = _project_with_scenes(tmp_path, n=2)

    result = project.claim_next_scene("job-for-scene-1", expected_idx=1)

    assert result is None
    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["status"] == "pending", "must not have been claimed under the wrong id"
    assert reloaded.scenes[0]["job_id"] is None


def test_claim_next_scene_still_claims_when_expected_idx_matches(tmp_path):
    project = _project_with_scenes(tmp_path, n=2)

    result = project.claim_next_scene("job-1", expected_idx=0)

    assert result["idx"] == 0
    assert result["status"] == "running"
    reloaded = p.load_project(project.path)
    assert reloaded.scenes[0]["status"] == "running"
    assert reloaded.scenes[0]["job_id"] == "job-1"


def test_claim_next_scene_without_expected_idx_keeps_the_old_unchecked_behaviour(tmp_path):
    """Every caller before this parameter existed passes no `expected_idx` at all -- must keep
    claiming whatever is next-ready, exactly as before.
    """
    project = _project_with_scenes(tmp_path, n=2)

    result = project.claim_next_scene("job-1")

    assert result["idx"] == 0
    assert result["status"] == "running"


def test_next_pending_scene_has_no_side_effect_on_self(tmp_path):
    """`next_pending_scene` is a read-only preview: calling it must not silently refresh `self`'s
    own attributes from disk the way the locked mutators do.
    """
    project = _project_with_scenes(tmp_path, n=1)
    other = p.load_project(project.path)
    other.set_scene_status(0, "done")

    project.next_pending_scene()  # must not pull in `other`'s write as a side effect
    assert project.scenes[0]["status"] == "pending", (
        "next_pending_scene must not mutate self.scenes as a side effect")


# -- I3: as_dict / unknown top-level fields survive save() ----------------------------------------


def test_unknown_top_level_fields_survive_a_save_round_trip(tmp_path):
    project = p.create_project(tmp_path, "video", "x")
    data = json.loads(project.path.read_text(encoding="utf-8"))
    data["custom_field_from_a_future_task"] = {"whatever": True}
    project.path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = p.load_project(project.path)
    assert reloaded.as_dict()["custom_field_from_a_future_task"] == {"whatever": True}

    reloaded.approve_stage("script")  # any locked mutator's write-back

    on_disk = json.loads(project.path.read_text(encoding="utf-8"))
    assert on_disk["custom_field_from_a_future_task"] == {"whatever": True}
    assert on_disk["stages"]["script"] == "approved"


# -- I4: _slug transliterates Cyrillic titles ------------------------------------------------------


def test_slug_transliterates_a_cyrillic_title():
    assert p._slug("Мой ролик") == "moy-rolik"


def test_create_project_with_a_cyrillic_title_gets_a_transliterated_slug(tmp_path):
    project = p.create_project(tmp_path, "video", "Мой ролик")
    assert "moy-rolik" in project.path.parent.name


# -- I5: concurrent writes from separate OS processes lose nothing --------------------------------


def test_concurrent_writes_from_separate_processes_lose_nothing(tmp_path):
    """Real OS processes, not threads (see `_external_lock`'s own docstring on why a same-process
    thread test is a much weaker proof of `flock` behaving correctly). `n` processes each claiming
    a distinct scene at once must all land: every mutator re-reads the whole file under the lock
    before writing the whole file back, so a genuinely enforced project lock is what keeps one
    process's write from being silently overwritten by another's stale in-memory copy of the rest
    of the file.
    """
    n = 6
    project = _project_with_scenes(tmp_path, n=n)
    script = (
        "import sys\n"
        "from h3_48gb import project as p\n"
        "path, idx = sys.argv[1], int(sys.argv[2])\n"
        "p.load_project(path).set_scene_status(idx, 'done', job_id=f'job-{idx}')\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(project.path), str(i)],
                          cwd=str(PROJECT_ROOT))
        for i in range(n)
    ]
    for proc in procs:
        assert proc.wait(timeout=30) == 0, f"worker process for scene {procs.index(proc)} failed"

    reloaded = p.load_project(project.path)
    for i in range(n):
        assert reloaded.scenes[i]["status"] == "done", f"scene {i} lost its update"
        assert reloaded.scenes[i]["job_id"] == f"job-{i}", f"scene {i} lost its job_id"


def test_project_module_does_not_import_mlx():
    """`h3_48gb.project` will be imported by the worker and the web server on every project page
    load; loading it must never pull in the 33B-parameter transformer stack. Run in a subprocess
    so this test's own process (which may have already imported mlx via another test module)
    cannot hide a leak. Mirrors `test_queue_module_does_not_import_mlx`.
    """
    code = "import sys; import h3_48gb.project; print('mlx' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.project must not import mlx"
