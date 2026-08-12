"""Провайдеры LLM: конфиг, .env, жизненный цикл llama-server, ход чата.

llama-server здесь всегда фальшивый: настоящий грузит 30 ГБ. Мок — обычный
http.server в потоке, отвечающий на /health и /v1/chat/completions.
"""
import http.server
import json
import textwrap
import threading
import urllib.request

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


class _FakeLlama:
    """llama-server, который ничего не грузит: /health 200 и захардкоженный чат-ответ.

    Поток обрывается close(); порт выдаёт ядро (port=0), чтобы тесты не дрались.
    """

    def __init__(self, chat_payload=None, health: int = 200):
        handler_cls = self._make_handler(chat_payload, health)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.requests: list[dict] = []
        handler_cls.seen = self.requests
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self, chat_payload, health):
        class Handler(http.server.BaseHTTPRequestHandler):
            seen: list = []

            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                code = health if self.path == "/health" else 404
                self.send_response(code); self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                type(self).seen.append({"path": self.path, "body": body,
                                         "headers": dict(self.headers)})
                out = json.dumps(chat_payload or {}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        return Handler

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def _llama_cfg(port: int) -> dict:
    return {"type": "llama-local", "llama_server": "/usr/bin/true",
            "presets_ini": "/tmp/presets.ini", "preset": "qwen", "port": port,
            "ctx": 4096, "resident_gb": 31}


def test_status_reflects_health_endpoint(tmp_path):
    fake = _FakeLlama()
    try:
        assert provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path).status() == "up"
    finally:
        fake.close()
    assert provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path).status() == "down"


def test_ensure_up_spawns_llama_with_the_preset_flags(tmp_path):
    """Не поднимаем настоящего: spawn подменён, health отвечает мок, а тест
    проверяет ровно командную строку — то, что сломается молча."""
    fake = _FakeLlama()
    spawned: list[list[str]] = []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        class P: pid = 1
        return P()

    try:
        lam = provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path)
        lam.ensure_up(timeout=5, spawn=spawn)   # health уже 200 -> spawn не нужен
        assert spawned == []
    finally:
        fake.close()

    lam = provider.LlamaLocal("q", _llama_cfg(0), tmp_path)  # порт 0 всегда down
    with pytest.raises(provider.ProviderError) as err:
        lam.ensure_up(timeout=0.3, spawn=spawn)
    assert err.value.code == "llama_did_not_start"
    (cmd,) = spawned
    assert cmd[0] == "/usr/bin/true"
    assert "--models-preset" in cmd and "/tmp/presets.ini" in cmd
    assert "--models-max" in cmd and "1" in cmd[cmd.index("--models-max") + 1]


_TURN = {"choices": [{"message": {"content": json.dumps({
    "reply": "Сделал мрачнее.",
    "prompt": {"instruction": None,
               "integrated_multimodal_description": "[Shot 1] Live-action…",
               "overall_soundscape": "Wind.",
               "non_diegetic_music": "N/A"}})}}]}


def test_chat_sends_schema_and_returns_parsed_turn(tmp_path):
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "мрачнее"}])
    finally:
        fake.close()
    assert turn["reply"] == "Сделал мрачнее."
    assert turn["prompt"]["non_diegetic_music"] == "N/A"
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["body"]["response_format"]["json_schema"] == provider.PROMPT_SCHEMA


def test_invalid_model_json_gets_one_retry_then_a_named_error(tmp_path):
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "не json"}}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "должен быть ровно один повтор"
    finally:
        fake.close()


def test_external_provider_authorises_with_its_env_token(tmp_path):
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {"type": "openai", "base_url": f"http://127.0.0.1:{fake.port}",
           "model": "m", "api_key_env": "K"}
    try:
        provider.chat(cfg, {"K": "sk-t"}, [{"role": "user", "content": "x"}])
        assert fake.requests[0]["headers"].get("Authorization") == "Bearer sk-t"
    finally:
        fake.close()
