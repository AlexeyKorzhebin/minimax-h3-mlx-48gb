# Чат и выбор LLM-провайдера в ai-writer 2.0: что есть и что годится в общий модуль

Дата: 2026-10-23. Только чтение: код ai-writer, его база и серверы не трогались, ключи не читались.
Контекст: владелец хочет сделать чат и форму провайдеров **отдельным подключаемым модулем** (сначала студия
и ai-writer), с дизайном хозяина и работой на телефоне. Перенос отложен до исследования упаковки и
темизации `docs/research/2026-10-23-reusable-llm-chat-components.md`. Этот документ — только разбор
ai-writer как исходника. Плана переноса в студию здесь нет.

Пометки: [факт] — проверено по коду (`файл:строка`); [гипотеза] — с указанием, чем проверить; [оценка] — суждение автора.
Пути — относительно `/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/ai-writer 2.0/`.

## 0. Итог

1. **Общего чата в ai-writer нет.** Есть один презентационный компонент диалога `ConversationStudio` (228 строк)
   и шесть доменных обёрток над ним (клип, иллюстрация, концепт, персонаж, локации, конституция). У каждой
   обёртки свой эндпоинт `…/refine` и своя история в JSON-поле сущности. **Стриминга нет нигде**: ход
   синхронный, сообщение «отправляется…» показывается оптимистично. [факт]
2. Форма провайдеров — `frontend/src/pages/ModelsPage/` (8 файлов, ≈2340 строк), модель в два уровня:
   «провайдер → профиль модели». Ключ хранится в БД под Fernet. «Быстрая смена» — `layouts/ActiveModelBadge.jsx`:
   чип в шапке. На странице проекта он закрепляет модель за проектом, вне проекта меняет глобальную модель по умолчанию. [факт]
3. Исходником для общего модуля годятся: `ConversationStudio` почти целиком; пресеты; форма провайдера
   (её поля и режим «ключ только запись»); семантика чипа «проект → по умолчанию»; контракт эндпоинтов провайдеров.
   Не годятся: проверка связи из браузера, маска ключа, модель данных в два уровня с профилями и привязками агентов,
   палитра, связка TanStack Query + axios внутри компонентов. [оценка]
4. Чтобы компоненты стали переиспользуемыми, их придётся отвязать от `shared/ui/*` (примитивы ai-writer), от
   своих токенов (`fg-*`, `accent-*`, `border-default`), от хуков `entities/*` и от `apiClient`. Форма и чип
   вместе — 18 строк импорта внутренних модулей и библиотек ai-writer. [факт]

## 1. Стек [факт]

| Слой | Что | Где |
|---|---|---|
| Фронт | React 19.3, **JS/JSX (не TS)**, Vite 8, Tailwind v4 (`@tailwindcss/postcss`), Radix отдельными пакетами (`@radix-ui/react-dialog`, `-dropdown-menu`, `-select`, …), `class-variance-authority`, `clsx`, `tailwind-merge`, `lucide-react`, TanStack Query 5, axios, zod 4, sonner, next-themes | `frontend/package.json:16-40` |
| Примитивы | свои shadcn-подобные компоненты: Button на cva, Dialog, DropdownMenu, Input/Textarea, FormField, IconButton | `frontend/src/shared/ui/` |
| Токены | «Modern Studio»: тёплый янтарный акцент, фон `#FAFAF7`, Inter + JetBrains Mono, тёмная тема через `class` | `frontend/src/styles/foundations.css:1-30` |
| Бэкенд | FastAPI, SQLAlchemy (async), Alembic, httpx, OpenAI SDK, cryptography (Fernet) | `backend/app/…` |
| Тесты фронта | Vitest + Testing Library; e2e — Playwright | `frontend/vitest.config.js`, `frontend/tests/` |
| Лицензия | файла нет; «лицензия фиксируется при первом релизе», проект того же владельца, single-user | `README.md:138-140` |

## 2. Чат [факт]

