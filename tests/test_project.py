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
