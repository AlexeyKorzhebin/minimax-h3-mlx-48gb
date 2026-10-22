"""The event journal (spec §5 «События», И8): a row per change, written inside the change's own
transaction, read by the SSE stream (O21) after a sequence number."""
from __future__ import annotations

import json
import sqlite3

from h3_48gb.studio import ids


def emit(conn: sqlite3.Connection, kind: str, *, project_id: str | None = None,
         scene_id: str | None = None, task_id: str | None = None,
         payload: dict | None = None) -> int:
    if not conn.in_transaction:
        raise RuntimeError(f"event {kind!r} emitted outside a transaction (И8)")
    cur = conn.execute(
        "INSERT INTO event (ts, kind, project_id, scene_id, task_id, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ids.now_iso(), kind, project_id, scene_id, task_id,
         json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)))
    return cur.lastrowid


def last_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM event").fetchone()[0]


def read_after(conn: sqlite3.Connection, after: int, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, ts, kind, project_id, scene_id, task_id, payload_json FROM event "
        "WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "project_id": r["project_id"],
             "scene_id": r["scene_id"], "task_id": r["task_id"],
             "payload": json.loads(r["payload_json"])} for r in rows]


def sse_frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, sort_keys=True)
    return f"id: {event['seq']}\nevent: {event['kind']}\ndata: {data}\n\n"
