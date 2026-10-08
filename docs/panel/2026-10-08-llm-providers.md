# Выбор модели и CAILA: что брать из ai-writer 2.0 в панель

Дата: 2026-10-08. Только чтение, код ai-writer и панели не менялся. Ключи не читал. На сервере
панели `.env` нет (`/Volumes/home/Outputs/h3-panel/.env` отсутствует). Разбирался только
`ai-writer 2.0`, lab по указанию владельца не трогал.
[факт] — проверено кодом или живым запросом; [гипотеза] — с указанием, чем проверить; [рек.] — рекомендация.

## 0. Суть
1. Протокол у нас уже OpenAI, и CAILA уже работает: `_chat_turn` отправляет `{base_url}/v1/chat/completions`,
   Bearer, строгую `json_schema`, SSE, выбирает ключ лимита. Проверено вживую 2026-08-24
   (`docs/LLM-PROVIDERS.md`; копия у ai-writer: `docs/reports/2026-08-24-caila-structured-output-cross-project.md`).
   Клиент к CAILA из ai-writer брать не нужно: по structured output наш сильнее.
2. Не хватает только выбора модели: модель зашита в запись (`provider.py:647`), а список моделей есть
   лишь в пробе (`test_provider`, до 20 id).
3. Код ai-writer не переносится дословно (FastAPI + httpx + openai SDK + SQLAlchemy/Fernet; React 18 +
   Radix + Tailwind + react-query). Переносимы идеи и 4 чистые функции.
4. Объём порта M, волна 1.6. В задачу 12 по желанию — только имя модели в подписи. Без кода уже сейчас:
   записи CAILA в providers.json.

## 1. Сервер ai-writer
### 1.1 Модель данных [факт]
Provider (name, base_url, token_encrypted, is_active) → ModelProfile (provider_id, model_name,
extra_params_json, detected_capabilities_json, max_tokens, max_concurrent_requests) → привязки агентов.
`services/provider_service.py:35-44`, `db/models/settings.py:28-42`. Ключ вводится в UI и шифруется
Fernet (`provider_service.py:40`), есть SSRF-гард (`:19-28`). Нам не нужно: у нас файл + `.env` по имени
(`provider.py:269-306`).

### 1.2 Список моделей [факт]
- `GET /providers/{id}/models?category=chat|embedding|image|audio` (`api/routes/providers.py:53-73`).
- `GET {base_url}/models` с Bearer; таймаут по умолчанию — LLM-ный, 300 с; из ответа берутся только
  `data[].id`, `sorted(set())` (`provider_service.py:220-259`). Кэша нет.
- Категория — эвристика по подстрокам имени (`_classify_model`, `:303-340`).
- `POST /providers/{id}/test` — тот же `GET /models`, `ok = нет ошибки` (`:291-300`).

### 1.3 CAILA
- Пресет: `https://caila.io/api/adapters/openai`, нужен токен (`frontend/.../providerPresets.js:7-13`). [факт]
- Auth — `Authorization: Bearer`. Без ключа: 401 `mlp.gateway.access_key_required`; с фальшивым ключом:
  401 `mlp.gateway.api_key_not_found` («MLP-API-KEY was not found»). [факт, 2026-10-08]
- `/models` и `/v1/models` — оба 200; `chat/completions` и `/v1/chat/completions` на GET — оба 405,
  то есть маршрут есть. Схема ai-writer (без /v1) и наша (с /v1) работают одинаково. [факт]
- **`/models` открыт без ключа** (200, 648 моделей). Поэтому наш `test_provider` и тест ai-writer
  на CAILA дают «ок» при неверном ключе. [факт]
- Каталог: 648 моделей, имена `just-ai/<маршрут>/...`. Маршруты: openrouter-proxy 467, openai-proxy 95,
  anthropic-claude 20, google-gemini 20, deepseek-deepseek 15, yandex-yandexgpt 15, sber-gigachat 7,
  perplexity 5, плюс 4 одиночных. [факт]
