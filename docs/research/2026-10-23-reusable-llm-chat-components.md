# Чат с LLM и выбор провайдера как переиспользуемые компоненты: упаковка, темизация, мобильность

Дата: 2026-10-23. Это исследование, код в проекты не писался. ai-writer 2.0 только читался.
Входные данные: `docs/research/2026-10-23-ai-writer-chat-port.md` (дальше «разбор переноса»): что есть в
ai-writer и какие решения Р1–Р9 приняты для студии. Здесь этот разбор не повторяется: документ отвечает
на вопрос, **как упаковать** чат и форму провайдеров, чтобы их подключали студия, ai-writer и следующие
проекты владельца.

Пометки: **[факт]** — проверено по коду (`файл:строка`), npm или документации (ссылка в конце);
**[гипотеза]** — не проверено, указано, чем проверить; **[рек.]** — рекомендация.
Пути студии даны относительно `/Users/aleksey.korzhebin/worktrees/studio-s2/`, пути ai-writer — относительно
`/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/ai-writer 2.0/`.

## 0. Итог

1. **Рекомендация: свой небольшой набор `llm-kit`. Готовую чат-библиотеку не берём.** Набор раздаётся
   **исходниками**: фронт — через свой shadcn-registry, сервер — вендорной копией Python-пакета.
   Фронт делится на два слоя:
   - headless-ядро: клиент API, хуки и машина состояний хода, без разметки;
   - тонкий стилизованный слой: `ChatPanel`, `ChatSheet`, `ProviderChip`, `ProviderForm`, `ProviderList`.
     Он написан **только на семантических токенах shadcn** (`bg-background`, `text-muted-foreground`,
     `bg-primary`, `border-border`, `ring`…) и на shadcn-примитивах хозяина (`Button`, `Textarea`, `Sheet`…).
   Сервер — FastAPI-роутер ядра. Хранилище, вызов LLM, гейт GPU и формат ошибок он берёт у адаптера хозяина.
2. **Почему компонент выглядит «как родной» без форка.** Студия уже связала переменные shadcn с токенами AB:
   `--color-primary: var(--cobalt)`, `--color-border: var(--line)` и т. д. (`h3_48gb/studio/web/src/styles.css:55-72`). [факт]
   Компонент из registry использует кнопку и поле самого хозяина, поэтому сразу получает таблетки-кнопки
   AB, кобальт и тёмную тему. В ai-writer переменных shadcn нет, у него свои токены `canvas/fg-*/accent-*`
   (`frontend/src/styles/index.css:22-50`) [факт]. Чтобы подключить туда набор, нужен **мост токенов
   на ~20 строк CSS** (§1.3) и `components.json`.
3. **Готовые библиотеки как база не подходят.**
   - assistant-ui ближе всех (MIT, shadcn-стиль, свой рантайм для любого бэкенда). Но это 0.x с релизом
     каждые 2–3 дня (7 версий за 06–22.10.2026) и 2,8 МБ распакованного кода — ради чата без стриминга,
     веток и вложений.
   - Vercel `useChat` и AI Elements завязаны на протокол сообщений AI SDK. Python-серверу пришлось бы его эмулировать.
   - CopilotKit требует своего рантайма.
   - deep-chat — web component со своим оформлением.
   - chatscope не развивается с 05.2025.
   Подробности в §3.
   Берём **точечно**: `use-stick-to-bottom` (прокрутка при стриминге, MIT, 33 КБ), разметку и приёмы
   shadcn chat components (`message`, `bubble`, июнь 2026) и, когда понадобится, `streamdown` для markdown в ответах.
4. **Объём — ≈4,7 дня**, это вместо 3 дней из разбора переноса, а не сверх них. Сверх переноса добавляются
   ≈1,7 дня: отделить ядро от адаптера, собрать registry, вендорить сервер, написать мост токенов и
   подключить ai-writer. Сам перенос в студию остаётся срезом S2c. Подключение ai-writer — отдельно,
   ≈1 день после того, как его сессия освободит фронт.
5. **Главные риски.**
   - Копии исходников расходятся с набором, если хозяин правит их руками. Меры: правило «скопированное не
     править, менять через слоты и токены» и тест-сверка контрольных сумм (§6.5).
   - На iOS клавиатура и `position: fixed` (§5) — проверять на живом телефоне.
   - Поля студии набраны 13,5px, iOS Safari на фокусе увеличивает страницу (§5.3).

## 1. Темизация: что реально даёт «выглядит как родное»

### 1.1 Варианты

| Подход | Что даёт | Чего не даёт | Вывод |
|---|---|---|---|
| Headless-логика (хуки, машина состояний) без разметки | хозяин рисует как хочет, логика общая | каждый хозяин пишет разметку сам, то есть три копии ленты, композера и формы | основа, но одной её мало |
| CSS-переменные набора (`--llm-*`) с маппингом на токены хозяина | цвет и радиусы подстраиваются | кнопки, поля и фокус остаются чужими: форма другая, высота другая, нет таблеток AB | только как дополнение для 2–3 своих мест (пузыри) |
| Компоненты набора **на shadcn-примитивах хозяина** (`@/components/ui/button` и т. п.) и семантических токенах shadcn | кнопка в чате — та же кнопка, что везде в приложении: AB-таблетка с градиентом, фокус-кольцо кобальтом, тёмная тема по `[data-theme]` | нужно, чтобы у хозяина были shadcn-примитивы и переменные `--color-primary/...` | **главный механизм** |
| Слоты, render-props, подмена компонентов | доменные вставки: кнопка «Применить» у предложения, подпись провайдера, пустое состояние | — | обязательны для домена (Р6 разбора переноса: `renderOffer`) |
| Shadow DOM или web component | изоляция | изолирует и от дизайна хозяина: тянуть токены можно только через `::part` и переменные | только для хозяев не на React (сейчас таких нет) |

