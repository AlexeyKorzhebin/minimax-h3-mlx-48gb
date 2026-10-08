# Панель h3, волна 1.6: выбор модели LLM — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** владелец выбирает модель внутри провайдера (CAILA и любой OpenAI-совместимый) в чате и в «Сюжете» проекта из короткого проверенного списка; ключ лимита и `temperature` подбираются по семейству модели; панель помнит, кто ответил.

**Architecture:** сервер — `h3_48gb/provider.py` (чистые функции семейства, список моделей с кэшем, `with_model`), `h3_48gb/web.py` (маршрут списка, поля ростера, `model` в двух ходах, запоминание), `h3_48gb/project.py` (поле `scenario_llm`). UI — экспортируемые чистые функции `app.js` + тонкая проводка, как в волне 1.5.

**Tech Stack:** Python 3.12 stdlib, node (через pytest), фейковый HTTP-сервер `tests/_fake_llama.py`.

**Spec:** `docs/superpowers/specs/2026-10-08-panel-model-picker-design.md` (читать целиком до любой задачи). Источник: `docs/panel/2026-10-08-llm-providers.md` §4.

**Две полосы:**
- **СЕРВЕР** — задачи 1–6, worktree `~/worktrees/panel-ui-gaps`, идут **сейчас**, параллельно UI-полосе волны 1.5. Строго последовательно (все правят `provider.py`/`web.py`).
- **UI** — задачи 7–8, worktree `~/worktrees/panel-ui-gaps-ui`, **только после коммита задачи 12 волны 1.5** (она правит те же `#chat-provider`/`#scenario-provider`) и после влития серверных задач 1–4 в ветку UI-полосы.

**Запуск — только при чистом worktree.** Перед каждой задачей `git status` и `git log -3`. Чужие незакоммиченные изменения в файле задачи — стоп, сообщить координатору. `git add -A` запрещён. **Исполнитель не коммитит** — коммитит координатор; шаг «коммит» означает: подготовить сообщение и список файлов в отчёте.

**Номера строк** сверены 08.10 на `cdedcee6` (сервер) и `c6b0462b` (UI). Ищите функцию по имени: задачи сдвигают строки друг другу.

## Global Constraints

- Окружение: `PY=~/venvs/h3-panel/bin/python`. Полный прогон перед сдачей задачи: `env -u NODE_OPTIONS $PY -m pytest -q -p no:cacheprovider` в форграунде, timeout 600000, 0 failed. Точечно: `... tests/test_model_picker.py::<тест>`.
- **Тест не написан, пока не видел его красным** (`CLAUDE.md`): шаг «красный» до кода; шаг мутации после — удалить/обратить защищаемую строку, увидеть красный, вернуть. Текст assertion-ошибки мутации — в отчёт. Без этого задача не сдана.
- Тесты пиннят содержимое: тела ответов и запросов — `==` целиком (допустима проекция тела запроса без `messages`/`response_format` — хелпер `_sent`, он сравнивается `==`). Не `in`, не `len()`, не «ключ есть».
- **Сеть в тестах — только `_FakeLlama`** (`tests/_fake_llama.py`) на 127.0.0.1 или закрытый порт. Ни CAILA, ни других внешних хостов. Ключей не читать, `.env` сервера не трогать.
- **JS-моки — только точное сравнение:** `closest(sel)`/`match(sel)` сравнивают `sel === "…"`, маршруты харнесса — точный ключ. Никаких `.includes()`/`startsWith` в моках (память `js-driver-mocks-need-exact-match`).
- Сообщения человеку — по-русски; комментарии, докстринги, идентификаторы — по-английски.
- Новых кодов ошибок нет: отказы — `args_invalid` (400, уже в `cli.ERROR_CODES`).
- Тело ответа хода чата (`web.py:6457-6460`) **не меняется** — на нём пиннятся десятки тестов.
- Кэш моделей — модульное состояние: каждый тест файла `tests/test_model_picker.py` чистит его автофикстурой.
- Сервер alex-neuro, GPU, sglang, Qwen не трогаются. Живая проба задачи 6 — шаг координатора, не исполнителя.

## Review Focus

1. **Явные ключи записи против выбранной модели.** Запись `max_tokens_param: "max_tokens"`/`send_temperature: true`, ход на `gpt-5` → ключи записи снимаются, уходит `max_completion_tokens` без `temperature`. И обратно: запись под `gpt-5` с `max_completion_tokens`, ход на Claude → `max_tokens`. Тест `test_chat_model_override_uses_the_family_wire_not_the_entry_keys` (задача 4).
2. **`gpt-5.1` — reasoning.** Регэксп ai-writer его пропускает. Строка в `WIRE_CASES` (задача 1).
3. **Отказ по `model` без побочных эффектов:** ни запроса к провайдеру, ни записи сессии/проекта. Тесты `test_chat_refuses_a_bad_model_before_the_network` и `test_scenario_refuses_a_model_outside_the_allow_list` (задача 4).
4. **Неудача каталога не кэшируется**, `refresh` идёт в сеть даже при живом кэше. Тесты задачи 2.
5. **Нет токена / выбор выключен — в сеть не ходим.** Тесты задачи 3 (`fake.requests == []`).
6. **Дубли id в каталоге — первый выигрывает**, иначе флаг `chat` может перевернуться. `CATALOG` задачи 2.
7. **Процедурная генерация сюжета стирает `scenario_llm`** — иначе подпись врёт «сюжет написал caila». Тест задачи 4.
8. **Проба ключа на шлюзе, проверяющем тело раньше ключа, ложно-зелёная** — шаг 0 задачи 6 с фальшивым ключом обязателен; без него задача 6 не начинается.
9. **UI: список моделей не грузится из функции разметки** — `renderProjectModal` перерисовывает панель на каждую правку; загрузка из разметки = запрос на каждую галочку. Сценарий `scenario_model_list_fetched_once` (задача 8).
10. **UI: `model` в теле только при `pick_model`** — старые тесты тел запросов не трогаются. Сценарий `chat_no_pick_model_sends_no_model` (задача 7).

---

## Карта файлов

| Файл | Ответственность | Задачи |
|---|---|---|
| `h3_48gb/provider.py` | `wire_defaults`, `_chat_turn` по семейству, `_fetch_models`, `list_models`+кэш, `allowed_prefixes`, `model_allowed`, `with_model`, `_content_text`, проба ключа в `test_provider` | 1, 2, 4, 5, 6 |
| `h3_48gb/web.py` | `GET /api/providers/<n>/models`, `model`/`pick_model` в ростере, `model` в ходе чата и в `/scenario/generate`, `session.provider/model`, `scenario_llm` | 3, 4 |
| `h3_48gb/project.py` | `scenario_llm` в `_SCENARIO_FIELDS` | 4 |
| `docs/LLM-PROVIDERS.md` | `pick_model`, `models_allow`, семейства, итог живой пробы ключа | 3, 6 |
| `tests/test_model_picker.py` (новый) | серверные тесты волны | 1–6 |
| `tests/test_web_projects.py` | правка `test_provider_test_route_never_leaks_the_token` под два запроса | 6 |
| `h3_48gb/webui/{app.js,index.html,style.css}` | чистые функции выбора модели, проводка в чате и «Сюжете» | 7, 8 |
| `tests/_model_picker_check.mjs` (новый), `tests/test_webui_model_picker.py` (новый) | сценарии и чистые функции UI | 7, 8 |

## Порядок и зависимости

СЕРВЕР: `1 → 2 → 3 → 4 → 5 → 6`. Задача 5 по смыслу независима, но правит `provider.py` — после 4. Задача 6 — только после шага 0 (живая проба координатора); при провале шага 0 задача 6 заменяется записью в `docs/LLM-PROVIDERS.md` и закрывается.
UI: задача 12 волны 1.5 закоммичена → ветка UI-полосы получила коммиты задач 1–4 → `7 → 8`.

## Общая шапка `tests/test_model_picker.py` (создаёт задача 1, дополняют следующие)

```python
"""Wave 1.6 (spec docs/superpowers/specs/2026-10-08-panel-model-picker-design.md): choosing the
model inside a provider entry -- family wire keys, the model catalog and its cache, the `model`
field of a turn, what the panel remembers, content blocks, the honest key probe."""
import json

import pytest

from _fake_llama import _TURN, _FakeLlama
from h3_48gb import provider

CLAUDE = "just-ai/anthropic-claude/claude-opus-5"
GPT5 = "just-ai/openai-proxy/gpt-5"


@pytest.fixture(autouse=True)
def _fresh_models_cache():
    clear = getattr(provider, "clear_models_cache", None)
    if clear:
        clear()
    yield
    if clear:
        clear()


def _ext(port, **extra):
    return {"type": "openai", "base_url": f"http://127.0.0.1:{port}", **extra}


def _sent(fake):
    """The one chat request's body without the two big, unrelated parts -- compared with `==`."""
    (req,) = [r for r in fake.requests if r["path"] == "/v1/chat/completions"]
    return {k: v for k, v in req["body"].items() if k not in ("messages", "response_format")}


def _user(text="x"):
    return [{"role": "user", "content": text}]
```