| Файл | Строк | Что делает |
|---|---|---|
| `frontend/src/features/conversation-studio/ConversationStudio.jsx` | 228 | лента `turns[{idx, role, content, attachments, kind, options, rewindable}]`; оптимистичный ход `pending`, который снимается, когда приходит ответ или запрос кончается (`:37-50`); автопрокрутка (`:41-43`); отправка по Cmd/Ctrl+Enter, Enter — перенос строки (`:68-73`); вложения перетаскиванием и вставкой (`:85-94`); кнопки-варианты для `kind:"question"` (`:150-158`); «вернуться к этому ходу» (`fromIdx`, `:160-165`); слот хозяина `renderAssistantExtra(turn)` (`:166`); кнопки «Очистить», «Отмена», «Отправить» (`:199-224`); `seedMessage` подставляет текст в поле (`:52-55`) |
| `…/AttachmentInput.jsx`, `AttachmentChip.jsx`, `attachmentUtils.js` | 38 + 52 + 30 | вложения: картинки (если `visionSupported`) и txt/md/json/pdf/docx до 5 МБ; классификация по MIME и расширению |
| `features/clip-studio/ClipConversation.jsx` (пример хозяина) | 327 | `POST /clips/{id}/refine {message, from_idx}` синхронно, с `If-Match`; история — `clip.dialog_json`; сброс — `POST /clips/{id}/dialog/reset` (`backend/app/api/routes/clips.py:110-145`); тело запроса — `ClipRefineRequest` (`backend/app/schemas/clip.py:56-66`) |
| Остальные хозяева | — | `features/illustration-studio/IllustrationConversation.jsx`, `concept-studio/ConceptStudio.jsx`, `edit-character/CharacterEditor.jsx`, `edit-locations/LocationsTree.jsx`, `constitution-presets/…`, `widgets/RegisterConstitutionPanel/…` |

Стриминга нет: в `backend/app` и `frontend/src` нет `StreamingResponse` и `EventSource`. Единственный
`stream=True` — загрузка референсов (`backend/app/services/reference_import_service.py:656`).
Отмена (`onCancel`) на стороне хозяина: в `ClipConversation` это `abortRef` + `AbortController` (`:60, 300, 308`).

Тесты: `frontend/tests/unit/ConversationStudio.test.jsx` — 7 тестов (лента и варианты, клик по варианту,
onSend с `message/attachments/fromIdx`, без скрепки, пустая отправка, кнопка неактивна без текста, автопрокрутка);
`ClipConversation.test.jsx` — 10; `AttachmentChip`, `AttachmentInput`, `attachmentUtils` — свои файлы.

## 3. Провайдеры и выбор модели [факт]

### 3.1 UI
| Файл | Строк | Что делает |
|---|---|---|
| `pages/ModelsPage/providerPresets.js` | 63 | 8 пресетов `{id, name, base_url, needsToken, hint}`: CAILA, OpenAI, Anthropic, OpenRouter, llama.cpp, Ollama, LMStudio, свой |
| `pages/ModelsPage/ConnectProviderDialog.jsx` | 309 | сетка пресетов → имя → base_url → токен (`type=password`; при правке «пусто = не менять», `:268-284`); «Test connection» **делает `fetch` из браузера** на `{base_url}/models` с `Authorization: Bearer <токен>` (`:74-111`) |
| `pages/ModelsPage/ProvidersSidebar.jsx` | 282 | список; «облако/локальный» по подстроке URL (`:31-41`); точка здоровья за 24 ч (`HealthDot`) |
| `pages/ModelsPage/AddModelDialog.jsx` | 483 | список моделей провайдера, поиск по подстроке, чипы категорий, «Обновить» |
| `pages/ModelsPage/ModelCard.jsx`, `ModelsPage.jsx`, `ModelTierDefaultsSection.jsx`, `DeleteProviderDialog.jsx` | 659 + 253 + 167 + 124 | карточка профиля (проба возможностей, калибровка), страница, уровни моделей по умолчанию, удаление с каскадом |
| `layouts/ActiveModelBadge.jsx` (смонтирован в `layouts/TopNav.jsx`) | 241 | чип быстрой смены. На проекте — `PATCH project.default_model_profile_id` (через `usePatchProject` с `expectedVersion`), вне проекта — `POST /model-profiles/{id}/set-default` (`:47-133`). Бейдж «проектная/дефолт», иконка vision, точка активных LLM-запросов (опрос `/workflow-runs/active` раз в 2,5 с, `:57-64`) |
| `entities/model/api.js`, `entities/modelProfile/api.js` | 298 + 93 | хуки TanStack Query поверх `apiClient`: провайдеры, модели, профили, проба |

