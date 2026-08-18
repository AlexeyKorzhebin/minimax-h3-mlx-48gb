# Диалоговый редактор промпта — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Чат с локальной LLM в веб-морде, который разворачивает идею в промпт H3 по документированному формату и правит существующие промпты; плюс пресеты разрешений, дублирование задач и скрипты обвязки.

**Architecture:** Новый модуль `h3_48gb/provider.py` (абстракция LLM-провайдера: `llama-local` поднимает/гасит `llama-server`, `openai` ходит по внешнему URL; оба говорят по OpenAI-протоколу, ответ модели зажат JSON-схемой). `web.py` получает маршруты `/api/chat/*`, `/api/providers`, `/api/llm/unload`, `/api/jobs/<id>/duplicate`. Работник перед взятием задачи проверяет, жив ли порт llama, — «очередь главнее», но гашение только после подтверждения человеком через страницу. Модалка в `app.js`/`index.html`: окно промпта (с переиспользованным живым разбором) + лента диалога, адаптивная раскладка.

**Tech Stack:** stdlib-only Python (`http.server`, `urllib.request`), ванильный JS (ES-модуль, без сборки), тесты pytest + настоящий node через `_node_eval`.

**Spec:** `docs/superpowers/specs/2026-08-12-chat-prompt-editor-design.md` (прочитать перед работой).

## Global Constraints

- Рабочая копия: `/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/h3-web-worktree` (ветка `h3-web`). Все пути ниже — от её корня.
- Тесты: `.venv/bin/python -m pytest tests/<файл> -q` из корня worktree. Полные свиты перед финальным коммитом задачи: `tests/test_provider.py tests/test_chat_web.py tests/test_web.py tests/test_queue.py tests/test_worker.py`.
- **Ни одной новой зависимости.** Только stdlib и то, что уже в `pyproject.toml`.
- **Тест не написан, пока ты не видел его красным.** После зелёного — мутационная проверка: сломай строку, которую тест охраняет, увидь красный, верни. Вставь текст ошибки упавшего assert в отчёт задачи.
- Тексты UI — по-русски, в тоне существующих («Ничего не считается», «опрос не проходил»).
- Комментарии в коде — в стиле репозитория: объясняют ограничение, которое код не может показать сам; по-английски в Python/JS.
- Коммиты — по-русски, первая строка вида `feat: …` / `test: …` / `fix: …`, тело объясняет «почему». Подпись: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Секреты не попадают ни в git, ни в ответы API: `providers.json` и `.env` создаются тестами во временном `root`, реальные ключи в тестах не используются.
- llama-server в тестах НИКОГДА не запускается настоящий: только мок на `http.server` (образец в Task 2).

---

### Task 1: provider.py — конфиг провайдеров и .env

**Files:**
- Create: `h3_48gb/provider.py`
- Create: `tests/test_provider.py`

**Interfaces:**
- Produces: `load_env(root) -> dict[str, str]` — построчный разбор `<root>/.env` (`ИМЯ=значение`, `#` — комментарий, пустые строки пропускаются; файла нет → `{}`).
- Produces: `load_providers(root) -> dict` — читает `<root>/providers.json`, отдаёт `{"active": str, "providers": {имя: конфиг}}`; каждому конфигу добавляет вычисленные ключи `available: bool` и `reason: str|None` (для `type: "openai"` без переменной из `api_key_env` в `.env` — `available: False`, `reason: "нет токена <ИМЯ>"`). Файла нет → `{"active": None, "providers": {}}`.
- Produces: `ProviderError(Exception)` с полем `.code: str`.
- Produces: `PROMPT_SCHEMA: dict` — JSON-схема ответа модели (см. шаг 3), используется Task 3 и Task 5.

- [ ] **Step 1: Написать красные тесты**

```python
# tests/test_provider.py
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
```

- [ ] **Step 2: Убедиться, что тесты красные**

Run: `.venv/bin/python -m pytest tests/test_provider.py -q`
Expected: FAIL / ERROR — `No module named 'h3_48gb.provider'` (или AttributeError).

- [ ] **Step 3: Минимальная реализация**

```python
# h3_48gb/provider.py
"""LLM providers for the chat prompt editor.

Two kinds speak one protocol (OpenAI /v1/chat/completions): `llama-local`
also owns the llama-server process lifecycle, `openai` only needs a URL and
a token. Tokens never live in providers.json -- only the *name* of an .env
variable does, so the roster can be shown to the page verbatim.
"""
from __future__ import annotations

import json
from pathlib import Path


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# The model cannot answer outside this shape: llama.cpp enforces it with a
# grammar compiled from the schema, external providers via response_format.
PROMPT_SCHEMA = {
    "name": "h3_chat_turn",
    "schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "prompt": {
                "type": ["object", "null"],
                "properties": {
                    "instruction": {"type": ["string", "null"]},
                    "integrated_multimodal_description": {"type": "string"},
                    "overall_soundscape": {"type": "string"},
                    "non_diegetic_music": {"type": "string"},
                },
                "required": ["instruction", "integrated_multimodal_description",
                             "overall_soundscape", "non_diegetic_music"],
                "additionalProperties": False,
            },
        },
        "required": ["reply", "prompt"],
        "additionalProperties": False,
    },
}


def load_env(root) -> dict[str, str]:
    path = Path(root) / ".env"
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        env[name.strip()] = value.strip()
    return env


def load_providers(root) -> dict:
    path = Path(root) / "providers.json"
    if not path.is_file():
        return {"active": None, "providers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    env = load_env(root)
    providers = {}
    for name, cfg in data.get("providers", {}).items():
        cfg = dict(cfg)
        if cfg.get("type") == "openai":
            wanted = cfg.get("api_key_env", "")
            if wanted and wanted not in env:
                cfg["available"], cfg["reason"] = False, f"нет токена {wanted}"
            else:
                cfg["available"], cfg["reason"] = True, None
        else:
            cfg["available"], cfg["reason"] = True, None
        providers[name] = cfg
    return {"active": data.get("active"), "providers": providers}
```