---

## Task 1 [СЕРВЕР]: ключ лимита и temperature по семейству модели

**Files:**
- Modify: `h3_48gb/provider.py` — новая `wire_defaults(model)` и `_REASONING_MODEL_RE` рядом с `_KNOWN_MAX_TOKENS_PARAMS` (`provider.py:439`); `_chat_turn` (`provider.py:635`, `:660`) берёт умолчания из неё.
- Create: `tests/test_model_picker.py` (шапка выше + тесты задачи).

**Interfaces:**
- Produces: `provider.wire_defaults(model: str | None) -> dict` ровно `{"max_tokens_param": str, "send_temperature": bool}`:
  - `"claude"` в `model.lower()` → `{"max_tokens_param": "max_tokens", "send_temperature": True}`;
  - `re.compile(r"(?:^|[/:])(?:o[1-9]|gpt-5)(?:[-_.][a-z0-9.]+)*$", re.IGNORECASE).search(model)` → `{"max_tokens_param": "max_completion_tokens", "send_temperature": False}`;
  - иначе (и `None`/`""`) → `{"max_tokens_param": "max_tokens", "send_temperature": True}`.
- `_chat_turn`: `family = wire_defaults(body_model)`, где `body_model = cfg.get("model", cfg.get("preset", "default"))`; `token_limit_key = cfg.get("max_tokens_param", family["max_tokens_param"])`; `send_temperature = cfg.get("send_temperature", family["send_temperature"])`. Проверка `_KNOWN_MAX_TOKENS_PARAMS` остаётся.

- [ ] **Step 1: Падающие тесты**

```python
# -- Task 1: wire keys by model family ------------------------------------------------------------

WIRE_CASES = [
    (CLAUDE, "max_tokens", True),
    ("claude-3-5-sonnet", "max_tokens", True),
    (GPT5, "max_completion_tokens", False),
    ("gpt-5.1", "max_completion_tokens", False),
    ("openai/gpt-5-mini", "max_completion_tokens", False),
    ("just-ai/openai-proxy/o3-mini", "max_completion_tokens", False),
    ("o1", "max_completion_tokens", False),
    ("gpt-4o", "max_tokens", True),
    ("just-ai/openai-proxy/gpt-4o-mini", "max_tokens", True),
    ("qwen3-30b-a3b", "max_tokens", True),
    ("default", "max_tokens", True),
    ("", "max_tokens", True),
    (None, "max_tokens", True),
]


def test_wire_defaults_by_model_family():
    assert [provider.wire_defaults(model) for model, _, _ in WIRE_CASES] == [
        {"max_tokens_param": key, "send_temperature": temp} for _, key, temp in WIRE_CASES]


def test_a_reasoning_model_without_explicit_keys_gets_its_family_wire():
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        provider.chat(_ext(fake.port, model=GPT5), {}, _user())
    finally:
        fake.close()
    assert _sent(fake) == {"model": GPT5, "max_completion_tokens": 24000}


def test_explicit_keys_of_the_entry_still_win_for_its_own_model():
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = _ext(fake.port, model=GPT5, max_tokens_param="max_tokens", send_temperature=True,
               temperature=0.2)
    try:
        provider.chat(cfg, {}, _user())
    finally:
        fake.close()
    assert _sent(fake) == {"model": GPT5, "max_tokens": 24000, "temperature": 0.2}


def test_claude_and_unknown_models_keep_todays_wire():
    sent = []
    for model in (CLAUDE, "qwen3-30b-a3b"):
        fake = _FakeLlama(chat_payload=_TURN)
        try:
            provider.chat(_ext(fake.port, model=model), {}, _user())
        finally:
            fake.close()
        sent.append(_sent(fake))
    assert sent == [{"model": CLAUDE, "max_tokens": 24000, "temperature": 0.7},
                    {"model": "qwen3-30b-a3b", "max_tokens": 24000, "temperature": 0.7}]
```

- [ ] **Step 2: Красный.** `... tests/test_model_picker.py -k "wire or family or explicit or claude"` — `AttributeError: wire_defaults` и `max_tokens` вместо `max_completion_tokens` у gpt-5. Вывод — в отчёт.
- [ ] **Step 3: Код** по Interfaces. Докстринг `wire_defaults` объясняет, почему точка добавлена в регэксп (ai-writer `openai_compatible_client.py:107-116` не распознаёт `gpt-5.1`), и что ложно-положительный reasoning безопасен, ложно-отрицательный — 400.
- [ ] **Step 4: Зелёный** — файл целиком и `tests/test_provider.py` (старые тесты `max_tokens_param`/`send_temperature` обязаны остаться зелёными без правки).
- [ ] **Step 5: Мутации:** (а) убрать `.` из `[-_.]` → красный `gpt-5.1` в `test_wire_defaults_by_model_family`; (б) в `_chat_turn` вернуть `cfg.get("send_temperature", True)` → красный `test_a_reasoning_model_without_explicit_keys_gets_its_family_wire`; (в) `"claude" in model` без `.lower()` — красного нет (в `WIRE_CASES` все имена строчные); добавить строку `("Claude-Opus-5", "max_tokens", True)`, показать красный, оставить. Порядок проверок claude/reasoning тестом не пиннится — в отчёте одной фразой: имена Claude не кончаются на `gpt-5…`/`oN…`, конфликта нет.
- [ ] **Step 6: Полный прогон; коммит** — `feat(provider): ключ лимита и temperature по семейству модели, если запись их не задала (gpt-5.1 — reasoning)`.

---

## Task 2 [СЕРВЕР]: каталог моделей провайдера, кэш, разрешённые префиксы

**Files:**
- Modify: `h3_48gb/provider.py` — новый `_fetch_models(cfg, env, timeout) -> (list | None, bool, str)` (сеть и разбор из `test_provider`, `provider.py:890-919`), `test_provider` переходит на него; новые `list_models`, `clear_models_cache`, `_now`, `MODELS_TTL`, `_classify_model` (порт ai-writer `provider_service.py:303-340`), `model_group`, `allowed_prefixes`, `model_allowed`.
- Test: `tests/test_model_picker.py`.

**Interfaces:**
- `_fetch_models(cfg, env, timeout) -> tuple[list | None, bool, str]` — `(data, reachable, detail)`; `data is None` — неудача. Тексты неудач — прежние (`"провайдер недоступен: {err}"`, `"провайдер ответил, но не JSON"`, `"провайдер ответил 200, но не списком моделей"`) плюс новый для `HTTPError` не 502/503/504: `reachable=True`, `"провайдер ответил {code}: {reason}"`. `HTTPError` ловится **до** `URLError`. Токен — только заголовком.
- `test_provider` — выход на успехе и прежних неудачах не меняется (старые тесты не правятся).
- `_classify_model(model_id) -> "embedding" | "image" | "audio" | "chat"` — паттерны дословно из ai-writer.
- `model_group(entry: dict) -> str` — `entry["modelVendor"].lower()`, если непустая строка; иначе `id.split("/")[-2]`, если в id есть `/`; иначе `""`.
- `list_models(cfg, env, timeout=10.0, refresh=False) -> {"ok": bool, "detail": str, "models": [{"id", "group", "chat"}]}`:
  - `llama-local` — `{"ok": True, "detail": "локальная модель записи", "models": [{"id": cfg.get("model") or cfg.get("preset") or "default", "group": "", "chat": True}]}`, без сети;
  - `openai` — кэш `(base_url, api_key_env or "")` → `(_now(), models)`; живой, если `_now() - t < MODELS_TTL` (`MODELS_TTL = 600.0`) и не `refresh`. Разбор: элементы-словари со строковым непустым `id`; первый дубль выигрывает; `chat` = `"CHAT_COMPLETIONS" in supported_request_types`, если это список, иначе `_classify_model(id) == "chat"`; сортировка `(group, id)`. `detail` — `f"{n} {'модель' if n == 1 else 'моделей'}"`. Неудача — `{"ok": False, "detail": <из _fetch_models>, "models": []}`, не кэшируется.
- `allowed_prefixes(cfg) -> list[str] | None` — список, только если `type == "openai"`, `pick_model is True`, `models_allow` — непустой `list` непустых `str`; иначе `None`.
- `model_allowed(cfg, model) -> bool` — `model == cfg.get("model")` или (`allowed_prefixes(cfg)` не `None` и `model.startswith(p)` для какого-то `p`). Регистр значим.

- [ ] **Step 1: Падающие тесты**