### 1.2 Решение [рек.]

- **Ядро** (`llmkit/core`): типы, клиент, хуки `useChat`, `useProviders`, `useProviderChoice`, `useVisualViewport`.
  Без JSX-разметки и без классов.
- **Стилизованный слой** использует **только** утилиты на семантических токенах shadcn: `bg-background`,
  `bg-muted`, `text-foreground`, `text-muted-foreground`, `bg-primary text-primary-foreground`, `border-border`,
  `border-input`, `ring-ring`, `text-destructive`, `rounded-md/lg`. Захардкоженных цветов и `bg-blue-500` нет.
  Это проверяет линтер-тест набора: grep по registry-файлам на палитровые классы Tailwind и `#hex`.
- **Свои переменные** — только там, где у shadcn нет семантики, всегда с запасным значением из shadcn:
  ```css
  --llm-bubble-user:      var(--color-muted);
  --llm-bubble-assistant: transparent;
  --llm-bubble-radius:    var(--radius-lg);
  --llm-composer-font:    16px;           /* iOS: < 16px увеличивает страницу на фокусе, §5.3 */
  ```
  Хозяин переопределяет их в своём `@theme`/`:root` при желании. В студии AB, например:
  `--llm-bubble-user: var(--cobalt-soft)`.
- **Слоты** (props): `renderMessageExtra(message)` для «Применить» у `offer`, `renderEmpty()`,
  `renderProviderBadge(provider)`, `headerActions`. Пропсами задаются тексты (`labels`, русские по
  умолчанию) и `locale` для времени.
- Примитивы импортируются по **алиасам хозяина** (`@/components/ui/button`). При установке shadcn CLI
  переписывает пути импорта под `components.json` хозяина [факт по документации shadcn, механизм `aliases`].

### 1.3 Мост токенов для хозяина без shadcn-переменных (ai-writer) [рек.]

У ai-writer есть `--color-canvas`, `--color-fg-primary`, `--color-accent-500`, `--color-border-default`…
(`frontend/src/styles/index.css:22-50`) [факт], а `--color-primary`, `--color-background`, `--color-muted-foreground` нет.
Мост добавляется в `@theme inline` хозяина:
```css
@theme inline {
  --color-background: var(--color-canvas);
  --color-foreground: var(--color-fg-primary);
  --color-muted: var(--color-subtle);
  --color-muted-foreground: var(--color-fg-muted);
  --color-primary: var(--color-accent-500);
  --color-primary-foreground: var(--color-accent-fg);
  --color-secondary: var(--color-subtle);
  --color-secondary-foreground: var(--color-fg-primary);
  --color-accent-foreground: var(--color-fg-primary);  /* --color-accent у ai-writer уже занят: accent-500 */
  --color-destructive: var(--color-danger);
  --color-border: var(--color-border-default);
  --color-input: var(--color-border-strong);
  --color-ring: var(--color-border-focus);
}
```
**Конфликт [факт]:** `--color-accent` в ai-writer означает акцентный цвет (`index.css:45`), а в shadcn —
фон наведения у пунктов меню. Если registry-компоненты используют `bg-accent` для hover, в ai-writer
они подсветятся янтарём. [рек.] В слое набора не использовать `bg-accent` и брать `bg-muted`.
Тогда мост не трогает `--color-accent` хозяина.

Примитивы ai-writer лежат в `frontend/src/shared/ui/Button.jsx` и т. п. (с заглавной буквы, JSX, алиаса
`@/` нет) [факт]. Для registry есть два пути: (а) поставить shadcn-примитивы рядом (`src/components/ui/button.jsx`)
и связать их с токенами через мост — кнопки чата тогда будут shadcn-кнопками в палитре ai-writer, но не его
`Button` с `isLoading`; (б) в `components.json` направить алиас `ui` на `src/shared/ui` — но имена файлов и
API (`isLoading`, `variant="quiet"`) не совпадают с shadcn, установка сломается. **Рекомендуется (а).**
Визуально это близко к родному, полностью родным не будет. Это решает владелец (вопрос 3).

## 2. Упаковка и доставка

