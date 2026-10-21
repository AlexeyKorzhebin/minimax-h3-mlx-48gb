"""Explicit studio operations (spec §7). The master and MCP both call these and nothing else.
Every write is one `db.tx` that also writes its event (И8)."""
from __future__ import annotations

import sqlite3

from h3_48gb.studio import db, events, ids
from h3_48gb.studio.errors import StudioError

FRAME_FORMATS = ("16:9", "9:16")


def _project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise StudioError(404, "project_not_found", "Проект не найден",
                          {"project_id": project_id})
    return row


def require_project(conn: sqlite3.Connection, project_id: str) -> None:
    _project_row(conn, project_id)


def project_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "revision": row["revision"], "title": row["title"],
            "frame_format": row["frame_format"], "idea_text": row["idea_text"],
            "style_block": row["style_block"], "created_at": row["created_at"]}


def create_project(conn: sqlite3.Connection, *, title: str, frame_format: str,
                   idea_text: str = "") -> dict:
    project_id = ids.new_id("p")
    with db.tx(conn):
        conn.execute(
            "INSERT INTO project (id, revision, title, frame_format, idea_text, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (project_id, title, frame_format, idea_text, ids.now_iso()))
        events.emit(conn, "project_created", project_id=project_id,
                    payload={"title": title, "frame_format": frame_format})
        return project_out(_project_row(conn, project_id))


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, "
        " (SELECT COUNT(*) FROM scene s WHERE s.project_id = p.id AND s.deleted_at IS NULL)"
        "   AS scenes_total, "
        " (SELECT COUNT(*) FROM scene s WHERE s.project_id = p.id AND s.deleted_at IS NULL"
        "   AND s.approved_final_take_id IS NOT NULL) AS scenes_done "
        "FROM project p ORDER BY p.created_at DESC, p.id").fetchall()
    return [{**project_out(r), "scenes_total": r["scenes_total"], "scenes_done": r["scenes_done"]}
            for r in rows]


def _check_revision(row: sqlite3.Row, expected: int, current: dict) -> None:
    if row["revision"] != expected:
        raise StudioError(409, "revision_conflict",
                          "Данные изменились в другом окне — обновите и повторите",
                          {"current": current})


def update_project(conn: sqlite3.Connection, project_id: str, *, expected_revision: int,
                   title: str | None = None, frame_format: str | None = None) -> dict:
    with db.tx(conn):
        row = _project_row(conn, project_id)
        _check_revision(row, expected_revision, project_out(row))
        changes: dict = {}
        if title is not None and title != row["title"]:
            changes["title"] = title
        if frame_format is not None and frame_format != row["frame_format"]:
            done = conn.execute(
                "SELECT COUNT(*) FROM take t JOIN scene s ON s.id = t.scene_id "
                "WHERE s.project_id = ? AND t.status = 'done'", (project_id,)).fetchone()[0]
            if done:
                raise StudioError(409, "format_locked",
                                  "Формат кадра нельзя менять после первого готового дубля",
                                  {"done_takes": done})
            changes["frame_format"] = frame_format
        if not changes:
            return project_out(row)
        sets = ", ".join(f"{name} = ?" for name in changes)
        conn.execute(f"UPDATE project SET {sets}, revision = revision + 1 WHERE id = ?",
                     (*changes.values(), project_id))
        events.emit(conn, "project_updated", project_id=project_id, payload=changes)
        return project_out(_project_row(conn, project_id))


def snapshot(conn: sqlite3.Connection, project_id: str) -> dict:
    """O3: the project with everything the master draws, and the `last_seq` it is consistent
    with (И10) — read in one snapshot so an event committed in between cannot be lost. Scenes
    and cast arrive in slice S1; takes and tasks in S2–S3."""
    with db.read(conn):
        project = project_out(_project_row(conn, project_id))
        return {"project": project, "scenes": [], "cast": [], "takes": [], "tasks": [],
                "last_seq": events.last_seq(conn)}
