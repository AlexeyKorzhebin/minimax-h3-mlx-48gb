"""SQLite for the studio (spec §5, §6): one file, WAL, `busy_timeout=5000`, foreign keys on,
migrations `migrations/NNN_name.sql` applied by `PRAGMA user_version`, each in one transaction."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: no implicit BEGIN; every write goes through `tx()` below.
    # check_same_thread=False: FastAPI may run a sync dependency and the endpoint that uses it on
    # different threadpool threads; one connection still serves one request at a time.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrations(directory: Path = MIGRATIONS) -> list[tuple[int, Path]]:
    found = []
    for path in sorted(directory.iterdir()):
        match = _MIGRATION_NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    numbers = [number for number, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migrations must be numbered 001.. without gaps, got {numbers}")
    return found


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS) -> int:
    """Apply every migration above `user_version`; return the version reached. A migration that
    fails leaves neither its tables nor its version number behind."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, path in migrations(directory):
        if number <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={number};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = number
    return current


@contextmanager
def tx(conn: sqlite3.Connection):
    """One write transaction. `BEGIN IMMEDIATE` takes the write lock up front, so two writers
    queue on `busy_timeout` instead of failing at their first UPDATE."""
    if conn.in_transaction:
        raise RuntimeError("nested studio transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:             # the body may have closed it; keep ITS exception
            conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:                   # a failed COMMIT can leave the transaction open
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def read(conn: sqlite3.Connection):
    """One read snapshot: every SELECT inside sees the same committed state (И10 needs the
    snapshot and its `last_seq` to agree)."""
    if conn.in_transaction:
        raise RuntimeError("nested studio transaction")
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


def backup_once(db_path: Path, dest_dir: Path, *, now: datetime, keep: int = 7) -> Path:
    """Consistent copy through `Connection.backup()` (a plain file copy of a WAL database is not
    consistent, spec §5); keeps the newest `keep` copies by name."""
    if not db_path.exists():                # sqlite3.connect() would silently create an empty one
        raise FileNotFoundError(f"studio database not found: {db_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"studio-{now:%Y%m%d-%H%M%S}.sqlite"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    copies = sorted(dest_dir.glob("studio-*.sqlite"))
    # keep=0 means "no older copies"; the one just made is never deleted (the caller gets its path).
    for old in copies[:max(len(copies) - keep, 0)]:
        if old != target:
            old.unlink()
    return target