Тесты UI провайдеров: `AddModelDialogExam.test.jsx`, `ModelCardProbe.test.jsx`, `ModelTierDefaultsSection.test.jsx`.
Отдельных тестов на `ConnectProviderDialog`, `ProvidersSidebar` и `ActiveModelBadge` нет.

### 3.2 API и хранение
- Эндпоинты `backend/app/api/routes/providers.py:30-160`: `GET /providers`, `POST /providers`,
  `GET/PATCH/DELETE /providers/{id}` (удаление с `?confirm_cascade=true`), `GET /providers/{id}/models?category=chat|embedding|image|audio`,
  `GET /providers/{id}/health` (сводка за 24 ч), `POST /providers/{id}/test`.
- Схемы `backend/app/schemas/settings.py:33-95`: `ProviderCreateRequest{name, base_url: HttpUrl, token?, description?}`,
  `ProviderPatchRequest` (`extra="forbid"`), `ProviderResponse{…, token_masked}`, `ProviderTestResponse{ok, detail, detected_models}`.
- Проверка связи на сервере = `GET {base_url}/models` (`backend/app/services/provider_service.py:292-301`);
  категория модели — эвристика по подстрокам имени (`:304-341`).
- Ключ: Fernet с префиксом `fernet:v1:`; без `TOKEN_ENCRYPTION_KEY` ключ хранится открытым текстом (режим legacy) (`backend/app/core/secrets.py:20-60`).
  Наружу уходит маска `token[:3]***token[-2:]` (`schemas/settings.py:25-30, 76`).
- SSRF-гард по литеральному хосту, без DNS (`backend/app/core/network_guard.py`).
- Модель данных: `Provider` → `ModelProfile` (модель, параметры, найденные возможности, лимит параллельности) →
  привязки агентов (`backend/app/db/models/settings.py:18`).
- Тесты бэкенда: `backend/tests/contract/test_providers_api.py`, `unit/test_provider_credentials.py`,
  `unit/test_secret_masking.py`, `unit/test_network_guard.py`, `integration/test_ssrf_provider_guard.py`.

## 4. Что годится как исходник общего модуля

| Исходник | Что берётся | Что нужно сделать, чтобы стало общим |
|---|---|---|
| `ConversationStudio.jsx` | лента, оптимистичный ход, автопрокрутка, горячая клавиша, «Отмена», «Очистить», слот хозяина под ответ ассистента | перевести на TS; заменить `shared/ui` на примитивы модуля или хозяина; заменить токены на семантические переменные хозяина; переименовать слот в общий (например, `renderOffer`); вложения и `fromIdx` сделать необязательными расширениями |
| `attachmentUtils.js` | классификация и лимит файлов | чистая функция, переносится как есть |
| `providerPresets.js` | список пресетов и подсказки | вынести в данные, которые хозяин может сузить (например, оставить только OpenAI-совместимые) |
| `ConnectProviderDialog.jsx` | поля, сценарий «пресет → имя → URL → ключ», режим «пусто = не менять» | проверку связи — только через сервер; добавить поле «модель» для хозяев без профилей; убрать прямые хуки `entities/model` |
| `ActiveModelBadge.jsx` | семантика «закреплено за контекстом → по умолчанию», бейдж области, недоступные пункты | контекст (проект) передаёт хозяин, а не `useMatch("/projects/:id")`; опрос активных запросов заменить сигналом хозяина |
| `AddModelDialog.jsx` (поиск по списку) | поиск, «Обновить», счётчик «N из M» | группировка: по `modelVendor` CAILA, а не по сегменту пути (467 из 648 моделей CAILA лежат под `openrouter-proxy`, `docs/panel/2026-10-08-llm-providers.md` §1.3) |
| Контракт `routes/providers.py` | набор операций: список, создать, изменить, удалить, модели, проверить | общий роутер с хранилищем, которое задаёт хозяин: у ai-writer — БД + Fernet, у других хозяев может быть файл |
| Тесты `ConversationStudio.test.jsx` | сценарии | переписать на точные сравнения (в этом репо мок с `.includes()` трижды пропускал мутации, память проекта) |