| Вариант | Как подключается | Обновление | Дизайн хозяина | Сборка студии (Docker) | Вывод |
|---|---|---|---|---|---|
| npm-пакет, GitHub Packages | `npm i @owner/llm-kit`, `.npmrc` с токеном | `npm update` с semver | только через CSS-переменные: компоненты собраны со своими примитивами, кнопки не «родные» | фронт собирается на хосте до Docker (`Dockerfile.studio` проверяет `web/dist`), токен нужен только локально | чужие кнопки; токен реестра на каждой машине |
| npm, git-зависимость | `"github:owner/llm-kit#v0.1.0"` | смена тега | то же | — | **npm не умеет подкаталог монорепо** в git-зависимости (pnpm умеет `&path:`), а оба хозяина на npm (`package-lock.json`) [факт]. Пришлось бы держать отдельный репо на каждый пакет |
| git-подмодуль | `git submodule add`, алиас в Vite | `git submodule update` | исходники собираются в хозяине; с алиасами `@/components/ui` они берут примитивы хозяина | Docker копирует подмодуль с контекстом, `.git` подмодуля — отдельная боль | рабочий, но подмодули в Яндекс.Диске и двух worktree — риск путаницы; правка из хозяина уходит в чужой репо |
| **shadcn-registry** (свой, файлы JSON) | `npx shadcn@latest add <url или ./path>/llm-chat-panel.json`; приватный — namespace `@llmkit` с `Authorization: Bearer ${TOKEN}` в `components.json` [факт, документация shadcn] | `shadcn add --overwrite` нужной версии и просмотр `git diff` в хозяине | **полный**: файлы ложатся в хозяина, импортируют его `Button/Textarea/Sheet`, пути переписываются под его алиасы | нет внешней зависимости на рантайме, Docker не меняется | **выбран** для фронта |
| web components (Lit, deep-chat) | `<llm-chat>` в любой странице | npm | только через `::part` и переменные, Shadow DOM | — | только для хозяина не на React (сейчас нет, YAGNI) |

Почему registry подходит именно здесь:
- Оба хозяина — React 19.3, Tailwind 4.3, Radix [факт: `h3_48gb/studio/web/package.json`, `frontend/package.json:16-40` ai-writer].
- В студии уже shadcn@4.22.1, `components.json` с `cssVariables: true` и алиасами `@/components/ui` [факт: `h3_48gb/studio/web/components.json`].
- Установка из **локального файла** поддерживается (`npx shadcn add ./registry/x.json`) [факт, документация].
  Обе кодовые базы живут на одном Маке, публиковать registry в сеть для первых двух хозяев не нужно.
  Достаточно репозитория набора и `shadcn build`, который собирает `public/r/*.json` из `registry.json`.
- Registry-элемент типа `registry:lib` (ядро) ставится тем же механизмом. Поэтому **ядро тоже копируется**
  (`src/lib/llmkit/*.ts`), и отдельный npm-пакет не нужен.

Минус registry — обновление. В хозяине появляется копия, и у shadcn нет «обновить с сохранением моих правок»
[факт: документация по namespace про обновления молчит; `--overwrite` перезаписывает]. Как с этим жить:
- **Правило: скопированные файлы набора не правятся.** Всё доменное идёт через слоты, всё визуальное — через
  токены и примитивы хозяина. Шапка каждого файла: `// llm-kit v0.3.0 — generated, do not edit; see …`.
- В хозяине лежит тест `llmkit.lock.test.ts`: sha256 каждого скопированного файла совпадает с записанным в
  `llmkit.lock.json`. Если кто-то поправил файл руками, тест красный. Сначала поднимают изменение в набор,
  потом ставят новую версию.
- Обновление: `npx shadcn add @llmkit/chat-panel@0.4.0 --overwrite`, затем `git diff`, затем тесты хозяина.
  [гипотеза] Параметр версии в namespace-URL (`params`/`{version}`) работает так, как описан в документации.
  Проверить на прототипе registry: два тега, установка, перезапись.

**Сервер (Python).** Студия ставит зависимости в slim-образе через `pip install -r requirements-studio.txt`,
`git` в образе нет (`Dockerfile.studio`) [факт]. Поэтому `pip install git+…` в образе не сработает без
правки Dockerfile и токена к приватному репо. [рек.] Сервер тоже копируется: `llmkit_server/` кладётся
подпакетом хозяина (`h3_48gb/llmkit/`) скриптом `llm-kit/tools/vendor.py --to <path>`, рядом
`VERSION` и `CHECKSUMS`. Тест хозяина сверяет контрольные суммы так же, как на фронте. Зависимостей сверх
FastAPI и pydantic у пакета нет. LLM-клиент даёт адаптер: разбор переноса запрещает второй LLM-клиент
рядом с `provider.py`.

## 3. Готовые библиотеки (данные npm на 2026-10-23)