- [ ] **Step 4: Зелёный прогон**

Run: `.venv/bin/python -m pytest tests/test_provider.py -q`
Expected: 4 passed.

- [ ] **Step 5: Мутационная проверка**

Сломай: в `load_providers` замени `cfg["available"], cfg["reason"] = False, f"нет токена {wanted}"` на `= True, None`. Прогони — `test_external_provider_without_its_token_is_visible_but_unavailable` обязан упасть. Верни. Затем сломай `env[name.strip()] = value.strip()` на `env[name] = value` — тест про `.env` НЕ упадёт (это не защищаемое поведение, обе формы валидны) — а вот удаление строки `if not line or line.startswith("#")…` уронит первый тест. Проверь именно её.

- [ ] **Step 6: Commit**

```bash
git add h3_48gb/provider.py tests/test_provider.py
git commit -m "feat: конфиг LLM-провайдеров — providers.json + токены только в .env"
```

---

### Task 2: provider.py — жизненный цикл llama-server

**Files:**
- Modify: `h3_48gb/provider.py`
- Modify: `tests/test_provider.py`

**Interfaces:**
- Consumes: `load_providers`, `ProviderError` из Task 1.
- Produces: `port_alive(port: int) -> bool` — TCP/HTTP-проба `GET http://127.0.0.1:<port>/health`, таймаут 2 с; используется и работником (Task 6).
- Produces: `class LlamaLocal` с конструктором `LlamaLocal(name: str, cfg: dict, root: Path)` и методами:
  - `status() -> str` — `"up"` (health 200) | `"down"`;
  - `ensure_up(timeout: float = 90.0, spawn=subprocess.Popen) -> None` — если down: запускает `llama_server` с `--models-dir <dirname presets_ini>`, `--models-preset <presets_ini>`, `--models-max 1`, `--host 127.0.0.1`, `--port <port>`, лог в `<root>/chat/llama.log`, ждёт health до `timeout`, иначе `ProviderError("llama_did_not_start", …)` с хвостом лога в сообщении;
  - `shutdown() -> None` — `pkill -f llama-server`-эквивалент через `subprocess.run(["pkill", "-f", "llama-server"])`, ждёт до 10 с пока health перестанет отвечать. Спокоен, если уже down.

- [ ] **Step 1: Мок llama-server и красные тесты**

Добавить в `tests/test_provider.py`:

```python
import http.server
import threading
import urllib.request


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
                type(self).seen.append({"path": self.path, "body": body})
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
```

- [ ] **Step 2: Красный прогон**

Run: `.venv/bin/python -m pytest tests/test_provider.py -q`
Expected: два новых теста падают (`AttributeError: … LlamaLocal`).

- [ ] **Step 3: Реализация**

```python
# в h3_48gb/provider.py
import subprocess
import time
import urllib.error
import urllib.request


def port_alive(port: int) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


class LlamaLocal:
    def __init__(self, name: str, cfg: dict, root):
        self.name, self.cfg, self.root = name, cfg, Path(root)

    def status(self) -> str:
        return "up" if port_alive(self.cfg.get("port", 0)) else "down"

    def ensure_up(self, timeout: float = 90.0, spawn=subprocess.Popen) -> None:
        if self.status() == "up":
            return
        log = self.root / "chat" / "llama.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        presets = str(Path(self.cfg["presets_ini"]).expanduser())
        cmd = [self.cfg["llama_server"],
               "--models-dir", str(Path(presets).parent),
               "--models-preset", presets,
               "--models-max", "1",
               "--host", "127.0.0.1", "--port", str(self.cfg["port"])]
        with open(log, "ab") as sink:
            spawn(cmd, stdout=sink, stderr=sink)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status() == "up":
                return
            time.sleep(0.2)
        tail = ""
        if log.is_file():
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-10:])
        raise ProviderError("llama_did_not_start",
                            f"llama-server не ответил /health за {timeout:.0f} с\n{tail}")

    def shutdown(self) -> None:
        subprocess.run(["pkill", "-f", "llama-server"], check=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.status() == "up":
            time.sleep(0.2)
```

- [ ] **Step 4: Зелёный прогон, затем мутация**

Run: `.venv/bin/python -m pytest tests/test_provider.py -q` → passed.
Мутация: убери `"--models-max", "1"` из `cmd` → `test_ensure_up_spawns_llama_with_the_preset_flags` красный. Верни.

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/provider.py tests/test_provider.py
git commit -m "feat: llama-local — подъём, health-check и гашение llama-server"
```

---

### Task 3: provider.py — ход чата по OpenAI-протоколу с JSON-схемой

**Files:**
- Modify: `h3_48gb/provider.py`
- Modify: `tests/test_provider.py`

**Interfaces:**
- Consumes: `_FakeLlama` из Task 2, `PROMPT_SCHEMA` из Task 1.
- Produces: `chat(cfg: dict, env: dict, messages: list[dict]) -> dict` — модульная функция: POST `<base>/v1/chat/completions` (для `llama-local` `base = http://127.0.0.1:<port>`, для `openai` — `cfg["base_url"]` + заголовок `Authorization: Bearer <env[api_key_env]>`), тело `{"model": cfg.get("model", cfg.get("preset")), "messages": …, "temperature": cfg.get("temperature", 0.7), "response_format": {"type": "json_schema", "json_schema": PROMPT_SCHEMA}}`. Разбирает `choices[0].message.content` как JSON. Невалидный JSON → **один** повтор с добавленным system-напоминанием схемы; второй провал → `ProviderError("bad_model_json", …)` с сырым текстом в сообщении.