## 5. Что завязано на ai-writer

| Завязка | Где | Почему мешает выделению |
|---|---|---|
| Примитивы и токены ai-writer | `shared/ui/*`, `foundations.css` | янтарь и кремовый фон — чужой визуальный язык для других хозяев (в студии они прямо запрещены, спека §4.4) |
| TanStack Query + axios внутри компонентов | `entities/model/api.js`, `ActiveModelBadge.jsx:1-70` | хозяин без react-query (студия: свой `api()` и SSE) не подключит компонент без обёртки |
| Маршрутизация | `useMatch("/projects/:projectId/*")`, `Link to="/settings/models"` (`ActiveModelBadge.jsx:1, 47, 135, 233`) | react-router; у других хозяев маршрутизация своя (в студии — хеш) |
| Два уровня: провайдер → профиль → привязки агентов | `db/models/settings.py`, `ModelCard.jsx`, `ModelTierDefaultsSection.jsx` | специфика многоагентного конвейера ai-writer; простому хозяину достаточно «провайдер + модель» |
| История диалога в JSON-поле доменной сущности, свой эндпоинт `refine` на каждый вид | `clip.dialog_json`, `routes/clips.py:110-145` и аналоги | общего хранилища и общего API тредов нет; модулю придётся ввести их самому |
| Проба возможностей, калибровка, здоровье за 24 ч | `ModelCard.jsx`, `probe.py` (1469 строк), `/health` | тяжёлые фоновые задачи ai-writer; общему модулю не нужны |
| Ключ в БД под Fernet | `core/secrets.py` | хорошо для ai-writer, но это реализация хранилища, а не часть модуля |
| Версионирование `If-Match`/`expectedVersion` | `ClipConversation`, `usePatchProject` | договор конкретного хозяина; модуль должен принимать его как параметр |

## 6. Находки, которые важны для любого хозяина

1. **Проверка связи из браузера** (`ConnectProviderDialog.jsx:74-111`): ключ уходит из браузера на чужой адрес.
   Мешает CORS. На CAILA проверка ложно-зелёная: `/models` открыт без ключа (проверено 08.10, `docs/panel/2026-10-08-llm-providers.md` §1.3).
   В общем модуле — только серверная проверка, и не одним `/models` (например, однотокенный `chat/completions`). [факт / рек.]
2. **Маска ключа** отдаёт пять символов ключа (`schemas/settings.py:25-30`). В общем модуле — только `key_set: bool`. [факт / рек.]
3. **Без `TOKEN_ENCRYPTION_KEY` ключ лежит в БД открытым текстом** (режим legacy, `core/secrets.py:59-61`). [факт]
4. **Клиент LLM ai-writer не шлёт строгую `json_schema`**: только `json_object` или ничего для CAILA, дальше
   ремонт ответа (`docs/panel/2026-10-08-llm-providers.md` §1.4). Для хозяев со строгой схемой (студия) его клиент брать не стоит. [факт из разбора 08.10]
5. **Стриминга в чате нет** — значит, переиспользовать нечего, но и обязательств нет. Если модулю понадобится стрим, это новая работа. [факт]
6. **Опрос активных запросов раз в 2,5 с** в чипе (`ActiveModelBadge.jsx:57-64`) — вне ai-writer нужен сигнал хозяина (у студии — SSE). [факт]

## 7. Не проверено

- [гипотеза] Как ведут себя `ConversationStudio` и `ConnectProviderDialog` на ширине 390 px. Проверить: Playwright-скриншот страниц ai-writer (сервер не запускался — там параллельно работает другая сессия).
- Однотокенный `chat/completions` на CAILA с неверным ключом даёт 401: ожидается, живьём не проверено.
- Схема упаковки и темизации — у параллельного исследования `docs/research/2026-10-23-reusable-llm-chat-components.md`.