| Библиотека | Версия, дата | Лицензия | Размер (распак.) | Темизация | Мобильность | Стриминг и бэкенд | Годится как база? |
|---|---|---|---|---|---|---|---|
| **assistant-ui** `@assistant-ui/react` | 0.15.31, 22.10.2026; 7 релизов за 06–22.10 | MIT | 2,8 МБ | shadcn/Tailwind, UI ставится CLI `npx assistant-ui add thread` (копирование, как registry) | адаптивна, но шторку и клавиатуру iOS решает хозяин | `LocalRuntime` + `ChatModelAdapter.run({messages, abortSignal})`, стрим через `async *` с полным текстом на каждом шаге; `ExternalStoreRuntime`, если сообщения хранит хозяин [факт, документация] | **частично**. Технически подходит (ExternalStore поверх наших тредов и событий), но 0.x с частыми ломками API, и примитивы у неё свои. Нам нужна десятая часть: без веток, вложений и голоса. Брать не стоит, кроме случая, когда понадобятся ветки и редактирование ходов |
| **Vercel AI SDK UI** `@ai-sdk/react` (`useChat`) + `ai` | 4.0.154 / 7.0.150, 22.10.2026 | Apache-2.0 | 0,3 МБ + 8,4 МБ (`ai`, частично вырезается при сборке) | без UI | — | протокол UI-сообщений AI SDK (поток частей с собственным заголовком), Python-сервер должен его эмулировать; peer `zod`; peer React `^19.2.1` студию пропускает | **нет**: ход сценариста — JSON по схеме, синхронный (Р7); протокол AI SDK — лишняя зависимость сервера от JS-экосистемы |
| **AI Elements** (shadcn, бывшие AI SDK Elements) | registry, без пакета; AI SDK Elements снимаются с поддержки в конце 11.2026, их заменяют AI Elements в shadcn (10.2026) | MIT (shadcn) [гипотеза: лицензия на странице не указана — проверить репозиторий] | копия в проект | shadcn-токены — «родное» оформление | не описана | примеры на AI SDK (`streamText`, части сообщений); привязка к `useChat` не заявлена, но рендер завязан на типы частей AI SDK | **как образец**: `Prompt Input`, `Model Selector`, `Message` — смотреть разметку и a11y, не ставить целиком |
| **shadcn chat components** (`message-scroller`, `message`, `bubble`, `attachment`, `marker`) + `@shadcn/react` | 06.2026; `@shadcn/react` 0.3.1, 31.08.2026 | MIT | малый | shadcn — родное | не описана | без бэкенда: «не владеет сообщениями, транспортом, состоянием» [факт, changelog] | **кандидат для слоя разметки** `message`/`bubble`. `message-scroller` пока не брать: 0.3.x и открытая ошибка про якоря при монтировании |
| **CopilotKit** `@copilotkit/react-ui` | 1.79.1, 16.10.2026 | MIT | 1,6 МБ | свои CSS-переменные | средне | свой рантайм (AG-UI, агентные действия), нужен серверный слой CopilotKit | **нет**: другой класс задач (копилот с действиями над приложением) |
| **deep-chat** (+`deep-chat-react`) | 2.5.1, 27.08.2026 | MIT | 6,9 МБ | Shadow DOM, стили объектами в JS | хорошая | свой `connect`/`handler`, SSE | **нет** для React-хозяев: не берёт дизайн хозяина. Запасной вариант для не-React страниц |
| **chatscope** `@chatscope/chat-ui-kit-react` | 2.1.1, 15.05.2025 | MIT | 0,7 МБ | свой SCSS | средне | без LLM-логики | **нет**: не развивается 17 месяцев, своя тема |
| **LlamaIndex chat-ui** `@llamaindex/chat-ui` | 0.6.1, 05.06.2026 | MIT | 1,8 МБ | shadcn-подобный | — | под `useChat`-совместимый хендлер | **нет**: 0.x, завязан на модель сообщений Vercel |
| **use-stick-to-bottom** | 1.1.6, 04.06.2026 | MIT | 33 КБ | — | да (ResizeObserver, «прилипание» только когда пользователь внизу) | — | **берём** для ленты |
| **streamdown** | 2.7.1, 19.10.2026 | Apache-2.0 | 125 КБ | Tailwind | — | markdown для потока, незакрытые блоки | **по необходимости**: сценарист отвечает JSON, а `reply` — простой текст. Нужен, если появится свободный чат с markdown |
| vaul (Drawer в shadcn) | 1.1.2, 14.12.2024 | MIT | — | — | шторка с жестом | — | **не брать**: релизов нет 22 месяца. Шторка — shadcn `Sheet` (`side="bottom"`) на Radix Dialog, который у хозяев уже есть |

## 4. Серверный контракт, общий для хозяев

Префикс задаёт хозяин при монтировании: студия — `/api/studio/llm`, ai-writer — `/api/llm`.
Схемы — pydantic в `llmkit_server.schemas`. Фронт-ядро держит зеркальные TS-типы, их совпадение
проверяет тест набора против `openapi.json` эталонного приложения.

### 4.1 Эндпоинты

| Метод и путь | Тело и ответ | Примечания |
|---|---|---|
| `GET /capabilities` | `{kit_version, streaming: bool, scopes: bool, presets: [...]}` | фронт выбирает транспорт (§4.3) и сверяет версию с копией |
| `GET /providers` | `{active, etag, providers: [{name, label, kind: "openai"\|"local", base_url, model, key: {set, source: "kit"\|"env"\|"none", env_name}, available, reason, shares_gpu}]}` | **ни ключа, ни маски** (разбор переноса: маска `sk-***xx` — утечка) |
| `POST /providers` | `{name, label?, kind, base_url, model, api_key?}` → провайдер | `api_key` пишет секрет; заголовок `If-Match: <etag>` обязателен |
| `PATCH /providers/{name}` | поля и `api_key?`, `clear_key?: true` | `api_key` отсутствует — ключ без изменений; смена хоста `base_url` при заданном ключе без нового ключа → 409 `key_required` (Р3) |
| `DELETE /providers/{name}` | — | нельзя удалить `active` → 409 `provider_active` |
| `PUT /providers/active` | `{name}` | глобальный основной |
| `POST /providers/{name}/test` | `{api_key?}` (проверка до сохранения) → `{ok, stage: "models"\|"chat"\|"gate", latency_ms, code, message}` | только сервером: `/v1/models` и однотокенный `chat` (Р4); для локального — сначала гейт |
| `GET /providers/{name}/models` | `{models: [{id, vendor}], cached_at}` | кэш 10 мин |
| `GET /scopes/{scope}/provider`, `PUT …` `{name\|null}` | выбор провайдера в области: проект студии, книга ai-writer | **быстрая смена в чате** (Р5); `null` — основной; хранит адаптер |
| `GET /threads/{thread}/messages?after=<seq>` | `{messages: [{id, seq, role, text, offer?, provider, created_at, status}], busy}` | история; `thread` задаёт хозяин (у студии `project:<id>:scenarist`) |
| `POST /threads/{thread}/turns` | `{text, client_turn_id}` + `Idempotency-Key` → `{user, assistant}` (синхронно) **или** поток (§4.3) | один ход на тред → 409 `chat_busy`; повтор того же ключа возвращает тот же результат |
| `POST /threads/{thread}/turns/{turn}/cancel` | — | см. §4.4 |
| `DELETE /threads/{thread}` | — | «Начать заново» |

