"""Studio settings (spec §6 `SETTING`, §8.2): defaults, overrides, unknown keys."""
from __future__ import annotations

import pytest

from h3_48gb.studio import db, settings

DEFAULTS = {
    "comfy.url": "http://127.0.0.1:8189",
    "comfy.output_dir": "/home/alex/Outputs/comfy-h3",
    "comfy.scope": "dispatcher:comfy-studio",
    "dispatcher.url": "http://127.0.0.1:8790",
    "machine": "alex-neuro",
}


def _conn(tmp_path):
    conn = db.connect(tmp_path / "s.sqlite")
    db.migrate(conn)
    return conn


def test_defaults_are_alex_neuros_and_exact(tmp_path):
    conn = _conn(tmp_path)
    assert settings.DEFAULTS == DEFAULTS
    assert settings.all_settings(conn) == DEFAULTS
    assert [settings.get(conn, k) for k in DEFAULTS] == list(DEFAULTS.values())


def test_put_overrides_one_key_and_a_second_put_replaces_it(tmp_path):
    conn = _conn(tmp_path)
    settings.put(conn, "comfy.url", "http://127.0.0.1:8188")
    assert settings.all_settings(conn) == {**DEFAULTS, "comfy.url": "http://127.0.0.1:8188"}
    settings.put(conn, "comfy.url", "http://127.0.0.1:9000")
    assert settings.get(conn, "comfy.url") == "http://127.0.0.1:9000"
    assert [tuple(r) for r in conn.execute("SELECT key, value FROM setting")] \
        == [("comfy.url", "http://127.0.0.1:9000")]


def test_an_unknown_key_is_refused_by_get_and_put_and_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError, match="unknown studio setting 'nope'"):
        settings.get(conn, "nope")
    with pytest.raises(KeyError, match="unknown studio setting 'nope'"):
        settings.put(conn, "nope", "x")
    assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0