- Свои поля CAILA: `modelVendor` (OPENAI 197, GOOGLE 62, ALIBABA 53, ANTHROPIC 51, DEEPSEEK 30, …) и
  `supported_request_types` (`["CHAT_COMPLETIONS"]` у 137, null у 511). Цены и контекста нет.
  ai-writer эти поля игнорирует. [факт]
- Особенности маршрутов (замерено обоими проектами):
  - Anthropic игнорирует `max_completion_tokens`, слушает только `max_tokens`. Без ключа потолок 4096,
    thinking съедает его, content приходит пустым. ai-writer: `max_tokens_param_key`
    (`openai_compatible_client.py:119-146`); у нас: `max_tokens_param` в записи (`provider.py:627-650`).
  - Reasoning-модели OpenAI (o1–o4, gpt-5*) отвергают `max_tokens` и `temperature`. ai-writer определяет
    их регэкспом (`:107-116`); у нас — вручную в записи (`max_tokens_param`, `send_temperature`).
  - Anthropic на не-потоковом пути может вернуть `message.content` списком блоков
    (`openai_compatible_client.py:224-235`, `integrations/llm/content.py`).
    [гипотеза: наш `_envelope_to_turn` (`provider.py:684-707`) пропустит список и упадёт как bad_model_json;
    проверить юнит-тестом с `content=[{"type":"text","text":"{...}"}]`]
  - Шлюз рвёт долгие ответы: у нас RemoteDisconnected на 7:16, у них обрыв chunked-потока и молчаливая
    обрезка ~10.7k символов (`docs/adr/039-scene-cards.md:65`). У нас лечится `stream: true`,
    у них — continuation.
  - `usage.prompt_tokens` недостоверен (2 при ~9k токенах). Temperature: у ai-writer был инцидент #273
    (отвергалась), на 08-24 принималась.

### 1.4 Запрос в ai-writer [факт] (`integrations/llm/openai_compatible_client.py`, 1292 строки)
- `AsyncOpenAI`, `trust_env=False`, `max_retries=0`, свои ретраи: 429/5xx/таймаут — бэкофф с джиттером;
  400/401/403/404/422 — без ретрая (`:60`, `:648-776`).
- **Строгая json_schema не отправляется**: параметр есть в сигнатуре (`:457`, `:549`), в тело не попадает
  (`docs/BACKLOG.md:985-993`). Для обычных хостов — `json_object`, для caila.io — ничего
  (`:381-385`, `:627-644`); дальше нормализация, repair и continuation.
- Стриминг по флагу (профиль → проба → глобальная настройка, `:653-662`), continuation при обрыве (`:778-916`).
- `tools` — только в пробе (`probe.py:1091`). Проба возможностей — фоновый джоб (`probe.py`, 1469 строк).

### 1.5 Переносимость в stdlib
| Что | Где | Переносимо |
|---|---|---|
| `_classify_model` + паттерны | provider_service.py:303-340 | дословно, ~35 строк |
| `max_tokens_param_key`, `_is_reasoning_model` | openai_compatible_client.py:103-146 | дословно |
| `_fetch_models_from_provider` | provider_service.py:220-259 | идея, у нас уже есть test_provider |
| вырезание `<think>` и fence | openai_compatible_client.py:186-221 | да, если пойдут модели без строгой схемы |
| content-блоки | integrations/llm/content.py | да |
| ретраи, семафоры, continuation, проба | — | нет (SDK/asyncio, нам не нужно) |
| БД, Fernet | — | нет, наш файл + .env лучше |

## 2. Клиент ai-writer
Стек: React 18 + Radix + Tailwind + react-query + lucide. Файлы `frontend/src/pages/ModelsPage/`
(2340 строк, 8 файлов).
- `AddModelDialog.jsx`: селектор провайдера (`:276-300`); автоскан при открытии и смене категории
  (`:115-144`); «Обновить» (`:318-330`); поиск по подстроке id (`:160-167`); чипы категорий и
  «N из M» (`:333-356`); группы по второму сегменту пути, сворачиваются (`groupByPath :49-66`,
  `:373-377`); `prettyName` (`:68-71`).