### 4.2 Ошибки
Ядро бросает `KitError(status, code, message, detail)`. Тело ответа делает адаптер: у студии это
`StudioError(...).body()` (тот же формат, что у остального API, `h3_48gb/studio/app.py:204-206`), у ai-writer — его формат.
Коды ядра: `llm_not_configured` 503, `llm_failed` 502 (текст только из таблицы безопасных сообщений, как
`SAFE_PROVIDER_MESSAGES` студии), `chat_busy`/`llm_busy` 409, `llm_gpu_busy`/`llm_unloaded` 409
(от гейта адаптера, Р8), `key_required` 409, `etag_mismatch` 409, `provider_not_found` 404,
`provider_active` 409, ошибка валидации 422 **без эха значений**. Ответ 422 в студии уже собирается
только из `loc` и `msg` (`app.py:208-212`) [факт]; тест с ключом-маркером обязателен в наборе (ИЧ1 разбора переноса).

### 4.3 Транспорт хода: синхронный по умолчанию, поток — опционально
- По Р7 разбора переноса для студии ход **синхронный**: `200 {user, assistant}`. Ответ, который не дошёл
  (телефон уснул), подтягивается через `GET /messages?after=` и событие хозяина. На этом режиме набор
  работает без стриминга.
- Поток — режим ядра на будущее (свободный чат, ai-writer): `POST …/turns` с `Accept: text/event-stream`
  отдаёт кадры `turn` → `delta*` → `message` | `error` → `done`. Это **POST и `fetch` + `ReadableStream`**, не `EventSource`:
  `EventSource` умеет только GET. Совместимость с охраной студии:
  - `SameOriginGuard` пропускает POST с того же Origin (`app.py:74-104`) [факт по коду];
  - `BodyLimit` читает только **тело запроса** и отдаёт его приложению повторно, ответ не трогает
    (`app.py:107-144`) [факт по коду];
  - `TrustedHost` — общий для всех путей.
  Разбор переноса советует для стрима в студии использовать существующий `/events`. Это разумно для студии
  (одно соединение, переживает сон вкладки), поэтому ядро даёт оба варианта: синхронный ответ плюс
  уведомления через события хозяина, либо поток в ответе.
- Гейт GPU: если адаптер держит `gpu_lock` для локального Qwen (`h3_48gb/studio/llm.py:41-67`), в потоковом режиме
  замок держится всё время генерации. Решение принимает адаптер, ядро его не скрывает.

### 4.4 Отмена
Клиент делает `AbortController.abort()`, лента сразу помечает ход «отменён вами». Затем `POST …/cancel`:
сервер помечает ход `abandoned`. Что делать с ответом модели, решает адаптер. Для студии — довести ход и
положить в историю со статусом `abandoned` и предложением без кнопки «Применить» (разбор переноса: «на сервере
ход доводится»). Синхронный `urllib`-вызов `provider.py` нельзя прервать посередине [факт: `provider.py`
на `urllib.request`], поэтому честная отмена для студии — именно «не ждать и не применять».

### 4.5 Эталонная реализация `llmkit_server` [рек.]
```python
router = create_router(KitHost(
    providers=FileProviderStore(home / "providers.json", secrets=home / "secrets.env",
                                readonly_env=home / ".env"),          # Р1, Р2: атомарно, flock, etag
    threads=StudioThreadStore(db_path),                                # таблица хозяина
    llm=StudioLlm(home, lock_path),                                    # обёртка над provider.chat_with_schema
    gate=QwenDispatcherGate(...),                                      # Р8; у ai-writer — None
    context=scenarist_context,                                         # (thread, scope) -> messages + schema
    errors=lambda e: StudioError(e.status, e.code, e.message, e.detail),
    events=studio_events.emit,                                         # «chat.message», «llm.provider_changed»
    idempotency=studio_idempotency,                                    # уже есть: h3_48gb/studio/idempotency.py
))
app.include_router(router, prefix=f"{api}/llm")
```
- `FileProviderStore` — общая реализация ядра: формат `providers.json` совместим с `provider.load_providers`
  (`h3_48gb/provider.py:283-303`). ai-writer пишет свой `DbProviderStore` поверх SQLAlchemy и Fernet.