```python
# -- Task 2: the catalog, its cache, the allow list ------------------------------------------------

CATALOG = {"data": [
    {"id": CLAUDE, "modelVendor": "ANTHROPIC", "supported_request_types": ["CHAT_COMPLETIONS"]},
    {"id": GPT5, "modelVendor": "OPENAI", "supported_request_types": None},
    {"id": "just-ai/openai-proxy/text-embedding-3-large", "modelVendor": "OPENAI",
     "supported_request_types": None},
    {"id": "just-ai/openrouter-proxy/qwen/qwen3-max", "modelVendor": None,
     "supported_request_types": None},
    {"id": "just-ai/yandex-yandexgpt/yandexgpt-pro", "modelVendor": "YANDEX",
     "supported_request_types": ["EMBEDDINGS"]},
    {"id": "gpt-4o"},
    {"id": CLAUDE, "modelVendor": "ANTHROPIC", "supported_request_types": ["EMBEDDINGS"]},
    {"id": 42},
    {"modelVendor": "X"},
    "not-a-dict",
]}

CATALOG_MODELS = [
    {"id": "gpt-4o", "group": "", "chat": True},
    {"id": CLAUDE, "group": "anthropic", "chat": True},
    {"id": GPT5, "group": "openai", "chat": True},
    {"id": "just-ai/openai-proxy/text-embedding-3-large", "group": "openai", "chat": False},
    {"id": "just-ai/openrouter-proxy/qwen/qwen3-max", "group": "qwen", "chat": True},
    {"id": "just-ai/yandex-yandexgpt/yandexgpt-pro", "group": "yandex", "chat": False},
]


def test_list_models_parses_a_caila_shaped_catalog():
    fake = _FakeLlama(models_payload=CATALOG)
    try:
        got = provider.list_models(_ext(fake.port, model=CLAUDE, api_key_env="K"), {"K": "sk-1"})
    finally:
        fake.close()
    assert got == {"ok": True, "detail": "6 моделей", "models": CATALOG_MODELS}
    assert [(r["path"], r["headers"].get("Authorization")) for r in fake.requests] == [
        ("/v1/models", "Bearer sk-1")]


def test_list_models_is_cached_for_ten_minutes_and_refresh_goes_to_the_network(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(provider, "_now", lambda: clock[0])
    fake = _FakeLlama(models_payload=CATALOG)
    cfg = _ext(fake.port, model=CLAUDE)
    seen = []
    try:
        seen.append(provider.list_models(cfg, {}))
        clock[0] += 599
        seen.append(provider.list_models(cfg, {}))
        hits_cached = len(fake.requests)
        seen.append(provider.list_models(cfg, {}, refresh=True))
        hits_refresh = len(fake.requests)
        clock[0] += 601
        seen.append(provider.list_models(cfg, {}))
        hits_expired = len(fake.requests)
    finally:
        fake.close()
    assert (hits_cached, hits_refresh, hits_expired) == (1, 2, 3)
    assert seen == [{"ok": True, "detail": "6 моделей", "models": CATALOG_MODELS}] * 4


def test_a_failed_catalog_is_not_cached(monkeypatch):
    answers = [(None, True, "провайдер ответил 503: Service Unavailable"),
               ([{"id": "m1"}], True, "")]
    calls = []

    def scripted(cfg, env, timeout):
        calls.append(timeout)
        return answers[len(calls) - 1]

    monkeypatch.setattr(provider, "_fetch_models", scripted)
    cfg = _ext(1, model="m1")
    got = [provider.list_models(cfg, {}), provider.list_models(cfg, {})]
    assert got == [{"ok": False, "detail": "провайдер ответил 503: Service Unavailable",
                    "models": []},
                   {"ok": True, "detail": "1 модель",
                    "models": [{"id": "m1", "group": "", "chat": True}]}]
    assert calls == [10.0, 10.0]


def test_list_models_for_llama_local_needs_no_network():
    assert provider.list_models({"type": "llama-local", "port": 1, "model": "qwen3"}, {}) == {
        "ok": True, "detail": "локальная модель записи",
        "models": [{"id": "qwen3", "group": "", "chat": True}]}


def test_an_http_error_on_the_models_list_is_an_answer_not_an_unreachable_host():
    fake = _FakeLlama(models_payload={"error": "no"}, models_status=401)
    try:
        got = provider.test_provider(_ext(fake.port, model="m"), {})
    finally:
        fake.close()
    assert got == {"ok": False, "reachable": True, "detail": "провайдер ответил 401: Unauthorized"}


ALLOW_CASES = [
    ({"type": "openai", "pick_model": True, "models_allow": ["a/"]}, ["a/"]),
    ({"type": "openai", "pick_model": True}, None),
    ({"type": "openai", "pick_model": True, "models_allow": []}, None),
    ({"type": "openai", "pick_model": True, "models_allow": ["a/", ""]}, None),
    ({"type": "openai", "pick_model": True, "models_allow": "a/"}, None),
    ({"type": "openai", "pick_model": "yes", "models_allow": ["a/"]}, None),
    ({"type": "openai", "models_allow": ["a/"]}, None),
    ({"type": "llama-local", "pick_model": True, "models_allow": ["a/"]}, None),
]


def test_allowed_prefixes_only_with_pick_model_and_a_clean_allow_list():
    assert [provider.allowed_prefixes(cfg) for cfg, _ in ALLOW_CASES] == [
        want for _, want in ALLOW_CASES]


def test_model_allowed_by_the_entry_model_or_a_prefix():
    pickable = {"type": "openai", "model": "x/def", "pick_model": True, "models_allow": ["a/"]}
    plain = {"type": "openai", "model": "x/def"}
    assert [provider.model_allowed(pickable, m) for m in ("x/def", "a/1", "b/1", "A/1")] == [
        True, True, False, False]
    assert [provider.model_allowed(plain, m) for m in ("x/def", "a/1")] == [True, False]
```

- [ ] **Step 2: Красный** (`AttributeError` на `list_models`/`allowed_prefixes`; у 401 — `reachable: False` и «провайдер недоступен: HTTP Error 401»). Вывод — в отчёт.
- [ ] **Step 3: Код** по Interfaces. `_now = time.monotonic` — модульная переменная (тест подменяет её, не `time`). Кэш — `dict` под `threading.Lock` (сервер `ThreadingHTTPServer`).
- [ ] **Step 4: Зелёный** + `tests/test_provider.py`, `tests/test_web_projects.py -k provider_test` без правок.
- [ ] **Step 5: Мутации:** (а) «последний дубль выигрывает» → красный `CATALOG_MODELS` (claude `chat: False`); (б) кэшировать неудачу → красный `test_a_failed_catalog_is_not_cached`; (в) `refresh` читает кэш → `(1, 1, 2)`; (г) `<=` вместо `<` в TTL не ловится на 599/601 — записать в отчёт, не подгонять; (д) убрать ветку `HTTPError` → красный 401-теста; (е) `models_allow` без проверки пустых строк → красный строки `["a/", ""]` (пустой префикс разрешил бы всё).
- [ ] **Step 6: Полный прогон; коммит** — `feat(provider): каталог моделей провайдера с кэшем на 10 минут, разрешённые префиксы models_allow, HTTP-ошибка /v1/models — ответ, а не «недоступен»`.

---

## Task 3 [СЕРВЕР]: маршрут списка моделей и поля ростера

**Files:**
- Modify: `h3_48gb/web.py` — `_route_get` (`web.py:3531`): `GET /api/providers/<name>/models` по форме пути (2 сегмента, второй `models`), как `_route_post` (`web.py:3576-3584`); новый `_provider_models(name)` рядом с `_test_provider` (`web.py:5705`); `_providers` (`web.py:5692-5703`) — `model` и `pick_model` в строке.
- Modify: `docs/LLM-PROVIDERS.md` — раздел «Выбор модели»: `pick_model`, `models_allow` (префиксы, модель записи разрешена всегда), правило семейств (задача 1), пример записи из спеки §3.1.
- Test: `tests/test_model_picker.py`.

**Interfaces:**
- Строка ростера: `{"name", "type", "available", "reason", "shares_gpu", "model": cfg.get("model"), "pick_model": provider.allowed_prefixes(cfg) is not None}`.
- `GET /api/providers/<name>/models[?refresh=1]` → 200 `{"ok", "provider", "model", "pick_model", "detail", "models": [{"id", "group"}]}`; `refresh` — `urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("refresh") == ["1"]`.
  - неизвестное имя — `CliError("args_invalid", f"нет провайдера {name}", {"provider": name, "known": sorted(...)})`;
  - `openai` и `available` ложно — `ok: False`, `detail` = `reason`, `models: []`, без сети;
  - выбор выключен — `ok: True`, `detail: "выбор модели выключен"`, `models: [{"id": model, "group": model_group({"id": model})}]`, без сети;
  - выбор включён — `list_models(cfg, env, refresh=...)`; успех: `chat` и `model_allowed`; модель записи добавляется, если её нет; сортировка `(group, id)`; `detail: f"{len(models)} из {M} разрешены (models_allow)"`, M = длина каталога; неудача: `ok: False`, `detail` из `list_models`, `models` — только модель записи.

- [ ] **Step 1: Падающие тесты**