- Цены и контекста при выборе нет. Цены — статическая list-price-таблица только для дашборда
  (`backend/app/services/model_pricing.py`); контекст — в карточке после пробы.
- Выбор запоминается на сервере (ModelProfile, `is_default`, привязки). localStorage не используется.
- `ConnectProviderDialog.jsx:74-111` проверяет связь fetch'ем из браузера с токеном. На CAILA это
  ложно-зелёно, и ключ уходит из браузера — нам не подходит.
- Поправка для CAILA: 467 из 648 моделей лежат под `openrouter-proxy`, и `groupByPath` сделает из них
  одну группу. Группировать лучше по `modelVendor`, с запасным вариантом по пути.
- Берём в ванильный JS: поиск, `<optgroup>` по вендору, «N из M», «Обновить».

## 3. Сравнение
| | Панель | ai-writer |
|---|---|---|
| Протокол | OpenAI, urllib (`provider.py:708-712`) | OpenAI SDK |
| Конфиг/ключи | providers.json + .env по `api_key_env` (`:283-306`) | БД + Fernet |
| Строгая json_schema | да (`:651`) | нет |
| Ключ лимита по семейству | вручную | автоматически |
| Temperature у reasoning-моделей | вручную (`send_temperature`) | автоматически |
| Поток против обрыва шлюза | `stream: true` (`:663-678`, `_read_sse`) | stream + continuation |
| Обрезка ≠ нарушение схемы | `chat_truncated` / `bad_model_json` | continuation / repair |
| Тест связи | `/api/providers/<n>/test` → `/v1/models`, ≤20 id (`provider.py:844-927`, `web.py:5705-5741`) | `/models` |
| **Полный список моделей** | **нет** | есть + категории |
| **Модель отдельно от провайдера** | **нет** (`provider.py:647`) | да |
| UI провайдера | `#chat-provider` (`app.js:3948-3973`), `#scenario-provider` + «Проверить» (`app.js:3243-3257`) | Radix |
| Поиск и группы моделей | нет | есть |
| Content-блоки Anthropic | [гипотеза] не обработаны | обработаны |
Сервер: providers.json содержит только `qwen-alex-neuro` (openai, 127.0.0.1:8000, без ключа); `.env` нет.

## 4. Предложение
### 4.0 Сразу, без кода [рек.]
Создать `/Volumes/home/Outputs/h3-panel/.env` с `CAILA_API_KEY` (права 600) и добавить записи:
  "caila-opus5": {"type":"openai","base_url":"https://caila.io/api/adapters/openai",
    "model":"just-ai/anthropic-claude/claude-opus-5","api_key_env":"CAILA_API_KEY",
    "stream":true,"shares_gpu":false}
  "caila-gpt5": {... "model":"just-ai/openai-proxy/gpt-5", "max_tokens_param":"max_completion_tokens",
    "send_temperature":false, "stream":true, "shares_gpu":false}
`base_url` пишется без `/v1`.

### 4.1 Сервер
1. `provider.list_models(cfg, env, timeout=10)`: `GET /v1/models` (общий хелпер с test_provider,
   те же правила редактирования ошибок) → `{ok, detail, models:[{id, group, chat}]}`.
   `group` = modelVendor, иначе сегмент пути. `chat` = по `supported_request_types`, иначе `_classify_model`.
   Для llama-local — `[cfg.model]`. Кэш в памяти на 10 мин, `?refresh=1`.
2. Маршрут `GET /api/providers/<name>/models` (рядом с `web.py:3531`). Всегда 200; неизвестное имя —
   args_invalid; при `available=False` в сеть не ходит.