- Эндпоинты — `def`, не `async def`: адаптер студии синхронный (urllib), FastAPI отправит их в пул потоков.
- Роутер **не** ставит CORS и свою охрану: защита — забота хозяина (TrustedHost, SameOriginGuard, лимит тела 2 МиБ).
  Предупреждение для хозяина без такой охраны есть в README набора.
- Снимок OpenAPI студии (`tests/test_studio_openapi.py`, жёсткие пины в `requirements-studio.txt`) изменится
  при монтировании роутера, это ожидаемо. После монтирования — `npm run gen:api`.

## 5. Мобильность

### 5.1 Раскладка [рек.]
- **Телефон (< 768px, тот же `PHONE_QUERY`, что у студии, `h3_48gb/studio/web/src/mobileAction.tsx:21`)**: чат —
  полноэкранная шторка `ChatSheet` (Radix Dialog, `side="bottom"`, высота = `visualViewport.height`).
  Шапка: «Сценарист», чип провайдера, «Закрыть». Лента. Композер, прижатый к низу, с
  `padding-bottom: env(safe-area-inset-bottom)`.
- **Десктоп**: `ChatPanel` встраивается в колонку хозяина (в студии — правая панель редактора, сетка `1.3fr/1fr` AB)
  или та же шторка справа.
- **Конфликт со студией [факт]:** на телефоне студия закрепляет внизу одну главную кнопку (`MobileActionProvider`,
  `mobileAction.tsx:1-20`). Открытая шторка чата должна её скрывать: шторка регистрирует пустое действие с высшим
  приоритетом или провайдер слушает флаг `chatOpen`. Иначе две нижние панели наложатся.

### 5.2 Клавиатура iOS
- `100dvh` не уменьшается при появлении клавиатуры в iOS Safari. Высоту шторки берём из
  `window.visualViewport.height`, сдвиг — из `visualViewport.offsetTop` (подписка на `resize` и `scroll`).
  Хук `useVisualViewport` входит в ядро. [гипотеза: поведение iOS 26 Safari — проверить на телефоне владельца]
- `<meta name="viewport" content="…, interactive-widget=resizes-content">` даёт Chrome на Android сжимать
  макет вместе с клавиатурой. iOS атрибут игнорирует, поэтому хук нужен. В `index.html` студии атрибута
  нет (`index.html:5`) [факт].
- Пока открыта шторка, у `body` выключена прокрутка: Radix Dialog делает это сам через `react-remove-scroll`.
  [гипотеза: на iOS фон под шторкой иногда всё равно прокручивается — проверить на телефоне]

### 5.3 Поле ввода
- **iOS Safari увеличивает страницу на фокусе поля со шрифтом < 16px.** `Textarea` студии набрана `text-[13.5px]`
  (`h3_48gb/studio/web/src/components/ui/textarea.tsx:12`), без `maximum-scale` в viewport [факт]. Композер
  чата: `text-[16px] md:text-[13.5px]` через `--llm-composer-font`. Остальные поля студии на телефоне
  страдают так же — это отдельная мелочь (§9).
- Высота растёт по тексту (`field-sizing: content`, как в студии) до 40% высоты шторки, дальше прокрутка внутри.
- Enter на телефоне — перенос строки, отправка — кнопкой. На десктопе Cmd/Ctrl+Enter, как в ai-writer.
  Проверяется `isComposing`: ввод через IME не отправляется.
- `enterkeyhint="send"`, `autocapitalize="sentences"`, `autocorrect="on"`.

### 5.4 Лента и длинные ответы
- `use-stick-to-bottom`: лента едет вниз за новым сообщением, только если пользователь уже внизу. Если он
  прокрутил вверх, появляется кнопка «↓ к последнему».
- Длинный ответ: `whitespace-pre-wrap`, `overflow-wrap: anywhere` (длинные промпты и URL не ломают 390px).
  Свернуть или развернуть — после 20 строк.
- Кнопка отмены заменяет «Отправить» на время хода и сохраняет её место и размер (44px — крупная кнопка AB).

### 5.5 Доступность
- Лента: `role="log"`, `aria-live="polite"`, `aria-relevant="additions"`. Озвучивается только финальное
  сообщение, при стриминге не каждый `delta`.
- Статус хода («думает…», «отменено», ошибка) — отдельный `role="status"`.
- Фокус: при открытии шторки — в композер, при закрытии — на кнопку, которая её открыла (Radix делает сам).
  После отправки фокус остаётся в поле.
- `ProviderChip` — Radix `DropdownMenu` или `Popover` со списком `radio`. Недоступный провайдер показывается
  `aria-disabled` с причиной в тексте пункта, а не только цветом.
- Цели нажатия ≥ 44×44 на телефоне. Контраст — от токенов хозяина (AB проверен владельцем).

## 6. Рекомендация целиком

### 6.1 Что берём готовым, что пишем
- **Готовое:** shadcn-примитивы хозяина (`button`, `textarea`, `input`, `label`, `badge`, `sheet`, `dropdown-menu`,
  `popover`, `command` для поиска моделей); `use-stick-to-bottom`; разметка `message`/`bubble` из shadcn chat
  components как образец (скопировать в набор и адаптировать, не зависеть); позже по необходимости `streamdown`.
- **Пишем:** ядро (клиент, машина состояний хода, хуки, `useVisualViewport`), пять стилизованных компонентов,
  `llmkit_server` (схемы, роутер, `FileProviderStore`, идемпотентность и замок через адаптер, безопасные
  сообщения), registry и скрипт вендоринга, тесты набора.

