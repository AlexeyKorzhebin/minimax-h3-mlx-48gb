"""Провайдеры LLM: конфиг, .env, жизненный цикл llama-server, ход чата.

llama-server здесь всегда фальшивый: настоящий грузит 30 ГБ. Мок — обычный
http.server в потоке, отвечающий на /health и /v1/chat/completions.
"""
import json
import textwrap

import pytest

from h3_48gb import provider


def _write(root, name, text):
    path = root / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_env_file_is_parsed_line_by_line_and_absence_is_empty(tmp_path):
    assert provider.load_env(tmp_path) == {}
    _write(tmp_path, ".env", """\
        # комментарий
        OPENROUTER_API_KEY=sk-or-abc

        EMPTY_TAIL=
    """)
    env = provider.load_env(tmp_path)
    assert env["OPENROUTER_API_KEY"] == "sk-or-abc"
    assert env["EMPTY_TAIL"] == ""
    assert "# комментарий" not in env


def test_missing_providers_file_is_an_empty_roster(tmp_path):
    assert provider.load_providers(tmp_path) == {"active": None, "providers": {}}


def test_external_provider_without_its_token_is_visible_but_unavailable(tmp_path):
    (tmp_path / "providers.json").write_text(json.dumps({
        "active": "qwen-local",
        "providers": {
            "qwen-local": {"type": "llama-local", "llama_server": "/opt/homebrew/bin/llama-server",
                           "presets_ini": "~/models/presets.ini", "preset": "qwen", "port": 18080,
                           "ctx": 49152, "resident_gb": 31},
            "openrouter": {"type": "openai", "base_url": "https://example.invalid/v1",
                           "model": "m", "api_key_env": "OPENROUTER_API_KEY"},
        },
    }), encoding="utf-8")
    roster = provider.load_providers(tmp_path)
    assert roster["active"] == "qwen-local"
    assert roster["providers"]["qwen-local"]["available"] is True
    ext = roster["providers"]["openrouter"]
    assert ext["available"] is False
    assert "OPENROUTER_API_KEY" in ext["reason"]
    # ключ появился — провайдер ожил
    _write(tmp_path, ".env", "OPENROUTER_API_KEY=sk-x\n")
    assert provider.load_providers(tmp_path)["providers"]["openrouter"]["available"] is True


def test_the_loaded_roster_never_carries_the_secret_itself(tmp_path):
    _write(tmp_path, ".env", "OPENROUTER_API_KEY=sk-very-secret\n")
    (tmp_path / "providers.json").write_text(json.dumps({
        "active": "openrouter",
        "providers": {"openrouter": {"type": "openai", "base_url": "https://example.invalid/v1",
                                     "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
    }), encoding="utf-8")
    assert "sk-very-secret" not in json.dumps(provider.load_providers(tmp_path))
