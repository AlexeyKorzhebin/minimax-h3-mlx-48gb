"""`Idempotency-Key` (spec §7): O11, O12 (and O15, O19, O24 later) run at most once per key. The
check, the operation and the stored answer are one transaction, so a retry after a crash either
finds the answer or finds nothing and runs the operation afresh."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from h3_48gb.studio import db, ids
from h3_48gb.studio.errors import StudioError

KEEP = timedelta(hours=24)


def request_sha256(route: str, body: dict) -> str:
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{route}\n{canonical}".encode()).hexdigest()


def run_once(conn: sqlite3.Connection, key: str | None, request_sha: str, operation,
             *, status: int = 201) -> tuple[int, dict]:
    """`operation(conn) -> dict` runs inside the transaction. The same key with the same request
    returns the stored answer; with another request — 422 `idempotency_mismatch`. A failing
    operation stores nothing: the key stays free for a corrected retry."""
    if not key or not key.strip():
        raise StudioError(400, "idempotency_key_required", "Нужен заголовок Idempotency-Key")
    with db.tx(conn):
        cutoff = (datetime.now(timezone.utc) - KEEP).isoformat(timespec="milliseconds")
        conn.execute("DELETE FROM op_log WHERE created_at < ?", (cutoff,))
        row = conn.execute("SELECT request_sha256, response_json, http_status FROM op_log "
                           "WHERE idempotency_key = ?", (key,)).fetchone()
        if row is not None:
            if row["request_sha256"] != request_sha:
                raise StudioError(422, "idempotency_mismatch",
                                  "Этот ключ уже использован для другого запроса", {"key": key})
            return row["http_status"], json.loads(row["response_json"])
        answer = operation(conn)
        conn.execute("INSERT INTO op_log (idempotency_key, request_sha256, response_json, "
                     "http_status, created_at) VALUES (?, ?, ?, ?, ?)",
                     (key, request_sha, json.dumps(answer, ensure_ascii=False), status,
                      ids.now_iso()))
        return status, answer