```python
# -- Task 3: GET /api/providers/<name>/models, roster fields ---------------------------------------

# (импорт перенести в шапку файла)
from test_chat_web import _serve  # noqa: E402,F401 -- the fixture, see test_chat_web's docstring

ENV = "CAILA_API_KEY=sk-caila\n"


def _caila(port, **extra):
    return {"type": "openai", "base_url": f"http://127.0.0.1:{port}", "model": CLAUDE,
            "api_key_env": "CAILA_API_KEY", "shares_gpu": False, "pick_model": True,
            "models_allow": ["just-ai/anthropic-claude/", "just-ai/openai-proxy/"], **extra}


def test_the_models_route_lists_only_allowed_chat_models_and_caches(_serve):
    fake = _FakeLlama(models_payload=CATALOG)
    try:
        srv = _serve(providers={"caila": _caila(fake.port)}, active="caila", env=ENV)
        first = srv.get_json("/api/providers/caila/models")
        second = srv.get_json("/api/providers/caila/models")
        refreshed = srv.get_json("/api/providers/caila/models?refresh=1")
    finally:
        fake.close()
    expected = {"ok": True, "provider": "caila", "model": CLAUDE, "pick_model": True,
                "detail": "2 из 6 разрешены (models_allow)",
                "models": [{"id": CLAUDE, "group": "anthropic"}, {"id": GPT5, "group": "openai"}]}
    assert [first, second, refreshed] == [expected] * 3
    assert [(r["path"], r["headers"].get("Authorization")) for r in fake.requests] == [
        ("/v1/models", "Bearer sk-caila"), ("/v1/models", "Bearer sk-caila")]


def test_the_entry_model_is_listed_even_outside_the_catalog(_serve):
    fake = _FakeLlama(models_payload=CATALOG)
    try:
        srv = _serve(providers={"caila": _caila(fake.port, model="vendor/custom-1")},
                     active="caila", env=ENV)
        got = srv.get_json("/api/providers/caila/models")
    finally:
        fake.close()
    assert got == {"ok": True, "provider": "caila", "model": "vendor/custom-1", "pick_model": True,
                   "detail": "3 из 6 разрешены (models_allow)",
                   "models": [{"id": CLAUDE, "group": "anthropic"}, {"id": GPT5, "group": "openai"},
                              {"id": "vendor/custom-1", "group": "vendor"}]}


def test_the_models_route_without_a_token_never_touches_the_network(_serve):
    fake = _FakeLlama(models_payload=CATALOG)
    try:
        srv = _serve(providers={"caila": _caila(fake.port)}, active="caila")
        got = srv.get_json("/api/providers/caila/models")
    finally:
        fake.close()
    assert got == {"ok": False, "provider": "caila", "model": CLAUDE, "pick_model": True,
                   "detail": "нет токена CAILA_API_KEY", "models": []}
    assert fake.requests == []


def test_the_models_route_with_picking_off_lists_the_entry_model_only(_serve):
    fake = _FakeLlama(models_payload=CATALOG)
    try:
        srv = _serve(providers={"plain": _ext(fake.port, model="m", shares_gpu=False)},
                     active="plain")
        got = srv.get_json("/api/providers/plain/models")
    finally:
        fake.close()
    assert got == {"ok": True, "provider": "plain", "model": "m", "pick_model": False,
                   "detail": "выбор модели выключен", "models": [{"id": "m", "group": ""}]}
    assert fake.requests == []


def test_the_models_route_reports_a_broken_catalog_and_keeps_the_entry_model(_serve):
    fake = _FakeLlama(models_payload={"error": "x"})
    try:
        srv = _serve(providers={"caila": _caila(fake.port)}, active="caila", env=ENV)
        got = srv.get_json("/api/providers/caila/models")
    finally:
        fake.close()
    assert got == {"ok": False, "provider": "caila", "model": CLAUDE, "pick_model": True,
                   "detail": "провайдер ответил 200, но не списком моделей",
                   "models": [{"id": CLAUDE, "group": "anthropic-claude"}]}


def test_the_models_route_refuses_an_unknown_name(_serve):
    srv = _serve(providers={"caila": _caila(1)}, active="caila", env=ENV)
    assert srv.get_json_raw("/api/providers/nope/models") == (400, {
        "ok": False, "error": {"code": "args_invalid", "message": "нет провайдера nope",
                               "detail": {"provider": "nope", "known": ["caila"]}}})


def test_the_roster_carries_model_and_pick_model(_serve):
    srv = _serve(providers={"caila": _caila(1),
                            "plain": _ext(1, model="m", shares_gpu=False),
                            "half": _ext(1, model="h", shares_gpu=False, pick_model=True)},
                 active="caila", env=ENV)
    assert srv.get_json("/api/providers") == {"ok": True, "active": "caila", "providers": [
        {"name": "caila", "type": "openai", "available": True, "reason": None,
         "shares_gpu": False, "model": CLAUDE, "pick_model": True},
        {"name": "plain", "type": "openai", "available": True, "reason": None,
         "shares_gpu": False, "model": "m", "pick_model": False},
        {"name": "half", "type": "openai", "available": True, "reason": None,
         "shares_gpu": False, "model": "h", "pick_model": False}]}
```

- [ ] **Step 2: Красный** — 404 `not_found` на маршруте, лишних ключей нет в ростере. Вывод — в отчёт.
- [ ] **Step 3: Код** по Interfaces; запись в `docs/LLM-PROVIDERS.md`.
- [ ] **Step 4: Зелёный** + `tests/test_chat_web.py tests/test_chat_project.py` без правок (их тесты ростера проверяют проекции и пустую роспись — проверить, что это так; если какой-то пиннит строку целиком — обновить ожидание и перечислить в отчёте).
- [ ] **Step 5: Мутации:** (а) убрать фильтр `chat` → красный первого теста (появится `text-embedding-3-large`); (б) не добавлять модель записи → красный `..._outside_the_catalog`; (в) проверку `available` поставить после сети → красный `fake.requests == []`; (г) `refresh` не читается → один запрос вместо двух — красный списка запросов.
- [ ] **Step 6: Полный прогон; коммит** — `feat(web): GET /api/providers/<name>/models — разрешённые чат-модели с кэшем и ?refresh=1; model и pick_model в ростере`.

---

## Task 4 [СЕРВЕР]: `model` в ходе чата и в `/scenario/generate`, запоминание

**Files:**
- Modify: `h3_48gb/provider.py` — `with_model(name, cfg, model)`, `_MODEL_ID_RE`, `MODEL_ID_TEXT`.
- Modify: `h3_48gb/web.py` — `_chat_message` (`allowed`, `web.py:6246`) + `"model"`; `_locked_turn` после проверки `available` (`web.py:6296-6301`) — `with_model`; перед `_write_session` (`web.py:6457`) — `session["provider"]`, `session["model"]`; `_generate_project_scenario` (`allowed`, `web.py:5144`) + `"model"`, `with_model` после проверки `available` (`web.py:5191-5196`), `update_scenario(..., scenario_llm=...)` (`web.py:5225`).
- Modify: `h3_48gb/project.py` — `"scenario_llm"` в `_SCENARIO_FIELDS` (`project.py:117`). В `_OWNED_TOP_LEVEL_FIELDS` **не** добавлять: поле живёт в `_extra` и выходит в `as_dict` без нового атрибута — проверить тестом, не чтением.
- Test: `tests/test_model_picker.py`.

**Interfaces:**
- `provider.MODEL_ID_TEXT = "`model` — id модели: латиница, цифры и ._:/@+-, до 200 знаков, первый знак — буква или цифра"`.
- `provider.with_model(name, cfg, model) -> dict`, порядок проверок:
  1. `not model or model == cfg.get("model")` → `cfg` как есть;
  2. `not _MODEL_ID_RE.fullmatch(model)` (`r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}"`) → `ValueError(MODEL_ID_TEXT)`;
  3. `allowed_prefixes(cfg) is None` → `ValueError(f"у провайдера {name} модель не выбирается: нет pick_model и models_allow в providers.json")`;
  4. `not model_allowed(cfg, model)` → `ValueError(f"модель {model} не входит в models_allow провайдера {name}")`;
  5. иначе — копия `cfg` **без** `max_tokens_param` и `send_temperature`, с `model`.
- web: `model = self._string_of(payload, "model")`; `ValueError` → `CliError("args_invalid", str(exc), {"provider": name, "model": model})`. В процедурной ветке `/scenario/generate` `model` не читается.
- Сессия после успешного хода: `session["provider"] = name`, `session["model"] = cfg.get("model")` (итоговая модель хода).
- Проект: `scenario_llm = {"provider": name, "model": cfg.get("model")}` в LLM-ветке, `None` в процедурной.

- [ ] **Step 1: Падающие тесты**