### 6.2 Структура репозитория набора
```
llm-kit/                                   # отдельный git-репо владельца (вопрос 1)
  registry.json                            # shadcn: элементы и их registryDependencies
  registry/llmkit/
    lib/        client.ts types.ts turn-machine.ts use-chat.ts use-providers.ts
                use-provider-choice.ts use-visual-viewport.ts            # registry:lib (ядро)
    components/ chat-panel.tsx chat-sheet.tsx message-list.tsx composer.tsx
                provider-chip.tsx provider-form.tsx provider-list.tsx     # registry:component
    tokens.css                                                            # --llm-* с запасными значениями
  public/r/*.json                          # результат `npx shadcn build`, коммитится
  server/llmkit_server/  schemas.py router.py store_file.py errors.py host.py   # Protocol адаптера
  server/tests/          контракт, ключ-маркер, etag, идемпотентность, 409-ки
  web-tests/             vitest-тесты ядра и компонентов; playwright 390px на демо-хозяине
  demo/                  Vite-хозяин с AB-токенами и вторая тема (мост ai-writer) — визуальная проверка «родного»
  tools/vendor.py        копирует server/ в хозяина, пишет VERSION и CHECKSUMS
```

### 6.3 Как подключает студия
1. `cd h3_48gb/studio/web && npx shadcn@latest add ~/Projects/llm-kit/public/r/chat-sheet.json …provider-form.json`.
   Файлы ложатся в `src/components/llmkit/*` и `src/lib/llmkit/*`, недостающие shadcn-примитивы (`sheet`,
   `dropdown-menu`) ставятся как зависимости, `use-stick-to-bottom` — в `package.json`.
2. В `styles.css`: `@import "./components/llmkit/tokens.css";` и при желании `--llm-bubble-user: var(--cobalt-soft)`.
3. `python ~/Projects/llm-kit/tools/vendor.py --to h3_48gb/llmkit` → адаптер `h3_48gb/studio/llm_host.py`
   (§4.5) → `include_router` в `app.py`.
4. Экран: `<ChatSheet thread={`project:${id}:scenarist`} scope={`project:${id}`} renderMessageExtra={m => <OfferApply offer={m.offer} />} />`.
   `OfferApply` — код студии: вызывает O8, O26, O29 (Р6).
5. Клиент: ядро принимает `fetch` хозяина (`createClient({ baseUrl: "/api/studio/llm", fetch: studioFetch })`) —
   свой `api()` студии без TanStack Query и axios (`web/src/api/client.ts`). События: ядро подписывается через
   переданный `subscribe(type, cb)`. У студии это существующий SSE `/events`.

### 6.4 Как подключает ai-writer (после освобождения его фронта)
1. `components.json` (`tsx: false` — CLI переводит TSX в JSX [гипотеза: проверить на прототипе], алиасы `@/`
   в `vite.config.js` и `jsconfig.json`).
2. Мост токенов §1.3, shadcn-примитивы рядом с `shared/ui`.
3. `npx shadcn add …`, адаптер на FastAPI ai-writer: `DbProviderStore` поверх существующих провайдеров и Fernet,
   треды — его `dialog_json` или новая таблица.
4. `ConversationStudio` и шесть доменных обёрток переводятся на `ChatPanel` постепенно, по одной.
   `renderAssistantExtra` переходит в `renderMessageExtra`.
   Вложения, `kind: "question"` и «перемотка» в первом срезе набора отсутствуют (разбор переноса §3).
   Если они нужны ai-writer до их появления в наборе, эти обёртки остаются на `ConversationStudio`.

### 6.5 Как обновлять
- В наборе: semver-тег, `npx shadcn build`, CHANGELOG с пометкой «ломает контракт сервера» или нет.
  `kit_version` уходит в `GET /capabilities`.
- В хозяине: `npx shadcn add <элементы> --overwrite` и `vendor.py --to …` той же версии, затем `git diff`,
  затем тесты. Тест `llmkit.lock` (фронт) и `CHECKSUMS` (сервер) краснеют, если копию правили руками.
  Тест `capabilities` краснеет, если версии фронта и сервера у хозяина разошлись.
- Правка, нужная одному хозяину, оформляется как слот или проп в наборе, а не как правка копии.

### 6.6 Объём [оценка]

| Работа | Дни |
|---|---|
| Ядро фронта: клиент, машина хода (idle→sending→streaming?→done/error/cancelled), оптимистичный ход, идемпотентность, abort, хуки | 0,8 |
| Стилизованный слой: 5 компонентов, токены, шторка, клавиатура, a11y | 1,2 |
| `llmkit_server`: схемы, роутер, `FileProviderStore` (атомарно, flock, etag, `secrets.env`), тест связи, модели, ошибки, ключ-маркер | 1,0 |
| Registry, `shadcn build`, `vendor.py`, lock-тесты, демо-хозяин с двумя темами | 0,5 |
| Адаптер студии и экран (миграция, контекст, `OfferApply`, гейт Qwen) — это часть S2c из разбора переноса | 0,8 |
| Мутационная проверка тестов (правило репозитория) и проход на телефоне 390 | 0,4 |
| **Итого до студии в работе** | **≈4,7** |
| Подключение ai-writer (мост, примитивы, адаптер на его БД, первая обёртка) | ≈1,0, позже |