3. В строку `/api/providers` (`web.py:5692-5703`) добавить `model` и `pick_model`.
4. Флаг записи `pick_model` (по умолчанию false); опционально `models_filter: "chat"` и
   `models_allow` (список префиксов).
5. Поле `model` в теле `POST /api/chat/<id>/message` (`web.py:6293`) и `/scenario/generate` (`web.py:5183`).
   Проверки: строка 1..200, `pick_model`, `models_allow`. Затем `cfg={**cfg,"model":m}`.
   С живым списком не сверять.
6. Обязательно вместе с п.5: если в записи ключ лимита не задан явно — claude → `max_tokens`;
   o1–o4/gpt-5* → `max_completion_tokens` без temperature; прочее → `max_tokens`.
7. Запоминание: `session["model"]` (рядом с `web.py:6290`); provider и model — в мету сценария.
   [гипотеза: сейчас там только провайдер — проверить]
8. Честный тест ключа для CAILA: после `/v1/models` отправить кривой `POST /v1/chat/completions`
   и различать 401 и не-401. [гипотеза: с верным ключом будет 400/404 без списания — проверить одним вызовом]
9. Нормализовать content-список в `_envelope_to_turn` — если гипотеза из 1.3 подтвердится.

### 4.2 UI
1. Рядом с `#chat-provider` (`index.html:450`) и `#scenario-provider` — поле модели, только при
   `pick_model`. Предлагаю `<input type=search>` + `<select size=8>` с `<optgroup>` по вендору и
   «N из M» (у datalist нет групп).
2. «Обновить» → `?refresh=1`.
3. `localStorage["h3.model.<provider>"]` в try/catch, как тема (`app.js:2449-2474`).
4. Подпись: «caila · claude-opus-5».
5. Цену и контекст не показывать: в `/models` CAILA их нет.

### 4.3 Оценка
список моделей + кэш + маршрут + поля ростера — S; переопределение модели + семейство + запоминание — S–M;
UI в двух местах — M; тест ключа и content-блоки — по S. Итого M (≈1–1.5 дня с тестами по правилу
«увидеть красное»).

### 4.4 Риски
1. Строгая json_schema на разных маршрутах CAILA. ai-writer риск обходит: для CAILA response_format
   не шлёт, достаёт JSON repair'ом. Что известно: Anthropic-маршрут строгость не применяет;
   OpenAI-строгий через openrouter-proxy отвергал схему, пока не все свойства были в `required` —
   у нас уже исправлено (`provider.py:161-207`). Не проверены gemini, deepseek, yandexgpt, gigachat, qwen.
   [рек.] `models_allow` с коротким проверенным списком; живая проба схемой при добавлении маршрута
   и запись в LLM-PROVIDERS.md. 400 у нас приходит честно, как bad_provider_reply (`provider.py:740-760`).
2. Ключ лимита и temperature при выборе модели в рантайме (п.4.1.6): без этого gpt-5 упадёт с 400.
3. Ложно-зелёное «Проверить» на CAILA при неверном ключе.
4. Таймаут шлюза: флаг `stream` остаётся на записи провайдера, и это верно — это свойство шлюза.
5. Без кэша каждое открытие модалки — внешний запрос примерно на 650 записей.

### 4.5 Волна
В 1.5 не вставлять: задача 12 правит тот же селектор, будет конфликт правок. Допустимо в 12, по решению
координатора: имя модели в подписи (`model` в `_providers`). Волна 1.6 — пп. 4.1.1–4.1.7 и 4.2, затем
4.1.8–4.1.9 после проверки гипотез. Перед реализацией план сверяет со спекой не его автор.
Пункт 4.0 — сейчас.

## 5. Не проверено
- Ответ CAILA с настоящим ключом на кривое тело (4.1.8).
- Content-список в `_envelope_to_turn` (1.3).
- Строгая схема на gemini, deepseek, yandexgpt, gigachat, qwen.
- Есть ли модель в мете сценария сейчас.
- Живая генерация не запускалась; сетевые проверки — только без ключа.