```python
# -- Task 4: `model` in a turn, what the panel remembers -------------------------------------------

# (импорт перенести в шапку файла)
from test_web_projects import (  # noqa: E402
    _clip_project_with_approved_track, _scenario_section, _scenario_turn_payload)


def _tuned_caila(port):
    """The entry's own explicit wire keys describe *its* model (Claude) -- a turn on another model
    must not inherit them (spec §3.2)."""
    return _caila(port, max_tokens_param="max_tokens", send_temperature=True, temperature=0.3)


def _chat_turn_on(srv, body):
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, answer = srv.post_json_raw(f"/api/chat/{sid}/message",
                                       {"text": "x", "prompt": "", "provider": "caila", **body})
    return sid, status, answer


def test_chat_model_override_uses_the_family_wire_not_the_entry_keys(_serve):
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        srv = _serve(providers={"caila": _tuned_caila(fake.port)}, active="caila", env=ENV)
        sid, status, answer = _chat_turn_on(srv, {"model": GPT5})
        session = srv.get_json(f"/api/chat/{sid}")
    finally:
        fake.close()
    assert status == 200, answer
    assert _sent(fake) == {"model": GPT5, "max_completion_tokens": 24000}
    assert (session["provider"], session["model"]) == ("caila", GPT5)


def test_chat_without_a_model_keeps_the_entry_model_and_its_keys(_serve):
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        srv = _serve(providers={"caila": _tuned_caila(fake.port)}, active="caila", env=ENV)
        sid, status, answer = _chat_turn_on(srv, {})
        session = srv.get_json(f"/api/chat/{sid}")
    finally:
        fake.close()
    assert status == 200, answer
    assert _sent(fake) == {"model": CLAUDE, "max_tokens": 24000, "temperature": 0.3}
    assert (session["provider"], session["model"]) == ("caila", CLAUDE)


MODEL_ID_TEXT = ("`model` — id модели: латиница, цифры и ._:/@+-, до 200 знаков, "
                 "первый знак — буква или цифра")

REFUSALS = [
    ("caila", "just-ai/google-gemini/gemini-3-pro",
     "модель just-ai/google-gemini/gemini-3-pro не входит в models_allow провайдера caila"),
    ("caila", "gpt 5", MODEL_ID_TEXT),
    ("caila", "a" * 201, MODEL_ID_TEXT),
    ("plain", "other",
     "у провайдера plain модель не выбирается: нет pick_model и models_allow в providers.json"),
]


@pytest.mark.parametrize("name, model, message", REFUSALS)
def test_chat_refuses_a_bad_model_before_the_network(_serve, name, model, message):
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        srv = _serve(providers={"caila": _caila(fake.port), "plain": _ext(fake.port, model="m")},
                     active="caila", env=ENV)
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        status, answer = srv.post_json_raw(f"/api/chat/{sid}/message",
                                           {"text": "x", "prompt": "", "provider": name,
                                            "model": model})
        session = srv.get_json(f"/api/chat/{sid}")
    finally:
        fake.close()
    assert (status, answer) == (400, {"ok": False, "error": {
        "code": "args_invalid", "message": message, "detail": {"provider": name, "model": model}}})
    assert fake.requests == []
    assert (session["messages"], "model" in session) == ([], False)


def test_scenario_generate_with_a_model_and_what_the_project_remembers(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "written by gpt-5")]))
    try:
        srv = _serve(providers={"caila": _tuned_caila(fake.port)}, active="caila", env=ENV)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        generated = srv.post_json(f"/api/projects/{pid}/scenario/generate",
                                  {"provider": "caila", "model": GPT5})
        procedural = srv.post_json(f"/api/projects/{pid}/scenario/generate", {"procedural": True})
    finally:
        fake.close()
    assert _sent(fake) == {"model": GPT5, "max_completion_tokens": 24000}
    assert generated["project"]["scenario_llm"] == {"provider": "caila", "model": GPT5}
    assert procedural["project"]["scenario_llm"] is None


def test_scenario_refuses_a_model_outside_the_allow_list(_serve, monkeypatch):
    fake = _FakeLlama(chat_payload=_scenario_turn_payload([
        _scenario_section("verse", 0.0, 16.0, "must never be written")]))
    try:
        srv = _serve(providers={"caila": _caila(fake.port)}, active="caila", env=ENV)
        pid = _clip_project_with_approved_track(srv, monkeypatch, duration=16.0)
        status, answer = srv.post_json_raw(f"/api/projects/{pid}/scenario/generate",
                                           {"provider": "caila", "model": "other/x"})
        project = srv.get_json(f"/api/projects/{pid}")["project"]
    finally:
        fake.close()
    assert (status, answer) == (400, {"ok": False, "error": {
        "code": "args_invalid",
        "message": "модель other/x не входит в models_allow провайдера caila",
        "detail": {"provider": "caila", "model": "other/x"}}})
    assert fake.requests == []
    assert (project["scenario_scenes"], "scenario_llm" in project) == ([], False)
```

Текст `MODEL_ID_TEXT` в тесте — литерал, а не ссылка на константу модуля: тест пиннит то, что увидит человек. Если `_clip_project_with_approved_track` требует в ростере иного провайдера — добавить его в `providers`, не ослабляя ожиданий.

- [ ] **Step 2: Красный** — 400 `args_invalid` «this route takes [...] and nothing else; it was also sent ['model']» (`_json_request`, `web.py:3765-3771`) и `KeyError: 'provider'` у сессии. Вывод — в отчёт.
- [ ] **Step 3: Код** по Interfaces. Проверка `model` — до `_llama_for`/`gpu_busy` и до любой записи. В `_generate_project_scenario` переменная `scenario_llm = None` объявляется до ветвления `procedural`.
- [ ] **Step 4: Зелёный** + `tests/test_chat_web.py tests/test_web_projects.py tests/test_chat_project.py tests/test_project.py`.
- [ ] **Step 5: Мутации:** (а) в `with_model` не снимать `max_tokens_param`/`send_temperature` → красный первого теста (`max_tokens` + `temperature` у gpt-5); (б) убрать шаг 4 (`model_allowed`) → красный строки gemini; (в) `with_model` после `_llama_for` и записи — не мутация, а проверка порядка: переставить проверку после `provider.chat` → красный `fake.requests == []`; (г) процедурная ветка не пишет `scenario_llm` → красный `procedural ... is None`; (д) записывать `session["model"]` до хода (при отказе провайдера останется) — показать, что красного нет, и добавить тест: провайдер отвечает `chat_status=500` → `"model" in session` ложно. Код теста — в отчёт.
- [ ] **Step 6: Полный прогон; коммит** — `feat(chat,projects): model в ходе чата и в /scenario/generate — только из models_allow, явные ключи записи не переходят на чужую модель; сессия и проект помнят провайдера и модель`.

---

## Task 5 [СЕРВЕР]: содержимое ответа списком блоков (проверка гипотезы 4.1.9)

**Files:**
- Modify: `h3_48gb/provider.py` — `_content_text(content)`; `_envelope_to_turn` (`provider.py:684-708`) возвращает `_content_text(content)`.
- Test: `tests/test_model_picker.py`.

**Interfaces:** `_content_text(content)` — список → склейка `block["text"]` у блоков-словарей с `type == "text"` и строковым `text`, по порядку; остальное (`thinking`, не-словари) пропускается; не список — как есть (включая `None`). SSE-путь (`_read_sse`) не трогается: блоки в `delta.content` не наблюдались — записать в «не проверено».

- [ ] **Step 1: Падающие тесты** — первый одновременно **проверка гипотезы**: его красный текст и число запросов (ожидается `bad_model_json` после двух запросов) — в отчёт как факт для `REPORT.md` §1.3.

```python
# -- Task 5: Anthropic content blocks ---------------------------------------------------------------

def _blocks_reply(blocks):
    return {"choices": [{"message": {"content": blocks}, "finish_reason": "stop"}]}


def test_content_blocks_are_joined_from_their_text_blocks_only():
    turn = json.loads(_TURN["choices"][0]["message"]["content"])
    text = json.dumps(turn, ensure_ascii=False)
    fake = _FakeLlama(chat_payload=_blocks_reply([
        {"type": "thinking", "thinking": "{\"reply\": \"not this\", \"prompt\": null}"},
        {"type": "text", "text": text[:25]}, "stray", {"type": "text", "text": text[25:]}]))
    try:
        got = provider.chat(_ext(fake.port, model=CLAUDE), {}, _user())
    finally:
        fake.close()
    assert got == turn
    assert [r["path"] for r in fake.requests] == ["/v1/chat/completions"]


def test_blocks_without_text_are_still_a_named_format_failure():
    fake = _FakeLlama(chat_payload=_blocks_reply([{"type": "thinking", "thinking": "..."}]))
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_ext(fake.port, model=CLAUDE), {}, _user())
    finally:
        fake.close()
    assert (err.value.code, str(err.value)) == ("bad_model_json", "модель не удержала формат: ")
    assert [r["path"] for r in fake.requests] == ["/v1/chat/completions"] * 2
```

- [ ] **Step 2: Красный** первого теста — вывод в отчёт (это и есть подтверждение/опровержение гипотезы). Второй тест может быть зелёным уже сейчас (сообщение из списка `[...][:400]` будет другим — сверить; если зелёный до кода, записать почему и что он защищает после).
- [ ] **Step 3: Код.**
- [ ] **Step 4: Зелёный** + `tests/test_provider.py`.
- [ ] **Step 5: Мутации:** (а) склеивать и `thinking` → красный первого (`JSONDecodeError` → повтор → `bad_model_json`); (б) вернуть `content` как есть → красный первого.
- [ ] **Step 6: Полный прогон; коммит** — `fix(provider): ответ содержимым-списком блоков (Anthropic через шлюз) склеивается из text-блоков`.