- [ ] **Step 1: Красные тесты**

```python
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
        # заголовки мок не пишет в body — проверяем через отдельный маршрут ниже
    finally:
        fake.close()
```

Для проверки заголовка расширь `_FakeLlama.do_POST`: сохраняй `dict(self.headers)` в `seen` рядом с body (`{"path": …, "body": …, "headers": dict(self.headers)}`) и дополни последний тест `assert fake.requests[0]["headers"].get("Authorization") == "Bearer sk-t"`.

- [ ] **Step 2: Красный прогон** — `AttributeError: … chat`.

- [ ] **Step 3: Реализация**

```python
def _base_url(cfg: dict) -> str:
    if cfg.get("type") == "openai":
        return cfg["base_url"].rstrip("/")
    return f"http://127.0.0.1:{cfg['port']}"


def chat(cfg: dict, env: dict, messages: list[dict]) -> dict:
    body = {"model": cfg.get("model", cfg.get("preset", "default")),
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "response_format": {"type": "json_schema", "json_schema": PROMPT_SCHEMA}}
    headers = {"Content-Type": "application/json"}
    key_env = cfg.get("api_key_env")
    if cfg.get("type") == "openai" and key_env:
        headers["Authorization"] = f"Bearer {env.get(key_env, '')}"

    def ask(msgs):
        req = urllib.request.Request(_base_url(cfg) + "/v1/chat/completions",
                                     data=json.dumps({**body, "messages": msgs}).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = json.loads(r.read())
        return payload["choices"][0]["message"]["content"]

    raw = ask(messages)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        reminder = {"role": "system",
                    "content": "Ответ строго одним JSON-объектом по схеме "
                               "{reply: string, prompt: object|null}. Без другого текста."}
        raw2 = ask([reminder, *messages])
        try:
            return json.loads(raw2)
        except (json.JSONDecodeError, TypeError):
            raise ProviderError("bad_model_json", f"модель не удержала формат: {raw2[:400]}")
```

- [ ] **Step 4: Зелёный прогон + мутация** — убери повтор (`raw2 = ask(...)` → сразу raise): тест про retry красный (счётчик запросов 1, не 2). Верни.

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/provider.py tests/test_provider.py
git commit -m "feat: ход чата — OpenAI-протокол, схема ответа, один повтор на плохой JSON"
```

---

### Task 4: системный промпт-документация формата

**Files:**
- Create: `docs/h3-prompt-system.md`
- Modify: `h3_48gb/provider.py` (функция `system_prompt()`)
- Modify: `tests/test_provider.py`

**Interfaces:**
- Produces: `system_prompt() -> str` — читает `docs/h3-prompt-system.md` (путь от корня пакета: `Path(__file__).parent.parent / "docs" / "h3-prompt-system.md"`), кэширует в модульной переменной.

Содержимое `docs/h3-prompt-system.md` — документация по «командам» формата для модели, по-английски (модель пишет промпты по-английски), выжимка из `docs/upstream-guides/VIDEO_PROMPT_WRITING_GUIDE_base_en.md` и `skills/generating-h3-video/SKILL.md`. Обязательные разделы (написать полноценно, ~120–180 строк):

1. Роль: «You turn ideas into MiniMax H3 video prompts and edit existing ones through dialogue.»
2. Три поля и их порядок, правило первой строки для i2v/flf (дословные инструкции из гайда).
3. `[Shot N] At MM:SS.mmm` — правила склеек; стиль первыми словами Shot 1.
4. Таблица словаря камеры (12 типов + амплитуда + скорость).
5. Речь: `(S1)`/`(S2)` только говорящим, `<d>[Language] …</d>`, 11 языков, `<scenetrans>`, `<cutoff>`, voiceover с сомкнутыми губами.
6. Два звуковых поля: бюджеты предложений, речь не в soundscape, запрет слов о настроении в музыке.
7. Режим с кейфреймом: когда контекст говорит `mode: i2v`, поле `instruction` обязано быть дословной строкой `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`, а Shot 1 якорится на приложенную картинку (стиль, субъекты, композиция — с неё); для t2va `instruction` = null.
8. Правила поведения: **ответ всегда JSON по схеме**; `prompt: null` когда нечего править; при переформатировании чужого текста содержание сохраняется, разметка меняется; придумывать новое — только по прямой просьбе; уточняющие вопросы задавать в `reply`.

- [ ] **Step 1: Красный тест**

```python
def test_system_prompt_carries_the_format_and_the_preservation_rule():
    text = provider.system_prompt()
    for anchor in ("integrated_multimodal_description", "overall_soundscape",
                   "non_diegetic_music", "[Shot 1]", "<scenetrans>", "Arc Shot",
                   "preserve", "JSON"):
        assert anchor in text, anchor
```

- [ ] **Step 2: Красный прогон** → `AttributeError: system_prompt`.

- [ ] **Step 3: Написать `docs/h3-prompt-system.md`** (по структуре выше, сверяясь с гайдом — не по памяти) **и реализацию:**

```python
_SYSTEM_PROMPT_CACHE: str | None = None


def system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        path = Path(__file__).parent.parent / "docs" / "h3-prompt-system.md"
        _SYSTEM_PROMPT_CACHE = path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE
```

- [ ] **Step 4: Зелёный прогон. Мутация:** удали из файла раздел про сохранение содержания (слово `preserve`) → тест красный. Верни.

- [ ] **Step 5: Commit**

```bash
git add docs/h3-prompt-system.md h3_48gb/provider.py tests/test_provider.py
git commit -m "feat: системный промпт — документация формата H3 для чат-модели"
```

---

### Task 5: web.py — маршруты чата, провайдеров и выгрузки

**Files:**
- Modify: `h3_48gb/web.py` (роутинг: `_route_get` ~1080, `_route_post` ~1107; новые методы рядом с `_list_prompts` ~1299)
- Create: `tests/test_chat_web.py`

**Interfaces:**
- Consumes: `provider.load_providers`, `provider.LlamaLocal`, `provider.chat`, `provider.system_prompt`, `provider.ProviderError`, `provider.port_alive`; `queue.reconcile` (поле `.alive` — есть живая генерация), `queue.scan`.
- Produces (HTTP, все JSON; шаблоны ошибок — как в существующих маршрутах через `_error_bytes`):
  - `GET /api/providers` → `{"active": str|null, "providers": [{"name","type","available","reason"}]}` — секретов нет по построению (Task 1).
  - `GET /api/llm` → `{"status": "up"|"down", "provider": имя активного}`.
  - `POST /api/llm/unload` → гасит llama активного провайдера, `{"status": "down"}`.
  - `POST /api/chat` тело `{"source": {"kind": "new"|"prompt"|"job"|"clip", "name"?: str, "id"?: str}, "prompt": str, "mode"?: str, "image"?: str}` → создаёт `<outdir>/chat/<id>.json` (сессия хранит и `mode`/`image`; вид `clip` только валиден и сохраняется — поведение придёт со спекой «проекты»), отвечает `{"id": str}`. id — как `_suffix()` в queue.py: `secrets.token_hex(4)`.
  - `GET /api/chat/<id>` → вся сессия `{"id","source","messages","prompt"}`.
  - `POST /api/chat/<id>/message` тело `{"text": str, "prompt": str, "provider"?: str}`:
    - генерация идёт (`queue.reconcile(root).alive` непусто) и активный провайдер локальный → `409` `_error_bytes("gpu_busy", "идёт прогон — модель поднимется после него", {"running": …})`; сообщение НЕ сохраняется;
    - иначе: `ensure_up()` для локального (может занять минуту — это один синхронный запрос, страница ждёт), собрать `messages = [system] + история + новое`, где system = `system_prompt()` + `"\n\n## Context\nmode: <mode или t2va>"` + `"\n\n## Current prompt\n" + prompt из тела` — **текст из окна, не из истории**: правки руками не затираются;
    - **кадр к ходу:** если у сессии есть `image` и файл читается — новое user-сообщение уходит массивом частей `[{"type": "text", "text": …}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,<b64>"}}]` (llama-server с mmproj и внешние OpenAI-провайдеры понимают один и тот же формат); файл не читается → обычный текстовый ход плюс пометка в `reply`-плашке не нужна — в ленту страница допишет «кадр не прочитался: <путь>» из поля `warning` ответа маршрута. В историю сессии картинка не пишется — только текст (иначе файл сессии распухнет); при каждом ходе кадр прикладывается заново;
    - ответ модели сохранить в сессию (`messages` += user, assistant; `prompt` в файле сессии обновить, если модель вернула не-null) и отдать `{"reply": str, "prompt": dict|null, "llm": {"status": …}}`.
- Тесты используют `make_server`-хелперы из `tests/test_web.py` — **прочитай его шапку** (фикстуры `_serve`/клиент, если есть — переиспользуй импортом или скопируй минимум в свой файл, не переписывая test_web.py).

- [ ] **Step 1: Красные тесты** (образец; допиши симметричные)

```python
# tests/test_chat_web.py
"""Маршруты чата: сессии, ходы, провайдеры, правило «очередь главнее»."""
import json
from pathlib import Path

# ВАЖНО: посмотри, как tests/test_web.py поднимает сервер (fixture в его шапке),
# и заведи такой же _serve здесь. Ниже он считается существующим.


def test_chat_session_is_created_and_survives_a_reload(_serve):
    srv = _serve()
    created = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})
    sid = created["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["source"] == {"kind": "new"}
    assert got["messages"] == []
    assert (Path(srv.root) / "chat" / f"{sid}.json").is_file()


def test_a_message_during_a_live_generation_is_refused_and_not_saved(_serve, monkeypatch):
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    # живой прогон: reconcile().alive непусто — подменяем на уровне web-модуля
    monkeypatch.setattr("h3_48gb.web._generation_running", lambda root: {"job": "j-1"})
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                        {"text": "мрачнее", "prompt": "x"})
    assert status == 409
    assert payload["code"] == "gpu_busy"
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == []


def test_a_turn_reaches_the_model_with_the_editors_text_not_the_saved_one(_serve, fake_llama):
    """Правка руками: в окне текст A', в сессии сохранён A — модель обязана видеть A'."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "старый"})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "проверь", "prompt": "правленый руками"})
    sent = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "правленый руками" in sent and "старый" not in sent
```

`fake_llama` — фикстура над `_FakeLlama` из `tests/test_provider.py` (импортируй класс оттуда: `from tests.test_provider import _FakeLlama, _TURN` — если импорт между тест-файлами в этом репо не принят, вынеси `_FakeLlama` в `tests/_fake_llama.py` и импортируй в обоих). `_serve(providers_port=…)` пишет во временный root `providers.json` с llama-local на этом порту.

Дополнительный красный тест на зрячий i2v (туда же):

```python
def test_a_session_with_a_keyframe_attaches_it_to_the_turn_as_an_image_part(_serve, fake_llama, tmp_path):
    png = tmp_path / "start.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")     # содержимое неважно, важна передача
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    content = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    kinds = [part["type"] for part in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # история сессии хранит только текст
    saved = srv.get_json(f"/api/chat/{sid}")["messages"][-2]
    assert isinstance(saved["content"], str)
```

- [ ] **Step 2: Красный прогон** `.venv/bin/python -m pytest tests/test_chat_web.py -q` — 404 с `not_found` вместо ответов.

- [ ] **Step 3: Реализация в web.py**

В `_route_get` добавить (по образцу соседей):

```python
if path == "/api/providers":
    return self._providers()
if path == "/api/llm":
    return self._llm_status()
if path.startswith("/api/chat/"):
    return self._read_chat(path[len("/api/chat/"):])
```

В `_route_post`:

```python
if path == "/api/chat":
    return self._create_chat()
if path == "/api/llm/unload":
    return self._llm_unload()
if path.startswith("/api/chat/") and path.endswith("/message"):
    return self._chat_message(path[len("/api/chat/"):-len("/message")])
```

Методы (рядом с `_list_prompts`; `self.server.root` — outdir, как в остальных):

```python
def _generation_running(root):
    """Живой прогон = чья-то лиза дышит. Модульная функция, чтобы тесты могли подменить."""
    from . import queue as q
    return q.reconcile(root).alive


class _Handler(...):  # существующий
    def _providers(self):
        roster = provider.load_providers(self.server.root)
        listed = [{"name": n, "type": c.get("type"), "available": c.get("available"),
                   "reason": c.get("reason")} for n, c in roster["providers"].items()]
        return 200, "application/json", _json_bytes({"active": roster["active"],
                                                     "providers": listed})

    def _active_llama(self):
        roster = provider.load_providers(self.server.root)
        name = roster["active"]
        cfg = roster["providers"].get(name, {})
        if cfg.get("type") != "llama-local":
            return None
        return provider.LlamaLocal(name, cfg, self.server.root)

    def _llm_status(self):
        lam = self._active_llama()
        status = lam.status() if lam else "down"
        return 200, "application/json", _json_bytes({"status": status})

    def _llm_unload(self):
        lam = self._active_llama()
        if lam:
            lam.shutdown()
        return 200, "application/json", _json_bytes({"status": "down"})

    def _chat_dir(self) -> Path:
        d = Path(self.server.root) / "chat"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _create_chat(self):
        body = self._json_request(allowed=("source", "prompt"))
        session = {"id": secrets.token_hex(4), "source": body.get("source", {"kind": "new"}),
                   "messages": [], "prompt": body.get("prompt", "")}
        path = self._chat_dir() / f"{session['id']}.json"
        queue.write_json_durably(path, session)
        return 200, "application/json", _json_bytes({"id": session["id"]})

    def _read_chat(self, sid: str):
        path = self._chat_dir() / f"{sid}.json"
        if not path.is_file():
            return 404, "application/json", _error_bytes("chat_not_found", f"нет сессии {sid}")
        return 200, "application/json", path.read_bytes()

    def _chat_message(self, sid: str):
        path = self._chat_dir() / f"{sid}.json"
        if not path.is_file():
            return 404, "application/json", _error_bytes("chat_not_found", f"нет сессии {sid}")
        body = self._json_request(allowed=("text", "prompt", "provider"))
        roster = provider.load_providers(self.server.root)
        name = body.get("provider") or roster["active"]
        cfg = roster["providers"].get(name)
        if not cfg or not cfg.get("available"):
            return 409, "application/json", _error_bytes(
                "provider_unavailable", (cfg or {}).get("reason") or f"нет провайдера {name}")
        if cfg.get("type") == "llama-local" and _generation_running(self.server.root):
            return 409, "application/json", _error_bytes(
                "gpu_busy", "идёт прогон — модель поднимется после него")
        if cfg.get("type") == "llama-local":
            provider.LlamaLocal(name, cfg, self.server.root).ensure_up()
        session = json.loads(path.read_text(encoding="utf-8"))
        system = (provider.system_prompt() + "\n\n## Current prompt\n" + body.get("prompt", ""))
        messages = ([{"role": "system", "content": system}]
                    + [{"role": m["role"], "content": m["content"]}
                       for m in session["messages"]]
                    + [{"role": "user", "content": body["text"]}])
        env = provider.load_env(self.server.root)
        try:
            turn = provider.chat(cfg, env, messages)
        except provider.ProviderError as err:
            return 502, "application/json", _error_bytes(err.code, str(err))
        session["messages"] += [{"role": "user", "content": body["text"]},
                                {"role": "assistant", "content": turn["reply"]}]
        if turn.get("prompt"):
            session["prompt_struct"] = turn["prompt"]
        queue.write_json_durably(path, session)
        return 200, "application/json", _json_bytes(
            {"reply": turn["reply"], "prompt": turn.get("prompt")})
```

(`import secrets` и `from . import provider` — в шапку web.py; `_generation_running` — модульная функция для monkeypatch. Существующий `_json_request` проверяет аллоулист полей тела — новые маршруты обязаны следовать этому же паттерну; если он принимает обязательные/опциональные поля, посмотри его сигнатуру на месте и передай как соседние.)

- [ ] **Step 4: Зелёный прогон + мутация:** в `_chat_message` замени `body.get("prompt", "")` в system на `session["prompt"]` → тест про правку руками красный. Верни. Прогони также `tests/test_web.py` целиком: старые маршруты не должны шелохнуться.

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/web.py tests/test_chat_web.py tests/_fake_llama.py h3_48gb/provider.py tests/test_provider.py
git commit -m "feat: маршруты чата — сессии, ходы, провайдеры, gpu_busy при живом прогоне"
```

---

### Task 6: работник — «очередь главнее», но с подтверждением

**Files:**
- Modify: `h3_48gb/worker.py:268-315` (`main_loop`)
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `provider.load_providers`, `provider.port_alive`.
- Produces: `_llm_holds_gpu(root) -> bool` — модульная функция worker.py: активный провайдер локален и его порт отвечает. В `main_loop` — после `reconcile`, перед `claim`: если `_llm_holds_gpu(root)` — не брать задачу, спать `poll` (страница в это время показывает подтверждение; POST `/api/llm/unload` гасит сервер — на следующем витке порт мёртв и работник берёт задачу). Гасит llama только человек кнопкой — работник никогда сам.

- [ ] **Step 1: Красный тест** (по образцу существующих в test_worker.py — посмотри, как они собирают `root` и фейковый `spawn`; используй их хелперы)

```python
def test_worker_leaves_the_queue_alone_while_the_llm_holds_the_gpu(tmp_path, monkeypatch):
    """Модель в памяти -- работник не берёт задачу, пока человек не подтвердит выгрузку.

    Подтверждение приходит извне (страница гасит llama через /api/llm/unload);
    для работника это выглядит как порт, переставший отвечать.
    """
    root = tmp_path
    _submit_stub_job(root)                     # хелпер из этого файла, если есть; иначе q.submit(...)
    holds = {"value": True}
    monkeypatch.setattr("h3_48gb.worker._llm_holds_gpu", lambda r: holds["value"])
    stop = threading.Event()
    ran = []

    def spawn(cmd, **kw):
        ran.append(cmd)
        holds["never"] = True
        class P:
            pid = 1
            def wait(self): return 0
        return P()

    t = threading.Thread(target=worker.main_loop, args=(root,),
                         kwargs={"poll": 0.05, "stop": stop, "spawn": spawn}, daemon=True)
    t.start()
    time.sleep(0.3)
    assert ran == [], "задача взята при живой модели"
    holds["value"] = False                     # «подтвердили выгрузку»
    time.sleep(0.3)
    stop.set(); t.join(timeout=5)
    assert ran, "после выгрузки задача так и не взята"
```

- [ ] **Step 2: Красный прогон** — первый assert падает (работник берёт задачу сразу).

- [ ] **Step 3: Реализация**

```python
# worker.py
from . import provider


def _llm_holds_gpu(root) -> bool:
    """A resident local LLM and a 27 GB generation cannot share 48 GB. The page
    owns the *decision* (it asks the human and calls /api/llm/unload); the worker
    only ever observes the port."""
    roster = provider.load_providers(root)
    cfg = roster["providers"].get(roster["active"] or "", {})
    return cfg.get("type") == "llama-local" and provider.port_alive(cfg.get("port", 0))
```

В цикле `main_loop`, после `if state.alive: …continue`:

```python
            if _llm_holds_gpu(root):
                if stop.wait(poll):
                    break
                continue
```

- [ ] **Step 4: Зелёный + вся свита test_worker.py + мутация:** закомментируй новый блок в цикле → тест красный. Верни.

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/worker.py tests/test_worker.py
git commit -m "feat: работник не трогает очередь, пока LLM держит GPU"
```

---

### Task 7: дублирование задачи и пресеты разрешений (сервер + форма)

**Files:**
- Modify: `h3_48gb/web.py` (`_route_post` + метод `_duplicate_job` рядом с `_promote_job` ~1264)
- Modify: `h3_48gb/webui/index.html` (кнопки пресетов у канваса ~строка 95; действие «копия» в шаблоне задач)
- Modify: `h3_48gb/webui/app.js` (обработчики)
- Modify: `tests/test_chat_web.py`, `tests/test_web.py` (JS-тест пресетов)

**Interfaces:**
- Produces: `POST /api/jobs/<id>/duplicate` → находит задачу в pending ИЛИ finished (см. `queue.job_path`/`scan`), создаёт новую pending через существующий `queue.submit` с теми же `args`/`note` (проверь сигнатуру `submit(root, args, note, dry_run_report, estimate, …)` на месте — возьми отчёт/оценку из файла исходной задачи), отвечает `{"id": новый}`.
- Produces (JS): `export const CANVAS_PRESETS = [{key: "draft", label: "черновик", w: 448, h: 288}, {key: "small", label: "малое", w: 896, h: 576}, {key: "large", label: "большое", w: 1344, h: 768}]` и `applyCanvasPreset(key)` — заполняет `#width`/`#height` и дёргает пересчёт оценки (найди, как форма пересчитывает estimate при ручном вводе — там есть обработчик input; вызови его же путём).

- [ ] **Step 1: Красные тесты** — python: дубликат ждущей и законченной задачи появляется в pending с теми же args; 404 на неизвестный id. JS (в test_web.py, по образцу `_node_eval`-тестов): `applyCanvasPreset("draft")` выставляет 448/288; пресеты содержат ровно три пункта с этими канвасами.

- [ ] **Step 2: Красный прогон.**

- [ ] **Step 3: Реализация** — сервер: `_duplicate_job(raw_id)`; JS: константа + функция + три кнопки `<button class="ghost preset" data-preset="draft">черновик 448×288</button>` и т.п. в ряд `#row` канваса; делегированный обработчик клика; кнопка «копия» в разметке задачи (рядом с существующими top/edit/del) шлёт POST duplicate и перечитывает состояние.

- [ ] **Step 4: Зелёный + мутации** (дубликат из finished: сломай ветку поиска в finished → тест красный; пресет: поменяй 448 на 447 → JS-тест красный).

- [ ] **Step 5: Commit**

```bash
git add h3_48gb/web.py h3_48gb/webui/index.html h3_48gb/webui/app.js tests/test_chat_web.py tests/test_web.py
git commit -m "feat: дублирование задачи и пресеты канваса черновик/малое/большое"
```

---

### Task 8: модалка чата — разметка, раскладка, логика

**Files:**
- Modify: `h3_48gb/webui/index.html` (модалка перед `</body>`; кнопки входа: «Новый через диалог» в форме, «обсудить» у промпта в библиотеке и у ждущей задачи)
- Modify: `h3_48gb/webui/style.css` (раскладка: `@media (min-width: 1100px)` — промпт и диалог рядом, иначе колонкой; промпт-панель всегда видима)
- Modify: `h3_48gb/webui/app.js`
- Modify: `tests/test_web.py` (JS-тесты чистой логики), `tests/test_chat_web.py` (сквозной ход через мок)

**Interfaces:**
- Consumes: `analysePrompt`, `highlightHtml`, `scaleHtml` (существующие), маршруты Task 5, `_TURN`-мок.
- Produces (JS, экспортируются для node-тестов):
  - `export function buildPromptText(p)` — из объекта схемы в текст: `instruction` (если не null) + пустая строка + три поля с заголовками `имя: значение`, между полями пустая строка, в конце `\n`;
  - `export function applyTurn(state, turn)` — чистая функция состояния модалки: `turn.prompt === null` → только лента; иначе — лента + `state.promptText = buildPromptText(turn.prompt)`;
  - `openChatModal(source)` / `finishChat()` — DOM-обвязка: три источника (плюс передача `mode` из `#mode` и пути кадра из `#image`, когда модалка открыта из формы в режиме i2v; у задачи-источника — из её args), кнопка завершения по `source.kind` («в Редактор» → текст в `#prompt` формы; «сохранить промпт» → PUT `/api/prompts/<имя>`; «обновить задачу» → PUT `/api/jobs/<id>`), закрытие модалки;
  - статусная плашка модели: `down` → «модель не поднята — поднимется при первом сообщении», ход с 409 `gpu_busy` → «идёт прогон — модель поднимется после него (~N мин)», где N — остаток из прогресса работника, который страница уже держит для верхней приборной строки (`#rail-run`/`#rail-steps` — найди источник этих данных в `app.js` и переиспользуй); ошибка 502 → текст ошибки в ленте, сообщение остаётся в поле ввода.

- [ ] **Step 1: Красные JS-тесты** (test_web.py, `_node_eval`):

```python
def test_a_model_turn_with_a_prompt_replaces_the_editor_text():
    out = _node_eval("""
      const turn = {reply: "ок", prompt: {instruction: null,
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"}};
      const state = {promptText: "старый", log: []};
      app.applyTurn(state, turn);
      console.log(JSON.stringify([state.promptText, state.log.length]));
    """)
    text, entries = out
    assert text.startswith("integrated_multimodal_description: [Shot 1] X.")
    assert "overall_soundscape: Wind." in text and text.endswith("non_diegetic_music: N/A\n")
    assert entries == 1


def test_a_null_prompt_turn_leaves_the_editor_alone():
    out = _node_eval("""
      const state = {promptText: "нетронутый", log: []};
      app.applyTurn(state, {reply: "а какой свет?", prompt: null});
      console.log(JSON.stringify(state.promptText));
    """)
    assert out == "нетронутый"


def test_an_i2v_prompt_puts_the_instruction_first_with_a_blank_line():
    out = _node_eval("""
      console.log(JSON.stringify(app.buildPromptText({
        instruction: "For the target video, at 0.00 seconds …",
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"})));
    """)
    assert out.startswith("For the target video") and "\n\nintegrated_multimodal_description:" in out
```

- [ ] **Step 2: Красный прогон.**

- [ ] **Step 3: Реализация** — JS-функции + разметка модалки:

```html
<div class="modal-back" id="chat-modal" hidden>
  <div class="modal chat">
    <div class="chat-prompt">
      <div class="editor"><pre class="hl" id="chat-hl" aria-hidden="true"></pre>
        <textarea id="chat-prompt-text" spellcheck="false" wrap="soft"></textarea></div>
      <div class="scale" id="chat-scale"></div>
      <ul class="parse" id="chat-parse"></ul>
    </div>
    <div class="chat-talk">
      <div class="chat-head">
        <select id="chat-provider" class="pick"></select>
        <span id="chat-llm" class="hint">модель не поднята</span>
        <button class="ghost" id="chat-finish" type="button">в Редактор</button>
        <button class="ghost" id="chat-close" type="button">закрыть</button>
      </div>
      <ol class="chat-log" id="chat-log"></ol>
      <form id="chat-form"><textarea id="chat-input" rows="2"
        placeholder="Опиши идею или попроси переделать…"></textarea>
        <button id="chat-send" type="submit">отправить</button></form>
    </div>
  </div>
</div>
```

Разбор/подсветка/таймлайн в модалке — те же вызовы, что у формы (`analysePrompt`/`highlightHtml`/`scaleHtml` на элементы `chat-*`), обработчик `input` на `#chat-prompt-text` (правка руками живёт в `state.promptText`). CSS: `.modal.chat {display: grid; grid-template-columns: 1fr;} @media (min-width: 1100px) {.modal.chat {grid-template-columns: minmax(0,1fr) minmax(0,1fr);}}`, фон `.modal-back {position: fixed; inset: 0; background: rgba(0,0,0,.55); overflow: auto;}`.

- [ ] **Step 4: Сквозной python-тест** (test_chat_web.py): создать сессию от источника `{"kind":"prompt","name":"x.txt"}`, ход через fake_llama с `_TURN`, проверить что ответ маршрута отдал prompt и сессия на диске обновилась. Зелёный прогон всех свит.

- [ ] **Step 5: Мутация** — в `applyTurn` убери присваивание `state.promptText` → первый JS-тест красный. Верни.

- [ ] **Step 6: Commit**

```bash
git add h3_48gb/webui/index.html h3_48gb/webui/style.css h3_48gb/webui/app.js tests/test_web.py tests/test_chat_web.py
git commit -m "feat: модалка диалогового редактора — промпт сбоку, адаптивная раскладка"
```

---

### Task 9: подтверждение выгрузки на странице

**Files:**
- Modify: `h3_48gb/webui/app.js`, `h3_48gb/webui/index.html`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `GET /api/llm`, `POST /api/llm/unload`, существующий опрос `/api/state` (раз в 20 с, `POLL_MS`).
- Produces (JS): `export function unloadBanner(state)` — чистая функция: `{pending > 0, llm: "up"}` → `{show: true, text: "Модель в памяти держит GPU — выгрузить и начать генерацию?"}`; иначе `{show: false}`. Плашка с кнопками «выгрузить и начать» (POST unload) и «пусть ждёт» (скрыть до следующего изменения состояния) в секции очереди.

- [ ] **Step 1: Красный JS-тест:** `unloadBanner({pending: 2, llm: "up"}).show === true`; `unloadBanner({pending: 0, llm: "up"}).show === false`; `unloadBanner({pending: 2, llm: "down"}).show === false`.
- [ ] **Step 2: Красный прогон.**
- [ ] **Step 3: Реализация** — функция + плашка + опрос `/api/llm` вместе с `/api/state`.
- [ ] **Step 4: Зелёный + мутация** (условие `llm === "up"` → `true` → третий случай красный).
- [ ] **Step 5: Commit**

```bash
git add h3_48gb/webui/app.js h3_48gb/webui/index.html tests/test_web.py
git commit -m "feat: страница спрашивает разрешение выгрузить LLM перед генерацией"
```

---

### Task 10: скрипты старта и останова веб-морды

**Files:**
- Create: `scripts/web-start.sh`, `scripts/web-stop.sh`
- Modify: `tests/test_cli.py` (синтаксис-проверка)

**Interfaces:**
- Produces: `scripts/web-start.sh [outdir]` — outdir по умолчанию `~/Research/TestVideo`; идемпотентен (живой `h3 web`/`h3 worker` не дублируется — проверка `pgrep -f`); сервер и работник — `nohup caffeinate -dimsu … &`, логи `<outdir>/_логи/h3-web.log`, `h3-worker.log`; запускает через `.venv/bin/python -m h3_48gb.cli`.
- Produces: `scripts/web-stop.sh` — SIGTERM работнику (его `_stop_signals` дожидается конца текущей задачи выбором, а не убийством — прочти docstring `main_loop` перед написанием), гасит llama-server (`pkill -f llama-server`), останавливает `h3 web`.

- [ ] **Step 1: Красный тест** (в test_cli.py, рядом с похожими):

```python
@pytest.mark.parametrize("script", ["scripts/web-start.sh", "scripts/web-stop.sh"])
def test_web_scripts_exist_are_executable_and_parse(script):
    path = Path(__file__).parent.parent / script
    assert path.is_file(), script
    assert path.stat().st_mode & 0o111, "не исполняемый"
    subprocess.run(["bash", "-n", str(path)], check=True)
```

- [ ] **Step 2: Красный прогон.** — файла нет.
- [ ] **Step 3: Написать скрипты** (образец стиля — `~/models/restart-llama.sh` и `~/Research/TestVideo/_очередь/queue28-ballad.sh`: `set -u`, комментарий-шапка, `pgrep`-идемпотентность).
- [ ] **Step 4: Зелёный. Живая проверка руками:** `bash scripts/web-start.sh /tmp/h3-web-smoke && curl -s 127.0.0.1:8765/api/state && bash scripts/web-stop.sh` — вставь вывод в отчёт.
- [ ] **Step 5: Commit**

```bash
git add scripts/web-start.sh scripts/web-stop.sh tests/test_cli.py
git commit -m "feat: скрипты старта и останова веб-морды с работником"
```

---

### Task 11: сквозная проверка и провайдерный конфиг по месту

**Files:**
- Create: `~/Research/TestVideo/providers.json` (вне git — боевой конфиг с qwen-local и gemma-local из `~/models/presets.ini`, порт 8080)
- Modify: `docs/superpowers/specs/2026-08-12-chat-prompt-editor-design.md` — приписка «реализовано, коммиты …»

- [ ] **Step 1: Все свиты зелёные:** `.venv/bin/python -m pytest tests/ -q` — вставь итоговую строку в отчёт.
- [ ] **Step 2: Живой smoke без модели:** поднять `h3 web` на временном outdir, открыть чат, отправить сообщение при неподнятой llama на нерабочем пути `llama_server: /usr/bin/false` → в ленте плашка об ошибке подъёма, сообщение осталось в поле. (Руками через curl: создать сессию, POST message, проверить 502 с `llama_did_not_start`.)
- [ ] **Step 3: Боевой конфиг** `providers.json` в `~/Research/TestVideo/` (qwen-local активный; gemma-local второй; `.env` не создавать — внешних токенов пока нет).
- [ ] **Step 4: Финальный коммит хвостов, если остались.**
