# Панель h3, волна 1.6: выбор модели LLM (CAILA и другие OpenAI-совместимые провайдеры)

Дата: 2026-10-08. Статус: мини-спека, утверждена координатором по решениям ниже.
Источник: `~/worktrees/battle/llm-providers/REPORT.md` (§4 «Предложение»), живые замеры CAILA —
`docs/LLM-PROVIDERS.md`. Образец — ai-writer 2.0 (`backend/app/services/provider_service.py`,
`integrations/llm/openai_compatible_client.py`, `frontend/src/pages/ModelsPage/AddModelDialog.jsx`):
переносятся идеи и две чистые функции, не код.

## 1. Цель

Владелец в панели сам выбирает модель внутри провайдера — в чате и в «Сюжете» проекта — из
короткого проверенного списка, без правки `providers.json` на каждую смену модели. Протокол —
OpenAI (`/v1/models`, `/v1/chat/completions`), он уже работает; добавляется только «модель
отдельно от записи провайдера».

## 2. Что есть сейчас (факты, 2026-10-08)

- Модель зашита в запись: `body["model"] = cfg.get("model", ...)` (`h3_48gb/provider.py:648`).
- Список моделей есть только в пробе `test_provider` (`provider.py:844-927`), обрезан до 20 id.
- Ключ лимита и `temperature` задаются в записи вручную (`max_tokens_param`, `send_temperature`,
  `provider.py:635`, `:660`); по умолчанию `max_tokens` + `temperature`.
- Ни сессия чата, ни проект не помнят, какой провайдер и какая модель ответили: в сессии нет
  ключей `provider`/`model` (`web.py:6290-6457`), в `project.json` — тоже (`project.py:117`,
  `:557-572`). Гипотеза отчёта «в мете сценария только провайдер» — неверна: нет ни того, ни другого.
- `test_provider` ловит `HTTPError` веткой `URLError` (`provider.py:899`): 401 на `/v1/models`
  читается как «провайдер недоступен», `reachable: false`.
- CAILA отдаёт `/v1/models` без ключа (648 моделей) — «Проверить» зелёная при неверном ключе.
- `_envelope_to_turn` (`provider.py:684-708`) отдаёт `message.content` как есть; список блоков
  Anthropic уходит в `json.loads(list)` → `TypeError` → повтор → `bad_model_json` (по чтению кода;
  подтверждается красным тестом задачи 5).

## 3. Решения

### 3.1 Включение выбора — только явно

Выбор модели у записи включён, когда **одновременно**: `type == "openai"`, `pick_model: true`,
`models_allow` — непустой список непустых строк (префиксов id). Иначе выбора нет, запись работает
как сегодня. `pick_model: true` без годного `models_allow` — выбора нет (не ошибка запуска).
Разрешена модель, если её id **равен `model` записи** или **начинается с одного из
`models_allow`**. Живой каталог провайдера при проверке не используется (иначе ход зависит от
сети и кэша).

Пример записи (координатор кладёт при выкладке, не код):
```json
"caila": {"type": "openai", "base_url": "https://caila.io/api/adapters/openai",
          "model": "just-ai/anthropic-claude/claude-opus-5", "api_key_env": "CAILA_API_KEY",
          "stream": true, "shares_gpu": false, "pick_model": true,
          "models_allow": ["just-ai/anthropic-claude/", "just-ai/openai-proxy/gpt-5"]}
```

### 3.2 Ключ лимита и temperature по семейству модели (обязательно)

`provider.wire_defaults(model) -> {"max_tokens_param", "send_temperature"}`:

| семейство | признак | ключ лимита | temperature |
|---|---|---|---|
| Anthropic | `"claude"` в id (без учёта регистра) | `max_tokens` | шлётся |
| OpenAI reasoning | `(?:^|[/:])(?:o[1-9]|gpt-5)(?:[-_.][a-z0-9.]+)*$`, без учёта регистра | `max_completion_tokens` | не шлётся |
| прочее | — | `max_tokens` | шлётся |

Регэксп взят из ai-writer и **расширен точкой**: у них `gpt-5.1` не распознаётся как reasoning
(`[-_]` без `.`) — ложно-отрицательный ответ там означает 400 от провайдера.

Правило применения:
- Явные `max_tokens_param`/`send_temperature` записи описывают **модель записи**. Они действуют,
  только пока модель хода равна `cfg["model"]`.
- Если ход идёт на другую модель (выбор из списка), оба явных ключа **снимаются** с копии записи,
  и действуют значения семейства. Иначе запись `gpt-5` с `max_completion_tokens` отправит этот
  ключ Claude, CAILA его проигнорирует, потолок 4096 съест thinking, ответ придёт пустым.
- `_chat_turn` берёт `cfg.get(key, wire_defaults(model)[key])`. Для записей без явных ключей и не
  reasoning-моделей поведение не меняется (`max_tokens` + `temperature`).

### 3.3 Список моделей

`provider.list_models(cfg, env, timeout=10.0, refresh=False) -> {"ok", "detail", "models"}`:
- `type: "openai"` — `GET {base_url}/v1/models` через общий с `test_provider` хелпер
  `_fetch_models` (те же правила: токен только в заголовке, тело ответа в `detail` не попадает).
  `HTTPError` (кроме 502/503/504) — `reachable: true`, «провайдер ответил {code}: {reason}».