---

## Task 6 [СЕРВЕР]: честная проба ключа (гипотеза 4.1.8)

**Files:**
- Modify: `h3_48gb/provider.py` — `test_provider` после успешного списка моделей.
- Modify: `tests/test_web_projects.py` — `test_provider_test_route_never_leaks_the_token` (сейчас `(req,) = fake.requests`; станет два запроса — пиннить оба пути и оба заголовка `Authorization`).
- Modify: `docs/LLM-PROVIDERS.md` — итог живой пробы (шаг 0) с датой.
- Test: `tests/test_model_picker.py`.

- [ ] **Step 0 (координатор, не исполнитель): живая проба CAILA.** Ключ не печатать, не класть в файлы плана/отчёта.
  ```bash
  # (1) фальшивый ключ — ожидание: 401. Если не 401 — шлюз смотрит тело раньше ключа, проба бесполезна.
  curl -s -o /dev/null -w '%{http_code}\n' -X POST https://caila.io/api/adapters/openai/v1/chat/completions \
    -H 'Authorization: Bearer fake-key-000' -H 'Content-Type: application/json' \
    -d '{"model": "just-ai/anthropic-claude/claude-opus-5", "messages": []}'
  # (2) настоящий ключ из /home/alex/Outputs/h3-panel/.env — ожидание: 400/422, не 401 и не 200.
  set -a; . /home/alex/Outputs/h3-panel/.env; set +a
  curl -s -w '\n%{http_code}\n' -X POST https://caila.io/api/adapters/openai/v1/chat/completions \
    -H "Authorization: Bearer $CAILA_API_KEY" -H 'Content-Type: application/json' \
    -d '{"model": "just-ai/anthropic-claude/claude-opus-5", "messages": []}'
  ```
  Плюс: в кабинете CAILA после (2) нет списания. **Гейт:** (1) = 401 **и** (2) ∉ {401, 403} **и** списания нет → задача идёт. Иначе: записать коды в `docs/LLM-PROVIDERS.md` («проверить ключ без платного вызова нельзя») и закрыть задачу без кода.

**Interfaces:** только при `type == "openai"` и непустом `api_key_env`, после успешного `_fetch_models`: `POST {base_url}/v1/chat/completions`, тело `{"model": cfg.get("model", ""), "messages": []}`, тот же `Authorization`, тот же `timeout`. Исход (`base` — прежний `detail` успеха, `"N модель(ей): id, …"`):
- `HTTPError` 401/403 → `{"ok": False, "reachable": True, "detail": f"ключ не принят: {code} {reason}"}`;
- `HTTPError` 502/503/504, `URLError`/`OSError`/`HTTPException` → `ok: True`, `detail: f"{base}; ключ не проверен: {code} {reason}"` (для не-HTTP — `"; ключ не проверен: шлюз не ответил"`), `models` как прежде;
- другой `HTTPError` → `ok: True`, `detail: f"{base}; ключ принят (проба ответила {code})"`;
- 200 → `ok: True`, `detail: f"{base}; ключ принят"`.
Тело ответа пробы не читается.

- [ ] **Step 1: Падающие тесты**

```python
# -- Task 6: the honest key probe -------------------------------------------------------------------

KEY_CASES = [
    (401, {"ok": False, "reachable": True, "detail": "ключ не принят: 401 Unauthorized"}),
    (403, {"ok": False, "reachable": True, "detail": "ключ не принят: 403 Forbidden"}),
    (400, {"ok": True, "reachable": True, "models": ["m1"],
           "detail": "1 модель: m1; ключ принят (проба ответила 400)"}),
    (422, {"ok": True, "reachable": True, "models": ["m1"],
           "detail": "1 модель: m1; ключ принят (проба ответила 422)"}),
    (503, {"ok": True, "reachable": True, "models": ["m1"],
           "detail": "1 модель: m1; ключ не проверен: 503 Service Unavailable"}),
    (200, {"ok": True, "reachable": True, "models": ["m1"], "detail": "1 модель: m1; ключ принят"}),
]


@pytest.mark.parametrize("status, expected", KEY_CASES)
def test_the_key_probe_tells_a_rejected_key_from_an_accepted_one(status, expected):
    fake = _FakeLlama(models_payload={"data": [{"id": "m1"}]}, chat_status=status,
                      chat_payload={"error": {"message": "Bearer sk-x echoed"}})
    try:
        got = provider.test_provider(_ext(fake.port, model="m1", api_key_env="K"), {"K": "sk-x"})
    finally:
        fake.close()
    assert got == expected
    assert [(r["path"], r.get("body"), r["headers"].get("Authorization"))
            for r in fake.requests] == [
        ("/v1/models", None, "Bearer sk-x"),
        ("/v1/chat/completions", {"model": "m1", "messages": []}, "Bearer sk-x")]


def test_no_key_env_means_no_key_probe():
    fake = _FakeLlama(models_payload={"data": [{"id": "m1"}]}, chat_status=401)
    try:
        got = provider.test_provider(_ext(fake.port, model="m1"), {})
    finally:
        fake.close()
    assert got == {"ok": True, "reachable": True, "detail": "1 модель: m1", "models": ["m1"]}
    assert [r["path"] for r in fake.requests] == ["/v1/models"]
```

Если на машине исполнителя системный прокси превращает 503 фейка в другой ответ (см. комментарий `provider.py:749-754`) — записать фактический ответ в отчёт и не подгонять ожидание без согласования с координатором.

- [ ] **Step 2: Красный** (нет второго запроса). Вывод — в отчёт.
- [ ] **Step 3: Код**; правка `test_provider_test_route_never_leaks_the_token`:
  ```python
  assert [(r["path"], r["headers"].get("Authorization")) for r in fake.requests] == [
      ("/v1/models", "Bearer sk-very-secret"),
      ("/v1/chat/completions", "Bearer sk-very-secret")]
  ```
- [ ] **Step 4: Зелёный** + `tests/test_web_projects.py -k provider_test`.
- [ ] **Step 5: Мутации:** (а) 403 считать принятым → красный строки 403; (б) пробовать без `api_key_env` → красный `test_no_key_env_means_no_key_probe`; (в) слать в пробе `"messages": [{"role": "user", "content": "ping"}]` → красный списка запросов (защита от платного вызова); (г) дописать тело ответа пробы в `detail` → красный через `==` (фейк отвечает `"Bearer sk-x echoed"`, оно попало бы в ответ).
- [ ] **Step 6: Полный прогон; коммит** — `feat(provider): «Проверить» отличает неверный ключ — пустой chat/completions после /v1/models, 401/403 — ключ не принят`.

---

## Task 7 [UI]: выбор модели в чате

**Предусловие:** задача 12 волны 1.5 закоммичена; в ветке UI-полосы есть коммиты задач 1–4. Номера строк ниже — с `c6b0462b`, после задачи 12 **сверить**.

**Files:**
- Modify: `h3_48gb/webui/app.js` — чистые функции (ниже); `loadProviders` (`app.js:4334`), обработчик `#chat-provider` (`app.js:5725`), отправка хода (тело `POST /api/chat/<id>/message`, рядом с `provider: $("chat-provider").value`), `enterChat` (`app.js:4389`) — восстановление по `session.provider/model`.
- Modify: `h3_48gb/webui/index.html` — после `<select id="chat-provider">` (`index.html:452`) `<span id="chat-model-box" class="sel model" hidden></span>`.
- Modify: `h3_48gb/webui/style.css` — `.model-pick`, `.model-count`, `.model-q` (токены `:root`, правило 390 px в существующем `@media (max-width: 420px)`).
- Create: `tests/_model_picker_check.mjs`, `tests/test_webui_model_picker.py`.

