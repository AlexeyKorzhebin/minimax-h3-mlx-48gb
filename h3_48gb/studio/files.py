"""Where a stored file is (spec §6 `asset.path`; S1 `assets.asset_file`): `asset.path` is relative
to the studio home, so moving `<outdir>` keeps every file. The home is the folder of the database
(S1 `create_app`: `home / "studio.sqlite"`; the worker opens the same file), so code that holds
only a connection — the plan (O10) and the materialization (O11, the worker) — finds a file
without a new parameter (plan S2–S3 revision 4: S1 stores `assets/<id>.<ext>`, not a full path)."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def studio_home(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()["file"]).resolve().parent


def asset_path(conn: sqlite3.Connection, stored: str) -> Path:
    """The file `stored` names; never outside the home (an absolute path or `..` is refused)."""
    home = studio_home(conn)
    if Path(stored).is_absolute():
        raise ValueError(f"asset path must be relative to the studio home: {stored!r}")
    path = (home / stored).resolve()
    if not path.is_relative_to(home):
        raise ValueError(f"asset path leaves the studio home: {stored!r}")
    return path


def stored_path(home: Path, path: Path) -> str:
    """What goes into `asset.path` for a file the studio wrote under its home."""
    home = Path(home).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(home):
        raise ValueError(f"{str(path)!r} is outside the studio home")
    return str(path.relative_to(home))