Сверх 3 дней разбора переноса добавляется ≈1,7 дня — цена переиспользования. Без второго хозяина эти 1,7 дня
не окупаются.

### 6.7 Риски

| Риск | Мера |
|---|---|
| Копии в хозяевах расходятся с набором | lock-тесты и правило «не править»; доменное — в слоты |
| CLI shadcn меняет поведение (`add --overwrite`, параметры версий, перевод в JSX) | зафиксировать `shadcn@4.22.1` в README набора; прототип registry до основной работы (0,2 дня, входит в 0,5) |
| ai-writer «почти родной»: shadcn-кнопки рядом с его `Button` | мост токенов; вопрос 3 владельцу; в худшем случае ai-writer подключает только ядро и рисует разметку своими примитивами — ядро для этого и отделено |
| iOS: клавиатура, прокрутка фона, увеличение на фокусе | `useVisualViewport`, 16px в композере, ручная проверка на телефоне (playwright WebKit клавиатуру не эмулирует) |
| Наложение нижней кнопки студии и композера | §5.1, тест: при открытом чате `MobileActionBar` скрыт |
| Ключ в ответе, логе или событии | ключ-маркер в тестах ядра для любого хозяина; `SecretStr`; операции с ключом без хеша тела в `op_log` |
| Поток держит GPU-замок долго | только синхронный режим для локального Qwen в студии; поток включает адаптер явно |
| assistant-ui понадобится позже (ветки, редактирование ходов) | ядро набора совместимо с `ExternalStoreRuntime` по форме данных: сообщения у хозяина, колбэк `onNew`. Переход возможен без смены сервера |

## 7. Стыковка со срезом студии «чат со сценаристом» (S2c)
- Разбор переноса (Р1–Р9) остаётся спецификацией **поведения**. Этот документ задаёт **форму**:
  код Р1/Р2 (`providers.json` + `secrets.env`, etag, Р3, Р4) — это `FileProviderStore` и роутер ядра;
  Р5 — `scopes`; Р6 — слот `renderMessageExtra` и `OfferApply` студии; Р7 — синхронный транспорт;
  Р8 — `gate` адаптера; Р9 — граница ядро/адаптер, у неё теперь есть физическая упаковка.
- Порядок: (1) прототип registry и вендоринга на пустом демо-хозяине, 0,2 дня, снимает риски CLI;
  (2) ядро и сервер в `llm-kit`; (3) подключение в студию как задача S2c. План S2c пишется после решения
  владельца по вопросу 1 и по правилу репозитория проверяется до первого исполнителя (`/codex`).
- В плане S2–S3 ред. 4.1 (коммит `935acb72`) места под набор нет [гипотеза: план не перечитывался под эту
  задачу — проверить, не занимает ли S2c уже экран чата другими компонентами].

## 8. Расхождения с разбором переноса
- Разбор: «если позже понадобится стрим — тем же `/events`, не отдельным POST-стримом». Для студии согласен.
  Для набора оставляю **оба** режима: у ai-writer и будущих хозяев нет журнала событий студии, а POST-поток
  совместим с охраной студии по коду (§4.3).
- Разбор: `ChatPanel` пишется в TS. Согласен. Добавляю, что ai-writer на JSX и получит копию через
  перевод CLI или подключит только ядро (§6.4).

## 9. Мелочи
- Поля студии на телефоне (13,5px) вызывают увеличение страницы в iOS Safari на фокусе. Это не про чат,
  но проявится в тех же сценариях (§5.3).
- В `index.html` студии нет `interactive-widget=resizes-content` (§5.2).
- `--color-accent` в ai-writer и в shadcn означает разное (§1.3). Это ловушка для любых shadcn-компонентов
  в ai-writer, не только для чата.

## 10. Не проверено
- Прототип registry не собирался: `shadcn build`, установка из локального файла, `--overwrite` с версией,
  перевод TSX→JSX для ai-writer. Проверка — пустой Vite-хозяин, 0,2 дня.
- Поведение `visualViewport`, прокрутки фона и `interactive-widget` на iOS 26 — нужен телефон владельца.
- Лицензия AI Elements в shadcn (на странице changelog не указана).
- Ошибка якорей `message-scroller` (из поисковой выдачи; тикет не открывался).
- Совместимость ядра с `ExternalStoreRuntime` assistant-ui — по документации, без кода.

## Источники
- npm registry, 2026-10-23: `npm view <пакет> version license time.modified dist.unpackedSize peerDependencies`
  для всех пакетов таблицы §3.
- [AI SDK Elements: объявление о переходе в shadcn](https://elements.ai-sdk.dev/)
- [shadcn changelog: AI Elements, 10.2026](https://ui.shadcn.com/docs/changelog/2026-10-ai-elements)
- [shadcn changelog: chat components, 06.2026](https://ui.shadcn.com/docs/changelog/2026-06-chat-components)
- [shadcn: namespaced registries](https://ui.shadcn.com/docs/registry/namespace)
- [assistant-ui: LocalRuntime](https://www.assistant-ui.com/docs/runtimes/custom/local-runtime.md),
  [Custom Backend](https://www.assistant-ui.com/docs/runtimes/custom/overview),
  [External Store](https://www.assistant-ui.com/docs/runtimes/custom/external-store)