**Interfaces (экспорт `app.js`):**
- `MODEL_SEARCH_MIN = 12`.
- `modelShortName(id)` — хвост после последнего `/`.
- `providerCaption(provider, model)` — `"caila · claude-opus-5"`; без модели — `"caila"`.
- `modelStorageKey(provider)` — `` `h3.model.${provider}` ``.
- `filterModels(models, query, chosen)` — без учёта регистра подстрока `query` в `id`; пустой запрос — все; `chosen` остаётся всегда.
- `modelOptionsHtml(models, chosen)` — `<optgroup label="…">` по `group` в порядке прихода (сервер отсортировал), пустая группа — `"другие"`; `<option value="{id}" title="{id}"[ selected]>{modelShortName}</option>`; всё через `escapeHtml`.
- `modelPickerHtml(prefix, {models, query, chosen, busy})` — `<span class="model-pick">` + (при `models.length > MODEL_SEARCH_MIN`) `<input type="search" class="inp model-q" id="{p}-model-q" value="{query}" placeholder="поиск модели" aria-label="поиск модели">` + `<select class="pick model-select" id="{p}-model" aria-label="модель">{modelOptionsHtml(filterModels(...))}</select>` + `<span class="hint model-count">{shown} из {total}</span>` + `<button class="ghost" type="button" data-act="{p}-model-refresh">Обновить</button>` + `</span>`; `busy` — ` disabled` у select, input и кнопки.
- `initialModel(row, ids, session, saved)` — `!row.pick_model` → `row.model || ""`; `session.provider === row.name` и `session.model` в `ids` → он; `saved` в `ids` → он; иначе `row.model || ""`.
- Проводка: при `pick_model` выбранной строки — `GET /api/providers/<name>/models` (с `?refresh=1` по «Обновить»), `#chat-model-box` открыт и заполнен `modelPickerHtml("chat", …)`; иначе скрыт и пуст. Поиск (`input` на `#chat-model-q`) меняет только `innerHTML` у `#chat-model` и текст счётчика — поле ввода не перерисовывается (фокус). `change` на `#chat-model` → выбор + `localStorage.setItem(modelStorageKey(name), id)` в try/catch. В теле хода `model` — только при `pick_model` и непустом выборе. Обработчики `change`/`input`/`click` — делегированные на `document` (элементы пересоздаются `innerHTML`), с точным сравнением `event.target.id === "chat-model"`.

- [ ] **Step 1: Тесты чистых функций** (`tests/test_webui_model_picker.py`; `_js`, `_needs_node` — как в `tests/test_webui_gaps.py`)

```python
"""Wave 1.6 UI: the model picker's pure functions with exact values, its wiring through
tests/_model_picker_check.mjs."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_web import _needs_node, _node_eval

_CHECK = Path(__file__).resolve().parent / "_model_picker_check.mjs"
_APP_URL = (Path(__file__).resolve().parent.parent / "h3_48gb" / "webui" / "app.js").as_uri()

CLAUDE = "just-ai/anthropic-claude/claude-opus-5"
GPT5 = "just-ai/openai-proxy/gpt-5"
MODELS_JS = (f"[{{id: 'gpt-4o', group: ''}}, {{id: '{CLAUDE}', group: 'anthropic'}}, "
             f"{{id: '{GPT5}', group: 'openai'}}]")


def _js(expr):
    return _node_eval(f"console.log(JSON.stringify({expr}));")


def _check(scenario):
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    result = subprocess.run([shutil.which("node"), str(_CHECK), _APP_URL, scenario],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, f"{scenario}: {result.stderr}"
    return json.loads(result.stdout)


@_needs_node
def test_names_captions_and_keys():
    assert _js(f"[app.modelShortName('{CLAUDE}'), app.modelShortName('gpt-4o'), "
               f"app.providerCaption('caila', '{CLAUDE}'), app.providerCaption('caila', ''), "
               f"app.modelStorageKey('caila')]") == [
        "claude-opus-5", "gpt-4o", "caila · claude-opus-5", "caila", "h3.model.caila"]


@_needs_node
def test_model_picker_html_short_list():
    assert _js(f"app.modelPickerHtml('chat', {{models: {MODELS_JS}, query: '', "
               f"chosen: '{GPT5}', busy: false}})") == (
        '<span class="model-pick"><select class="pick model-select" id="chat-model" '
        'aria-label="модель"><optgroup label="другие"><option value="gpt-4o" title="gpt-4o">'
        'gpt-4o</option></optgroup><optgroup label="anthropic"><option '
        f'value="{CLAUDE}" title="{CLAUDE}">claude-opus-5</option></optgroup><optgroup '
        f'label="openai"><option value="{GPT5}" title="{GPT5}" selected>gpt-5</option>'
        '</optgroup></select><span class="hint model-count">3 из 3</span><button class="ghost" '
        'type="button" data-act="chat-model-refresh">Обновить</button></span>')


@_needs_node
def test_model_picker_html_long_list_has_search_and_keeps_the_choice():
    many = "Array.from({length: 13}, (_, i) => ({id: `v/m${i}`, group: 'v'}))"
    assert _js(f"app.modelPickerHtml('scenario', {{models: {many}, query: 'M1', "
               f"chosen: 'v/m7', busy: true}})") == (
        '<span class="model-pick"><input type="search" class="inp model-q" id="scenario-model-q" '
        'value="M1" placeholder="поиск модели" aria-label="поиск модели" disabled><select '
        'class="pick model-select" id="scenario-model" aria-label="модель" disabled><optgroup '
        'label="v"><option value="v/m1" title="v/m1">m1</option><option value="v/m7" '
        'title="v/m7" selected>m7</option><option value="v/m10" title="v/m10">m10</option>'
        '<option value="v/m11" title="v/m11">m11</option><option value="v/m12" title="v/m12">'
        'm12</option></optgroup></select><span class="hint model-count">5 из 13</span><button '
        'class="ghost" type="button" data-act="scenario-model-refresh" disabled>Обновить</button>'
        '</span>')


@_needs_node
def test_initial_model_order():
    row = "{name: 'caila', model: 'c', pick_model: true}"
    off = "{name: 'caila', model: 'c', pick_model: false}"
    ids = "['c', 'g', 's']"
    assert _js(f"[app.initialModel({row}, {ids}, {{provider: 'caila', model: 's'}}, 'g'), "
               f"app.initialModel({row}, {ids}, {{provider: 'other', model: 's'}}, 'g'), "
               f"app.initialModel({row}, {ids}, {{provider: 'caila', model: 'gone'}}, 'gone'), "
               f"app.initialModel({row}, {ids}, null, null), "
               f"app.initialModel({off}, {ids}, {{provider: 'caila', model: 's'}}, 'g')]") == [
        "s", "g", "c", "c", "c"]
```

- [ ] **Step 2: Сценарии проводки** (`tests/_model_picker_check.mjs`; импорт из `./_ui_harness.mjs`, как `_ui_gaps_check.mjs`). Фикстура сессии — по `_create_chat` (`web.py`); если `enterChat` требует ещё полей — дописать, не ослабляя ожиданий.

```js
// Wave 1.6 model picker wiring; usage: node _model_picker_check.mjs <appUrl> <scenario>
import { calls, getElementById, start, ok, sleep, fire } from "./_ui_harness.mjs";

const [, , appUrl, scenario] = process.argv;
const CLAUDE = "just-ai/anthropic-claude/claude-opus-5";
const GPT5 = "just-ai/openai-proxy/gpt-5";
const ROW = { name: "caila", type: "openai", available: true, reason: null, shares_gpu: false,
  model: CLAUDE, pick_model: true };
const LIST = { ok: true, provider: "caila", model: CLAUDE, pick_model: true,
  detail: "2 из 6 разрешены (models_allow)",
  models: [{ id: CLAUDE, group: "anthropic" }, { id: GPT5, group: "openai" }] };
const SESSION = (over = {}) => ({ ok: true, id: "c1", source: { kind: "new" }, mode: "t2va",
  image: "", end_image: "", duration: 10, messages: [], prompt: "", ...over });
const TURN = ok({ ok: true, reply: "ок", prompt: null, slug: null, project: null, warning: null,
  llm: { status: "up", provider: "caila" } });
const gets = () => calls.filter((c) => c.method === "GET" && c.url.startsWith("/api/providers/"))
  .map((c) => c.url);
const turnBodies = () => calls.filter((c) => c.method === "POST" && c.url === "/api/chat/c1/message")
  .map((c) => c.body);
const send = async (text) => {
  getElementById("chat-input").value = text;
  getElementById("chat-form").__listeners.submit[0]({ preventDefault() {} });
  await sleep(120);
};
const pick = (id) => fire("change", { id: "chat-model", value: id });

const SCENARIOS = {
  async chat_pick_model_sends_it() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, { "GET /api/providers": ok({ active: "caila", providers: [ROW] }),
      "GET /api/providers/caila/models": ok(LIST), "GET /api/chat/c1": ok(SESSION()),
      "POST /api/chat/c1/message": TURN });
    await sleep(120);
    const shown = !getElementById("chat-model-box").hidden;
    pick(GPT5);
    await send("x");
    return { shown, gets: gets(), saved: localStorage.getItem("h3.model.caila"),
             models: turnBodies().map((b) => b.model) };
  },
  async chat_session_model_wins_over_storage() {
    localStorage.setItem("h3.model.caila", CLAUDE);
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, { "GET /api/providers": ok({ active: "caila", providers: [ROW] }),
      "GET /api/providers/caila/models": ok(LIST),
      "GET /api/chat/c1": ok(SESSION({ provider: "caila", model: GPT5 })),
      "POST /api/chat/c1/message": TURN });
    await sleep(120);
    await send("x");
    return { models: turnBodies().map((b) => b.model) };
  },
  async chat_no_pick_model_sends_no_model() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, { "GET /api/providers": ok({ active: "plain", providers: [
        { ...ROW, name: "plain", model: "m", pick_model: false }] }),
      "GET /api/chat/c1": ok(SESSION()), "POST /api/chat/c1/message": TURN });
    await sleep(120);
    await send("x");
    return { shown: !getElementById("chat-model-box").hidden, gets: gets(),
             keys: turnBodies().map((b) => Object.keys(b).sort()) };
  },
  async chat_refresh_asks_the_server_again() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, { "GET /api/providers": ok({ active: "caila", providers: [ROW] }),
      "GET /api/providers/caila/models": ok(LIST),
      "GET /api/providers/caila/models?refresh=1": ok(LIST), "GET /api/chat/c1": ok(SESSION()) });
    await sleep(120);
    fire("click", { dataset: { act: "chat-model-refresh" },
      closest(sel) { return sel === "button[data-act]" ? this : null; } });
    await sleep(80);
    return { gets: gets() };
  },
};

const fn = SCENARIOS[scenario];
if (!fn) { process.stderr.write(`no scenario ${scenario}\n`); process.exit(1); }
process.stdout.write(JSON.stringify(await fn()));
process.exit(0);
```