- Запись модели: `{"id", "group", "chat"}`. `group` — `modelVendor` в нижнем регистре, иначе
  предпоследний сегмент id по `/`, иначе `""`. `chat` — по `supported_request_types` (если это
  список: есть ли `"CHAT_COMPLETIONS"`), иначе эвристика `_classify_model` из ai-writer
  (подстроки embedding/image/audio → не чат). Дубли id — первый выигрывает; не-словари и записи
  без строкового `id` пропускаются. Сортировка `(group, id)`.
- `llama-local` — `[{"id": model записи, "group": "", "chat": true}]`, без сети.
- Кэш в памяти процесса: ключ `(base_url, api_key_env)`, TTL 600 с по `provider._now`
  (`time.monotonic`), кэшируется только успех; `refresh=True` идёт в сеть и перезаписывает.
  `provider.clear_models_cache()` — для тестов.

### 3.4 API

- `GET /api/providers` — в строку добавляются `model` (строка или `null`) и `pick_model`
  (итоговый bool по §3.1). Секретов по-прежнему нет.
- `GET /api/providers/<name>/models[?refresh=1]` — всегда 200, кроме неизвестного имени
  (`400 args_invalid`). Тело:
  `{"ok", "provider", "model", "pick_model", "detail", "models": [{"id", "group"}]}`.
  - `available: false` — `ok: false`, `detail` = причина записи, `models: []`, **в сеть не ходит**.
  - выбор выключен — `ok: true`, `pick_model: false`, `detail: "выбор модели выключен"`,
    `models` = только модель записи, **в сеть не ходит**.
  - выбор включён — только `chat`-модели, разрешённые §3.1; модель записи присутствует всегда;
    `detail: "{N} из {M} разрешены (models_allow)"`, где M — весь каталог.
  - ошибка каталога — `ok: false`, `detail` из хелпера, `models` = только модель записи.
- `POST /api/chat/<id>/message` и `POST /api/projects/<id>/scenario/generate` принимают
  необязательный `model` (строка; `""`/`null`/отсутствие — модель записи). Проверка до сети и до
  записи сессии: id по `^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$`; выбор у провайдера включён;
  модель разрешена (§3.1). Отказ — `400 args_invalid`, тексты в плане (задача 4). В процедурной
  ветке `/scenario/generate` `model` не читается (как `provider`).
- Запоминание: успешный ход чата пишет `session["provider"]`, `session["model"]` (итоговая
  модель хода); `/scenario/generate` пишет в проект `scenario_llm: {"provider", "model"}` для
  LLM-ветки и `scenario_llm: null` для процедурной. Тело ответа хода не меняется.
- Ответ модели содержимым-списком (`[{"type": "text", "text": ...}, {"type": "thinking", ...}]`)
  склеивается из `text`-блоков до разбора JSON (задача 5).

### 3.5 Честная проба ключа (задача 6, после живой пробы)

После успешного `/v1/models`, если у записи `type: "openai"` и `api_key_env`: `POST
{base_url}/v1/chat/completions` с телом `{"model": <model записи>, "messages": []}` (генерировать
нечего — списания быть не должно). 401/403 — `ok: false`, «ключ не принят: {code} {reason}»;
другой HTTP-код или 200 — ключ принят; сеть/таймаут — «ключ не проверен». Задача делается
только если живая проба координатора (план, задача 6, шаг 0) показала: фальшивый ключ → 401,
настоящий → не 401 и без списания.

### 3.6 UI (после задачи 12 волны 1.5, UI-полоса)

- Чат: рядом с `#chat-provider` — `#chat-model` (`<select>` с `<optgroup>` по `group`, «другие»
  для пустой), кнопка «Обновить» (`?refresh=1`), счётчик «N из M»; поле поиска появляется при
  списке длиннее 12. Показывается только при `pick_model` выбранной строки.
- «Сюжет» проекта: то же внутри разметки `projectScenarioProviderPickHtml`, плюс подпись
  последней генерации из `project.scenario_llm`: «сюжет написал: caila · claude-opus-5».
- Выбор: `session.model` (если `session.provider` совпадает) → `localStorage["h3.model.<provider>"]`
  (в try/catch, если id есть в списке) → `model` записи.
- `model` уходит в тело **только** при `pick_model`; иначе ключа в теле нет (старые тесты тел
  запросов остаются зелёными без правки).
- Список моделей загружается по смене провайдера и по «Обновить», **не из функции разметки**:
  `renderProjectModal` перерисовывает `#project-body` на каждую правку.
- Цену и контекст не показываем: в `/models` CAILA их нет.

## 4. Вне объёма

Ввод ключей в UI, БД профилей, проба возможностей модели, continuation/repair ответа, ретраи,
категории image/audio, автоматическое расширение `models_allow`. Строгая `json_schema` на
маршрутах gemini/deepseek/yandexgpt/gigachat/qwen не проверена — поэтому `models_allow`.

## 5. Риски

1. Модель из `models_allow` не держит строгую схему → честный `bad_provider_reply`/`bad_model_json`
   (`provider.py:740-760`); лечится сужением `models_allow` и записью в `docs/LLM-PROVIDERS.md`.
2. Семейство угадано неверно (новое имя reasoning-модели) → 400 от провайдера с названием
   параметра; лечится явной записью отдельного провайдера под эту модель.
3. Кэш устарел (модель удалили у провайдера) → ход вернёт 4xx; «Обновить» сбрасывает кэш.
4. Проба ключа (§3.5) на шлюзе, проверяющем тело раньше ключа, даёт ложно-зелёное — отсюда шаг 0
   задачи 6 с фальшивым ключом.
