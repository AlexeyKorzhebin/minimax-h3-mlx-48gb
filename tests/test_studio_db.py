"""Studio storage (spec §5, §6): migrations, pragmas, write-once and immutable rows, backups."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from h3_48gb.studio import config, db

TABLES = ["assembly", "asset", "cast_asset", "cast_member", "estimate_sample", "event", "op_log",
          "project", "proposal", "render_plan", "scene", "scene_ref", "setting", "take", "task",
          "workflow_template"]


def _fresh(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "s.sqlite")
    db.migrate(conn)
    return conn


def _seed_scene(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO project (id, title, frame_format, created_at) "
                 "VALUES ('p', 'Лес', '16:9', 'n')")
    conn.execute("INSERT INTO scene (id, project_id, idx, mode, requested_s, frames, seed, "
                 "created_at) VALUES ('s', 'p', 0, 'refs', 8.0, 192, 11, 'n')")
    conn.execute("INSERT INTO task (id, kind, executor, gpu_scope, payload_json, status, "
                 "created_at, updated_at) VALUES ('k', 'render', 'comfy', 'none', '{}', 'blocked',"
                 " 'n', 'n')")
    conn.execute("INSERT INTO take (id, scene_id, scene_revision, tier, executor, template, "
                 "status, task_id, created_at) VALUES ('t', 's', 1, 'sketch', 'comfy', "
                 "'h3-fl2va', 'pending_input', 'k', 'n')")


def test_migration_001_creates_the_path_tables_and_sets_the_version(tmp_path):
    conn = _fresh(tmp_path)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    assert names == TABLES
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_twice_is_a_no_op(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
    assert db.migrate(conn) == 1
    assert [tuple(r) for r in conn.execute("SELECT value FROM setting")] == [("v",)]


def test_a_failing_migration_leaves_no_tables_and_no_version(tmp_path):
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mig / "002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nSELECT * FROM no_such_table;")
    conn = db.connect(tmp_path / "m.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="no such table: no_such_table"):
        db.migrate(conn, mig)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")] == ["a"]
    assert conn.in_transaction is False


def test_migration_numbers_must_have_no_gaps(tmp_path):
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_a.sql").write_text("")
    (mig / "003_c.sql").write_text("")
    with pytest.raises(RuntimeError, match=r"without gaps, got \[1, 3\]"):
        db.migrations(mig)


def test_take_inputs_json_is_written_exactly_once(tmp_path):
    """И2: NULL -> value once; any other UPDATE of the field aborts — NULL -> NULL before
    materialization and any change (even to the same value) after it."""
    conn = _fresh(tmp_path)
    _seed_scene(conn)
    for sql in ("UPDATE take SET inputs_json = NULL WHERE id = 't'",
                "UPDATE take SET inputs_sha256 = 'h' WHERE id = 't'"):
        with pytest.raises(sqlite3.IntegrityError, match=r"take.inputs_json is write-once \(И2\)"):
            conn.execute(sql)                      # before materialization: NULL -> NULL refused
    conn.execute("UPDATE take SET inputs_json = '{\"a\":1}', inputs_sha256 = 'h' WHERE id = 't'")
    for sql in ("UPDATE take SET inputs_json = '{\"a\":2}' WHERE id = 't'",
                "UPDATE take SET inputs_json = '{\"a\":1}' WHERE id = 't'",
                "UPDATE take SET inputs_sha256 = 'other' WHERE id = 't'"):
        with pytest.raises(sqlite3.IntegrityError, match=r"take.inputs_json is write-once \(И2\)"):
            conn.execute(sql)
    conn.execute("UPDATE take SET status = 'queued' WHERE id = 't'")
    assert tuple(conn.execute("SELECT inputs_json, inputs_sha256, status FROM take").fetchone()) \
        == ('{"a":1}', "h", "queued")


def test_asset_rows_are_immutable(tmp_path):
    """И7 at the storage level: a new version is a new row, never an UPDATE."""
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO asset (id, kind, path, sha256, bytes, mime, origin_kind, created_at)"
                 " VALUES ('a', 'image', '/x.png', 'h', 3, 'image/png', 'upload', 'n')")
    with pytest.raises(sqlite3.IntegrityError, match=r"asset is immutable \(И7\)"):
        conn.execute("UPDATE asset SET path = '/y.png' WHERE id = 'a'")


@pytest.mark.parametrize("frames", [72, 190, 362, 56])
def test_scene_frames_must_sit_on_the_grid_inside_the_bounds(tmp_path, frames):
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO project (id, title, frame_format, created_at) "
                 "VALUES ('p', 'Лес', '16:9', 'n')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute("INSERT INTO scene (id, project_id, idx, mode, requested_s, frames, seed, "
                     "created_at) VALUES ('s', 'p', 0, 'refs', 8.0, ?, 1, 'n')", (frames,))


def test_tx_rolls_back_on_error_and_refuses_nesting(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError, match="boom"):
        with db.tx(conn):
            conn.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0
    with db.tx(conn):
        with pytest.raises(RuntimeError, match="nested studio transaction"):
            with db.tx(conn):
                pass


def test_backup_keeps_the_newest_seven_consistent_copies(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
    start = datetime(2026, 10, 21, 10, 0, 0)
    for hour in range(9):
        db.backup_once(tmp_path / "s.sqlite", tmp_path / "backups",
                       now=start + timedelta(hours=hour))
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert names == [f"studio-20261021-{h:02d}0000.sqlite" for h in range(12, 19)]
    copy = sqlite3.connect(tmp_path / "backups" / names[-1])
    assert copy.execute("SELECT key, value FROM setting").fetchall() == [("k", "v")]


def test_outdir_inside_models_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / "models" / "h3").mkdir(parents=True)
    for bad in (home / "models", home / "models" / "h3" / "out"):
        with pytest.raises(config.ConfigError, match="не может лежать внутри"):
            config.resolve_outdir(str(bad), home=home)
    assert config.resolve_outdir(str(home / "Research"), home=home) == (home / "Research").resolve()
    with pytest.raises(config.ConfigError, match="нужен --outdir"):
        config.resolve_outdir("", home=home)