```python
CHAT_EXPECTED = {
    "chat_pick_model_sends_it": {"shown": True, "gets": ["/api/providers/caila/models"],
                                 "saved": GPT5, "models": [GPT5]},
    "chat_session_model_wins_over_storage": {"models": [GPT5]},
    "chat_no_pick_model_sends_no_model": {"shown": False, "gets": [], "keys": [None]},
    "chat_refresh_asks_the_server_again": {"gets": [
        "/api/providers/caila/models", "/api/providers/caila/models?refresh=1"]},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(CHAT_EXPECTED))
def test_chat_model_wiring(scenario):
    assert _check(scenario) == CHAT_EXPECTED[scenario]
```

`"keys": [None]` — заглушка: исполнитель вписывает **точный** отсортированный список ключей тела хода, который шлёт страница сегодня (без `model`), снятый с кода до правки, и показывает в отчёте, откуда он взят. Если селектор делегированного клика в `app.js` — не `"button[data-act]"`, исправить строку в моке на точный селектор кода (без `.includes`).

- [ ] **Step 3: Красный** (нет экспортов, `#chat-model-box` не открывается, в теле нет `model`). Вывод — в отчёт.
- [ ] **Step 4: Код** по Interfaces; CSS + строка `modelPickerHtml(...)` в `CLASS_SOURCES` `tests/test_webui_gaps.py`, `model-select`/`model-q` — в `HOOK_CLASSES`, если своего вида нет (с причиной).
- [ ] **Step 5: Мутации:** (а) слать `model` всегда → красный `chat_no_pick_model_sends_no_model`; (б) `localStorage` раньше сессии в `initialModel` → красный `chat_session_model_wins_over_storage` и строки 1 `test_initial_model_order`; (в) `filterModels` без `chosen` → красный длинного списка (нет `m7`); (г) поиск с учётом регистра → красный (`M1` не найдёт `m1`).
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): выбор модели в чате — список из models_allow, группы по вендору, «Обновить», память по сессии и браузеру`.

---

## Task 8 [UI]: выбор модели в «Сюжете» проекта и подпись «кто написал»

**Files:**
- Modify: `h3_48gb/webui/app.js` — `projectScenarioProviderPickHtml` (`app.js:3629`), `loadScenarioProviders` (`app.js:3283`), клик генерации (`app.js:5379`), обработчик `change` `#scenario-provider` (`app.js:5614`) + новые `change`/`input` для `#scenario-model`/`#scenario-model-q`, `data-act="scenario-model-refresh"`; новая `scenarioLlmCaption(proj)`.
- Test: `tests/_model_picker_check.mjs`, `tests/test_webui_model_picker.py`.

**Interfaces:**
- Состояние: `scenarioModelLists = {}` (по имени провайдера), `scenarioModelChoice`. Загрузка списка — в `loadScenarioProviders` и в обработчике смены провайдера/«Обновить», **никогда** из `projectScenarioProviderPickHtml`.
- `projectScenarioProviderPickHtml(proj, busy)` — после кнопки «Проверить» `modelPickerHtml("scenario", {models: scenarioModelLists[choice] || [], query, chosen: scenarioModelChoice, busy})`, только если строка выбранного провайдера `pick_model`.
- Тело `/scenario/generate` LLM-путём: `{provider}` как сейчас плюс `model`, только при `pick_model` и непустом выборе; процедурный путь — без изменений.
- `scenarioLlmCaption(proj)` — `proj.scenario_llm` → `"сюжет написал: " + providerCaption(provider, model)`; `null`/нет — `""`. Выводится в этапе «Сюжет» `<span class="hint scenario-llm">…</span>` только непустым.

- [ ] **Step 1: Тесты**

```python
@_needs_node
def test_scenario_llm_caption():
    assert _js(f"[app.scenarioLlmCaption({{scenario_llm: {{provider: 'caila', model: '{CLAUDE}'}}}}), "
               "app.scenarioLlmCaption({scenario_llm: null}), app.scenarioLlmCaption({})]") == [
        "сюжет написал: caila · claude-opus-5", "", ""]
```

Сценарии в `_model_picker_check.mjs` (`PROJECT`, `clickable` — из харнесса; проект на этапе «Сюжет» — по образцу `_ui_gaps_check.mjs`, клип с утверждённым треком):

```js
// (импорт дополнить: PROJECT, clickable)
const SCENARIO_PROJECT = PROJECT({ kind: "clip", stages: { script: "approved", track: "approved",
  scenario: "draft", scenes: "draft", upscale: "draft", assembly: "draft" },
  track: { duration: 16 }, scenario_scenes: [], scenario_llm: null });
const openProject = async () => {
  fire("click", clickable({ dataset: { act: "open-project", id: "p1" },
                            match: (s) => s === "button[data-act]" }));
  await sleep(120);
};
const generateBodies = () => calls.filter((c) => c.method === "POST"
  && c.url === "/api/projects/p1/scenario/generate").map((c) => c.body);

// SCENARIOS:
async scenario_model_list_fetched_once() {
  await start(appUrl, { "GET /api/providers": ok({ active: "caila", providers: [ROW] }),
    "GET /api/providers/caila/models": ok(LIST), "GET /api/projects/p1": ok(SCENARIO_PROJECT) });
  await openProject();
  fire("change", { id: "scenario-model", value: GPT5, dataset: { id: "p1" } });
  await sleep(80);
  fire("click", clickable({ dataset: { act: "scenario-generate", id: "p1" },
                            match: (s) => s === "button[data-act]" }));
  await sleep(120);
  return { gets: gets(), bodies: generateBodies(),
           picker: getElementById("project-body").innerHTML.match(/<select class="pick model-select" id="scenario-model"[^>]*>/)[0] };
},
```

```python
SCENARIO_EXPECTED = {
    "scenario_model_list_fetched_once": {
        "gets": ["/api/providers/caila/models"],
        "bodies": [{"provider": "caila", "model": GPT5}],
        "picker": '<select class="pick model-select" id="scenario-model" aria-label="модель">'},
}


@_needs_node
@pytest.mark.parametrize("scenario", sorted(SCENARIO_EXPECTED))
def test_scenario_model_wiring(scenario):
    assert _check(scenario) == SCENARIO_EXPECTED[scenario]
```

Имя `data-act` кнопки генерации сверить с кодом (`app.js`, поиск `scenario-procedural`) и вписать точное. Если панель после открытия перерисовывается несколько раз (ответы `withProject`) — `gets` всё равно ровно один: это и есть защищаемое свойство.

- [ ] **Step 2: Красный.** Вывод — в отчёт.
- [ ] **Step 3: Код** по Interfaces; CSS `.scenario-llm` при необходимости; `CLASS_SOURCES` — `projectScenarioProviderPickHtml` с `pick_model`-строкой, если функция экспортирована (иначе — `modelPickerHtml('scenario', …)` уже покрыт задачей 7).
- [ ] **Step 4: Зелёный.**
- [ ] **Step 5: Мутации:** (а) загрузку списка вызвать из `projectScenarioProviderPickHtml` → красный `gets` (два и более); (б) не слать `model` → красный `bodies`; (в) `scenarioLlmCaption` без проверки `null` → красный второй строки.
- [ ] **Step 6: Полный прогон; коммит** — `feat(webui): выбор модели в «Сюжете» проекта и подпись «сюжет написал: провайдер · модель»`.

---

## Самопроверка плана (автор — не ревьюер; сверку со спекой делает другой агент, `CLAUDE.md`)

- Спека §3.1 → задачи 2 (`allowed_prefixes`, `model_allowed`), 4 (`with_model`). §3.2 → 1, 4. §3.3 → 2. §3.4 → 3, 4, 5. §3.5 → 6. §3.6 → 7, 8.
- Заглушка, оставленная сознательно и помеченная: `"keys": [None]` (задача 7, причина — список ключей тела хода снимается с кода после задачи 12, которой ещё нет). Исполнитель заменяет и показывает в отчёте.
- Не проверено до реализации: поведение CAILA на пустой `chat/completions` (шаг 0 задачи 6); блоки в SSE-`delta.content` (задача 5); строгая схема на маршрутах вне `models_allow`.
