"""O1-O4 and the event journal: exact content, not just shape."""
from __future__ import annotations

import re

import pytest

from h3_48gb.studio import db, events, ids, ops
from h3_48gb.studio.errors import StudioError


def _conn(tmp_path):
    conn = db.connect(tmp_path / "s.sqlite")
    db.migrate(conn)
    return conn


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _scene(conn, project_id, idx, *, deleted=False, done=False):
    sid = ids.new_id("s")
    with db.tx(conn):
        conn.execute(
            "INSERT INTO scene (id, project_id, idx, mode, requested_s, frames, seed, created_at, "
            "deleted_at) VALUES (?, ?, ?, 'text', 5.0, 73, 1, ?, ?)",
            (sid, project_id, idx, ids.now_iso(), ids.now_iso() if deleted else None))
    return sid


def _take(conn, scene_id, status="done"):
    tid = ids.new_id("t")
    with db.tx(conn):
        conn.execute(
            "INSERT INTO take (id, scene_id, scene_revision, tier, executor, template, status, "
            "created_at) VALUES (?, ?, 1, 'final', 'x', 'x', ?, ?)",
            (tid, scene_id, status, ids.now_iso()))
    return tid


def _approve(conn, scene_id, take_id):
    with db.tx(conn):
        conn.execute("UPDATE scene SET approved_final_take_id = ? WHERE id = ?", (take_id, scene_id))


def test_create_project_event_and_response_are_exact(tmp_path):
    conn = _conn(tmp_path)
    out = ops.create_project(conn, title="Лес", frame_format="9:16", idea_text="идея")
    assert {k: v for k, v in out.items() if k not in ("id", "created_at")} == {
        "revision": 1, "title": "Лес", "frame_format": "9:16", "idea_text": "идея",
        "style_block": ""}
    assert re.fullmatch(r"p_[0-9a-f]{16}", out["id"])
    (ev,) = events.read_after(conn, 0)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", ev["ts"])
    assert {k: v for k, v in ev.items() if k not in ("seq", "ts")} == {
        "kind": "project_created", "project_id": out["id"], "scene_id": None, "task_id": None,
        "payload": {"title": "Лес", "frame_format": "9:16"}}


def test_list_projects_counts_live_and_done_scenes(tmp_path):
    conn = _conn(tmp_path)
    a = ops.create_project(conn, title="A", frame_format="16:9")
    b = ops.create_project(conn, title="B", frame_format="9:16")
    s1 = _scene(conn, a["id"], 1)
    _scene(conn, a["id"], 2)
    s3 = _scene(conn, a["id"], 3, deleted=True)
    _approve(conn, s1, _take(conn, s1))
    _approve(conn, s3, _take(conn, s3))     # deleted scene must not count as done
    got = {p["id"]: (p["title"], p["scenes_total"], p["scenes_done"])
           for p in ops.list_projects(conn)}
    assert got == {a["id"]: ("A", 2, 1), b["id"]: ("B", 0, 0)}
    assert len(ops.list_projects(conn)) == 2


def test_snapshot_last_seq_is_the_last_event(tmp_path):
    conn = _conn(tmp_path)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    ops.update_project(conn, p["id"], expected_revision=1, title="A2")
    snap = ops.snapshot(conn, p["id"])
    assert snap["last_seq"] == events.read_after(conn, 0)[-1]["seq"] == 2
    assert snap["project"]["title"] == "A2"
    assert (snap["scenes"], snap["cast"], snap["takes"], snap["tasks"]) == ([], [], [], [])


def test_update_returns_new_state_and_bumps_revision_once(tmp_path):
    conn = _conn(tmp_path)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    out = ops.update_project(conn, p["id"], expected_revision=1, title="B")
    assert (out["revision"], out["title"], out["frame_format"]) == (2, "B", "16:9")
    assert ops.snapshot(conn, p["id"])["project"] == out
    out = ops.update_project(conn, p["id"], expected_revision=2, frame_format="9:16")
    assert (out["revision"], out["frame_format"]) == (3, "9:16")
    assert [e["payload"] for e in events.read_after(conn, 0)][1:] == [
        {"title": "B"}, {"frame_format": "9:16"}]


def test_stale_revision_is_409_with_current_project(tmp_path):
    conn = _conn(tmp_path)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    ops.update_project(conn, p["id"], expected_revision=1, title="B")
    with pytest.raises(StudioError) as e:
        ops.update_project(conn, p["id"], expected_revision=1, title="C")
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    assert e.value.detail == {"current": ops.snapshot(conn, p["id"])["project"]}
    assert ops.snapshot(conn, p["id"])["project"]["title"] == "B"


def test_format_is_locked_after_first_done_take(tmp_path):
    conn = _conn(tmp_path)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    s = _scene(conn, p["id"], 1)
    _take(conn, s, status="queued")
    ops.update_project(conn, p["id"], expected_revision=1, frame_format="9:16")
    _take(conn, s, status="done")
    with pytest.raises(StudioError) as e:
        ops.update_project(conn, p["id"], expected_revision=2, frame_format="16:9")
    assert (e.value.status, e.value.code, e.value.detail) == (409, "format_locked",
                                                            {"done_takes": 1})
    assert ops.snapshot(conn, p["id"])["project"]["frame_format"] == "9:16"


def test_unknown_project_is_404(tmp_path):
    conn = _conn(tmp_path)
    for call in (lambda: ops.update_project(conn, "p_nope", expected_revision=1, title="x"),
                 lambda: ops.snapshot(conn, "p_nope"),
                 lambda: ops.require_project(conn, "p_nope")):
        with pytest.raises(StudioError) as e:
            call()
        assert (e.value.status, e.value.code, e.value.detail) == (
            404, "project_not_found", {"project_id": "p_nope"})


def test_noop_update_keeps_revision_and_writes_no_event(tmp_path):
    conn = _conn(tmp_path)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    out = ops.update_project(conn, p["id"], expected_revision=1, title="A", frame_format="16:9")
    assert out == p
    assert (ops.snapshot(conn, p["id"])["project"]["revision"], _count(conn, "event")) == (1, 1)


def test_bad_frame_format_is_400_args_invalid_and_changes_nothing(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(StudioError) as e:
        ops.create_project(conn, title="A", frame_format="4:3")
    assert (e.value.status, e.value.code) == (400, "args_invalid")
    assert (_count(conn, "project"), _count(conn, "event")) == (0, 0)
    p = ops.create_project(conn, title="A", frame_format="16:9")
    with pytest.raises(StudioError) as e:
        ops.update_project(conn, p["id"], expected_revision=1, frame_format="1:1")
    assert (e.value.status, e.value.code) == (400, "args_invalid")
    assert ops.snapshot(conn, p["id"])["project"]["revision"] == 1


def test_read_after_returns_full_events_and_honours_limit(tmp_path):
    conn = _conn(tmp_path)
    with db.tx(conn):
        seq = events.emit(conn, "k", project_id="P", scene_id="S", task_id="T", payload={"a": 1})
        events.emit(conn, "k2")
        events.emit(conn, "k3")
    assert seq == 1
    got = events.read_after(conn, 0, limit=2)
    assert [e["seq"] for e in got] == [1, 2]
    assert {k: v for k, v in got[0].items() if k != "ts"} == {
        "seq": 1, "kind": "k", "project_id": "P", "scene_id": "S", "task_id": "T",
        "payload": {"a": 1}}
    assert events.last_seq(conn) == 3
    with db.tx(conn):
        assert events.emit(conn, "k4") == 4
