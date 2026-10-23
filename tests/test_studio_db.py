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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3     # 001 + 002 (S1) + 003 (S2)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_twice_is_a_no_op(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
    assert db.migrate(conn) == 3
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


# ---- fix round 1 -------------------------------------------------------------------------------

def _scene_insert(conn: sqlite3.Connection, sid: str, idx: int, frames: int = 192) -> None:
    conn.execute("INSERT INTO scene (id, project_id, idx, mode, requested_s, frames, seed, "
                 "created_at) VALUES (?, 'p', ?, 'refs', 8.0, ?, 1, 'n')", (sid, idx, frames))


def _project(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO project (id, title, frame_format, created_at) "
                 "VALUES ('p', 'Лес', '16:9', 'n')")


def test_a_committed_tx_is_visible_to_a_second_connection(tmp_path):
    conn = _fresh(tmp_path)
    other = db.connect(tmp_path / "s.sqlite")
    with db.tx(conn):
        conn.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
    assert [tuple(r) for r in other.execute("SELECT key, value FROM setting")] == [("k", "v")]


def test_tx_holds_the_write_lock_from_its_first_statement(tmp_path):
    conn = _fresh(tmp_path)
    other = db.connect(tmp_path / "s.sqlite")
    other.execute("PRAGMA busy_timeout=0")
    with db.tx(conn):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            other.execute("BEGIN IMMEDIATE")
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")


def test_read_is_one_snapshot_that_ignores_later_commits(tmp_path):
    conn = _fresh(tmp_path)
    other = db.connect(tmp_path / "s.sqlite")
    with db.read(conn):
        assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0
        other.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
        assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 1


@pytest.mark.parametrize("frames", [73, 345])
def test_scene_frames_accept_both_bounds(tmp_path, frames):
    conn = _fresh(tmp_path)
    _project(conn)
    _scene_insert(conn, "s", 0, frames)
    assert conn.execute("SELECT frames FROM scene").fetchone()[0] == frames


def test_a_new_scene_starts_at_edit_version_zero(tmp_path):
    conn = _fresh(tmp_path)
    _project(conn)
    _scene_insert(conn, "s", 0)
    assert tuple(conn.execute("SELECT edit_version, revision, state_version FROM scene")
                 .fetchone()) == (0, 1, 1)


def test_live_scene_order_is_unique_until_the_scene_is_deleted(tmp_path):
    conn = _fresh(tmp_path)
    _project(conn)
    _scene_insert(conn, "s1", 0)
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE constraint failed: scene.project_id, "
                                                     r"scene.idx"):
        _scene_insert(conn, "s2", 0)
    conn.execute("UPDATE scene SET deleted_at = 'n' WHERE id = 's1'")
    _scene_insert(conn, "s2", 0)
    assert [r[0] for r in conn.execute("SELECT id FROM scene WHERE deleted_at IS NULL")] == ["s2"]


def test_connect_returns_rows_and_works_from_another_thread(tmp_path):
    import threading
    conn = _fresh(tmp_path)
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
    box: list = []

    def work():
        try:
            box.append(conn.execute("SELECT 7 AS n").fetchone()["n"])
        except BaseException as exc:                # noqa: BLE001 - reported by the assert below
            box.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert box == [7]


def test_a_failing_commit_leaves_no_open_transaction(tmp_path):
    class NoCommit(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    conn = sqlite3.connect(tmp_path / "c.sqlite", isolation_level=None, factory=NoCommit)
    conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        with db.tx(conn):
            conn.execute("INSERT INTO t VALUES (1)")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_tx_keeps_the_original_error_when_the_body_already_rolled_back(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError, match="boom"):
        with db.tx(conn):
            conn.execute("ROLLBACK")
            raise ValueError("boom")


def test_take_inputs_json_needs_its_sha256_in_the_same_update(tmp_path):
    conn = _fresh(tmp_path)
    _seed_scene(conn)
    with pytest.raises(sqlite3.IntegrityError, match=r"take.inputs_json is write-once \(И2\)"):
        conn.execute("UPDATE take SET inputs_json = '{}' WHERE id = 't'")
    assert conn.execute("SELECT inputs_json FROM take").fetchone()[0] is None


def test_backup_of_a_missing_database_fails_and_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError, match="studio database not found"):
        db.backup_once(tmp_path / "gone.sqlite", tmp_path / "backups",
                       now=datetime(2026, 10, 21, 10, 0, 0))
    assert not (tmp_path / "gone.sqlite").exists()
    assert not (tmp_path / "backups").exists()


