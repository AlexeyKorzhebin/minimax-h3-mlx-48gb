"""И8: the event row is written in the same transaction as the change it describes."""
from __future__ import annotations

import pytest

from h3_48gb.studio import db, events, ops


def _conn(tmp_path):
    conn = db.connect(tmp_path / "s.sqlite")
    db.migrate(conn)
    return conn


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_event_is_emitted_inside_the_uncommitted_change(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    seen = {}
    real_emit = events.emit

    def spy(c, kind, **kw):
        other = db.connect(tmp_path / "s.sqlite")
        try:
            seen.update(kind=kind, in_tx=c.in_transaction,
                        own=_count(c, "project"), other=_count(other, "project"))
        finally:
            other.close()
        return real_emit(c, kind, **kw)

    monkeypatch.setattr(events, "emit", spy)
    ops.create_project(conn, title="Лес", frame_format="16:9")
    assert seen == {"kind": "project_created", "in_tx": True, "own": 1, "other": 0}


def test_a_failure_after_the_event_leaves_neither_change_nor_event(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    real_emit = events.emit

    def emit_then_fail(c, kind, **kw):
        real_emit(c, kind, **kw)
        raise RuntimeError("boom after emit")

    monkeypatch.setattr(events, "emit", emit_then_fail)
    with pytest.raises(RuntimeError, match="boom after emit"):
        ops.create_project(conn, title="Лес", frame_format="16:9")
    assert (_count(conn, "project"), _count(conn, "event")) == (0, 0)


def test_emit_outside_a_transaction_is_refused(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(RuntimeError, match=r"event 'x' emitted outside a transaction \(И8\)"):
        events.emit(conn, "x")


def test_sequence_is_monotonic_and_read_after_is_strict(tmp_path):
    conn = _conn(tmp_path)
    a = ops.create_project(conn, title="A", frame_format="16:9")
    ops.update_project(conn, a["id"], expected_revision=1, title="A2")
    ops.create_project(conn, title="B", frame_format="9:16")
    got = [(e["seq"], e["kind"], e["project_id"] == a["id"]) for e in events.read_after(conn, 0)]
    assert got == [(1, "project_created", True), (2, "project_updated", True),
                   (3, "project_created", False)]
    assert [e["seq"] for e in events.read_after(conn, 2)] == [3]
    assert events.read_after(conn, 1)[0]["payload"] == {"title": "A2"}


def test_sse_frame_is_the_wire_format(tmp_path):
    frame = events.sse_frame({"seq": 7, "kind": "project_created", "payload": {"title": "Лес"}})
    assert frame == ('id: 7\nevent: project_created\n'
                     'data: {"kind": "project_created", "payload": {"title": "Лес"}, "seq": 7}\n\n')
