"""Studio settings (spec §6 `SETTING`, §8.2): addresses of the executor and the dispatcher. A row in
`setting` overrides the default; the defaults are alex-neuro's. On the Mac:
`python -m h3_48gb.studio setting comfy.url http://127.0.0.1:8188` and so on."""
from __future__ import annotations

import sqlite3

from h3_48gb.studio import db

DEFAULTS = {
    "comfy.url": "http://127.0.0.1:8189",
    "comfy.output_dir": "/home/alex/Outputs/comfy-h3",
    # dispatcher:<engine> on alex-neuro, local-mac on the Mac (spec §8.4)
    "comfy.scope": "dispatcher:comfy-studio",
    "dispatcher.url": "http://127.0.0.1:8790",
    # the estimate key (§3.1: executor, template, machine, canvas, frames, steps)
    "machine": "alex-neuro",
}


def get(conn: sqlite3.Connection, key: str) -> str:
    if key not in DEFAULTS:
        raise KeyError(f"unknown studio setting {key!r}")
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else DEFAULTS[key]


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {key: get(conn, key) for key in DEFAULTS}


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"unknown studio setting {key!r}")
    with db.tx(conn):
        conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