def test_backup_with_keep_zero_drops_every_older_copy(tmp_path):
    _fresh(tmp_path)
    start = datetime(2026, 10, 21, 10, 0, 0)
    for hour in range(3):
        db.backup_once(tmp_path / "s.sqlite", tmp_path / "backups",
                       now=start + timedelta(hours=hour))
    last = db.backup_once(tmp_path / "s.sqlite", tmp_path / "backups",
                          now=start + timedelta(hours=3), keep=0)
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [last.name]


def test_outdir_inside_models_is_refused_in_any_letter_case(tmp_path):
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    if not (home / "MODELS").exists():
        pytest.skip("case-sensitive filesystem: MODELS is a different directory here")
    for bad in (home / "MODELS" / "out", home / "Models" / "out", home / "MODELS"):
        with pytest.raises(config.ConfigError, match="не может лежать внутри"):
            config.resolve_outdir(str(bad), home=home)


def test_new_id_has_the_prefix_and_sixteen_hex_digits_and_does_not_repeat():
    import re
    from h3_48gb.studio import ids
    assert re.fullmatch(r"p_[0-9a-f]{16}", ids.new_id("p"))
    assert len({ids.new_id("s") for _ in range(500)}) == 500


def test_now_iso_is_utc_with_milliseconds():
    import re
    from h3_48gb.studio import ids
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", ids.now_iso())


def test_studio_error_body_is_the_envelope():
    from h3_48gb.studio.errors import StudioError
    err = StudioError(409, "stale_revision", "сцена изменилась", {"scene_id": "s"})
    assert (err.status, err.code, err.message) == (409, "stale_revision", "сцена изменилась")
    assert err.body() == {"error": {"code": "stale_revision", "message": "сцена изменилась",
                                    "detail": {"scene_id": "s"}}}
    assert StudioError(404, "nf", "m").body() == {"error": {"code": "nf", "message": "m",
                                                            "detail": {}}}


def test_migration_002_adds_source_auto_to_a_database_made_by_001_and_keeps_its_rows(tmp_path):
    only_001 = tmp_path / "mig"
    only_001.mkdir()
    (only_001 / "001_core.sql").write_text((db.MIGRATIONS / "001_core.sql").read_text())
    conn = db.connect(tmp_path / "old.sqlite")
    assert db.migrate(conn, only_001) == 1
    _seed_scene(conn)
    conn.execute("UPDATE scene SET source_frame_kind = 'prev_last_frame', action_text = 'walks' "
                 "WHERE id = 's'")
    conn.commit()
    assert "source_auto" not in [r[1] for r in conn.execute("PRAGMA table_info(scene)")]
    # up to 002 only: later migrations (003 in S2, 004 in S2b) must not change this test
    (only_001 / "002_scene_source_auto.sql").write_text(
        (db.MIGRATIONS / "002_scene_source_auto.sql").read_text())
    assert db.migrate(conn, only_001) == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    row = conn.execute("SELECT id, idx, action_text, source_frame_kind, seed, source_auto "
                       "FROM scene").fetchall()
    assert [tuple(r) for r in row] == [("s", 0, "walks", "prev_last_frame", 11, 0)]
    assert conn.execute("SELECT COUNT(*) FROM take").fetchone()[0] == 1


def test_migration_003_adds_the_task_links_of_slice_s2_and_keeps_the_rows(tmp_path):
    """Plan S2–S3 R-S2-1 (revision 4: file 003, after S1's 002): task.project_id,
    task.result_asset_id, task.engine_version, task.template_sha256, asset.origin_task_id,
    render_plan.used_at — exact column lists, so a dropped column fails here. Migrated over a
    database made by 001 + 002 with a row in it, and only up to 003, so S2b's 004 does not touch
    this test."""
    upto = tmp_path / "mig"
    upto.mkdir()
    names = ["001_core.sql", "002_scene_source_auto.sql", "003_tasks.sql"]
    for name in names[:2]:
        (upto / name).write_text((db.MIGRATIONS / name).read_text())
    conn = db.connect(tmp_path / "old.sqlite")
    assert db.migrate(conn, upto) == 2
    _seed_scene(conn)
    conn.commit()
    (upto / names[2]).write_text((db.MIGRATIONS / names[2]).read_text())
    assert db.migrate(conn, upto) == 3

    def columns(table):
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    assert columns("task")[-4:] == ["project_id", "result_asset_id", "engine_version",
                                    "template_sha256"]
    assert columns("asset")[-1:] == ["origin_task_id"]
    assert columns("render_plan")[-1:] == ["used_at"]
    assert [r[1] for r in conn.execute("PRAGMA index_list(task)") if r[1].startswith("task_")] \
        == ["task_by_project", "task_by_status"]
    assert [tuple(r) for r in conn.execute(
        "SELECT id, status, project_id, result_asset_id, engine_version, template_sha256 "
        "FROM task")] == [("k", "blocked", None, None, None, None)]
