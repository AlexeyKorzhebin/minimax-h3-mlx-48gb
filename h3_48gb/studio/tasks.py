"""Tasks (spec §8.5): their public shape (O13), the project's list for the snapshot (O3) and the
logical cancel (O14). The worker owns every other state change."""
from __future__ import annotations

import json
import sqlite3
import time

from h3_48gb.studio import db, events, ids
from h3_48gb.studio.errors import StudioError

TERMINAL = ("succeeded", "failed", "cancelled")
#: Not yet taken by the worker: O14 ends them at once. Everything else is the worker's to stop
#: (it watches `cancel_requested_at` in every wait and while the engine runs).
NOT_STARTED = ("queued", "blocked")
#: Spec §8.4 `queue_order` (revision 3.1, §14 seam 5): ONE order for the worker's `_claim` and for
#: the plan's count of model switches (revision 4.1: the plan simulates the queue with the same
#: `queue_key`). Priority first (pictures 10, renders 0); pictures strictly in the order they were
#: queued (the screen shows the presses in order); among renders the one on the model the engine
#: holds goes first (a switch costs 16–38 s, R-S2-14), then age.
IMAGE_PRIORITY = 10


def queue_key(*, kind: str, priority: int, template: str | None, seq: int,
              loaded_template: str | None) -> tuple:
    return (-priority, 0 if kind == "image" or template == loaded_template else 1, seq)


def queued_in_order(conn: sqlite3.Connection, *, kinds, loaded_template: str | None,
                    limit: int | None = None) -> list[sqlite3.Row]:
    """Queued tasks of these kinds, the next one first; `queue_seq` is the row's age (rowid)."""
    marks = ", ".join("?" for _ in kinds)
    rows = conn.execute(f"SELECT rowid AS queue_seq, * FROM task WHERE status = 'queued' "
                        f"AND kind IN ({marks})", tuple(kinds)).fetchall()
    rows.sort(key=lambda r: queue_key(
        kind=r["kind"], priority=r["priority"],
        template=json.loads(r["payload_json"]).get("template"), seq=r["queue_seq"],
        loaded_template=loaded_template))
    return rows if limit is None else rows[:limit]


def task_out(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    asset = row["result_asset_id"]
    return {"id": row["id"], "kind": row["kind"], "project_id": row["project_id"],
            "status": row["status"], "template": payload.get("template"),
            "wait_reason": row["wait_reason"], "progress_step": row["progress_step"],
            "progress_total": row["progress_total"], "error": row["error"],
            "attempts": row["attempts"], "engine_stop": row["engine_stop"],
            "engine_version": row["engine_version"], "template_sha256": row["template_sha256"],
            "cancel_requested": row["cancel_requested_at"] is not None,
            "depends_on_task_id": row["depends_on_task_id"],
            "result_asset_id": asset,
            "result_url": f"/api/studio/assets/{asset}/file" if asset else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise StudioError(404, "task_not_found", "Задача не найдена", {"task_id": task_id})
    return row


def get_task(conn: sqlite3.Connection, task_id: str) -> dict:
    return task_out(task_row(conn, task_id))


def project_tasks(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    # rowid = insertion order: the tasks of one O11 share their created_at to the millisecond
    rows = conn.execute("SELECT * FROM task WHERE project_id = ? ORDER BY rowid",
                        (project_id,)).fetchall()
    return [task_out(r) for r in rows]


def cancel_task(conn: sqlite3.Connection, task_id: str, *, reason: str) -> dict:
    """O14: the logical cancel — from now on the task's result is never attached, even if the
    engine finishes it (§8.5). A task the worker has not taken ends here; one it has taken is
    stopped by the worker (engine cancel, up to 30 s)."""
    with db.tx(conn):
        row = task_row(conn, task_id)
        if row["status"] in TERMINAL:
            raise StudioError(409, "task_finished", "Задача уже завершена",
                              {"task": task_out(row)})
        now = ids.now_iso()
        conn.execute("UPDATE task SET cancel_requested_at = ?, updated_at = ? WHERE id = ?",
                     (time.time(), now, task_id))
        events.emit(conn, "task_cancel_requested", project_id=row["project_id"], task_id=task_id,
                    payload={"reason": reason})
        if row["status"] in NOT_STARTED:
            finish_cancelled(conn, task_id, project_id=row["project_id"], error=None)
        return task_out(task_row(conn, task_id))


def finish_cancelled(conn: sqlite3.Connection, task_id: str, *, project_id: str | None,
                     error: str | None, engine_stop: str | None = None) -> None:
    """Inside a transaction: the task and its take (slice S3) end as cancelled."""
    conn.execute("UPDATE task SET status = 'cancelled', error = COALESCE(?, error), "
                 "engine_stop = ?, wait_reason = NULL, updated_at = ? WHERE id = ?",
                 (error, engine_stop, ids.now_iso(), task_id))
    conn.execute("UPDATE take SET status = 'cancelled' WHERE task_id = ? "
                 "AND status IN ('pending_input', 'queued', 'running')", (task_id,))
    events.emit(conn, "task_cancelled", project_id=project_id, task_id=task_id,
                payload={"engine_stop": engine_stop})
