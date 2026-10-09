# Срез B: фронтенды, app-билдеры, оркестраторы поверх ComfyUI и мульти-бэкенд

Дата ресерча: 2026-10-09. Метод: GitHub API (звёзды, pushed_at, релизы, контрибьюторы, лицензия) и README/доки первоисточников + WebSearch для обнаружения. Всё, что не проверено по первоисточнику, помечено «не проверено». Критерии — из BRIEF.md (п. 1-8).

## 1. Главный вывод

1. Самая сильная находка среза — **ArcReel** (ArcReel/ArcReel): 5405 звёзд, релиз v0.33.0 от 2026-10-08, AGPL-3.0, Docker, FastAPI+React, очередь задач, REST+API-ключи+MCP, учёт стоимости, агент на Claude Agent SDK (оркестрирующий skill + субагенты), **подключение своего ComfyUI по адресу через импорт API-workflow с привязкой узлов** (ADR 0081/0082). Это зрелее AI Movie Studio 2 на порядок (169 звёзд). Минусы: заточен под китайский рынок (китайские провайдеры, экспорт в Jianying), нет подтверждённой поддержки H3 локально/LoRA/продолжения от последнего кадра (не проверено), агент завязан на Anthropic или совместимые конфигурации.
2. Как **исполнительный слой** свой ComfyUI заменять не надо: он живее всех (v0.39.0 от 2026-10-05, 136 тыс. звёзд, H3 нативно через Comfy-Org/MiniMax-H3). Вокруг него есть готовые слои: App Mode (интерфейс), comfy-mcp / artokun/comfyui-mcp (агентный доступ), comfyui-api от Salad (HTTP-шлюз), SwarmUI (мульти-бэкенд, очередь, API).
3. **SwarmUI** — единственный «взрослый» UI с **MiniMax H3, Krea 2, Z-Image, LTX-2, Wan** в списке поддерживаемых моделей и полным сетевым API; но это генератор, а не студия (нет проекта/сцен/сборки).
4. Готового «проект → сценарий → сцены → дубли → таймлайн» поверх ComfyUI с LLM-сценаристом и локальным H3 в этом срезе я не нашёл в законченном виде; ближайшие: ArcReel (проектный слой, ComfyUI как провайдер), NodeTool (агент + раскадровка + таймлайн, но ComfyUI в README не упомянут), Nomi (только Mac/Windows).

## 2. Таблица кандидатов (24 строки)

Колонки: ★ — звёзды; push — последний коммит (pushed_at); релиз — последний тег; лиц. — лицензия; роль для нас: **целиком / основа / куски / идеи**.

| # | Проект | ★ | push | Последний релиз | Лиц. | Что это | ComfyUI / свой адрес | Роль |
|---|---|---|---|---|---|---|---|---|
| 1 | [ArcReel/ArcReel](https://github.com/ArcReel/ArcReel) | 5405 | 2026-10-09 | v0.33.0 (2026-10-08), 5 релизов за 5 недель, 20 контрибьюторов, 101 открытый issue | AGPL-3.0 | Self-hosted студия: проект→ассеты→сценарий→раскадровка→видео→сборка, агент, очередь, стоимость | Да: ComfyUI-эндпоинт = импортированный API-workflow + привязки узлов, свой URL, опц. прокси-авторизация | **основа / кандидат №1** |
| 2 | [Comfy-Org/ComfyUI](https://github.com/comfyanonymous/ComfyUI) + App Mode | 136 593 | 2026-10-09 | v0.39.0 (2026-10-05) | GPL-3.0 | Сам исполнитель; App Mode/App Builder (фронтенд ≥1.41.13) | — это он | **исполнитель, оставить** |
| 3 | [mcmonkeyprojects/SwarmUI](https://github.com/mcmonkeyprojects/SwarmUI) | 4644 | 2026-10-01 | 0.9.8-Beta (2026-02-06) | MIT | Генератор: UI+API, мульти-бэкенд, очередь, Comfy-вкладка; H3/Krea2/Z-Image/LTX-2/Wan | Сам ставит ComfyUI или подключает внешний бэкенд | **куски / запасной UI** |
| 4 | [nodetool-ai/nodetool](https://github.com/nodetool-ai/nodetool) | 560 | 2026-10-09 | v0.8.2 (2026-10-08) | AGPL-3.0 | Agent-first студия: раскадровка, сценарий/голос, таймлайн, нод-редактор, mini apps, MCP; macOS/Win/Linux, Docker | ComfyUI в README не упомянут (есть страница «alternatives/comfyui»); Ollama/MLX/GGUF, FAL, Replicate | **куски / идеи UX** |
| 5 | [aqm857886159/Nomi](https://github.com/aqm857886159/Nomi) | 555 | 2026-10-09 | v0.23.1 (2026-10-08) | AGPL-3.0 | Локальная десктоп-студия: агент, раскадровка, 3D-режиссёр с телефоном-видоискателем, таймлайн, MCP (24 тула) | Да, «local ComfyUI» как провайдер; но сборки только macOS/Windows, Linux нет | **идеи (3D-режиссёр, телефон)** |
| 6 | [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) | 802 | 2026-10-05 | v0.52.205 (2026-10-01), 19 контр. | MIT | MCP-сервер + боковой агент: 38 тулов, 42 skill (Flux, WAN, LTX 2.3, **MiniMax H3**, Qwen, Z-Image), удалённый ComfyUI по URL | Да (`COMFYUI_URL`, remote-режим) | **основа агентного доступа** |
| 7 | [Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) | 268 | 2026-10-09 | v0.10.0 (2026-08-10) | NOASSERTION (в GitHub; в доках «fully open source») | Официальный MCP: локальный (stdio, через comfy-cli) и облачный | Локальный — ComfyUI из workspace; про произвольный URL доки молчат | **куски** |
| 8 | [PxTicks/vlo](https://github.com/PxTicks/vlo) | 219 | 2026-10-09 | v0.3.1 (2026-10-09) | AGPL-3.0 | Браузерный видеоредактор, ComfyUI как движок генерации; 18 workflow, в т.ч. **MiniMax H3**, LTX-2.5; SAM2 | Да: свой/удалённый ComfyUI | **куски (монтаж + H3-workflow)** |
| 9 | [ViewComfy/ViewComfy](https://github.com/ViewComfy/ViewComfy) | 669 | 2026-03-19 | v0.3.23 (2026-01-08) | AGPL-3.0 | Workflow→веб-приложение (Next.js) | Да | **не брать: 6+ мес. без коммитов, App Mode перекрыл** |
| 10 | [SaladTechnologies/comfyui-api](https://github.com/SaladTechnologies/comfyui-api) | 454 | 2026-09-18 | 1.19.2 (2026-09-18) | MIT | HTTP-шлюз к ComfyUI: sync/async, webhooks, S3/HF, манифест моделей, swagger | Это обёртка над ComfyUI | **куски (шлюз при масштабировании)** |
| 11 | [robertvoy/ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed) | 632 | 2026-08-14 | v1.4.0 (2026-02-28) | Apache-2.0 | Расширение: параллельные воркеры на нескольких GPU/машинах, распределённый апскейл тайлами, видео | Внутри ComfyUI | **не нужно при одной карте** |
| 12 | [invoke-ai/InvokeAI](https://github.com/invoke-ai/InvokeAI) | 28 497 | 2026-10-09 | v6.14.2 (2026-09-27), 100 контр. | Apache-2.0 | Зрелая студия для картинок (холст, слои) | Нет, свой движок; Wan только через API | **идеи холста; не исполнитель** |
| 13 | [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion) | 10 683 | 2026-10-03 | v1.53.0 (2026-08-22) | GPL-3.0 | Плагин Krita, бэкенд ComfyUI, свой/удалённый сервер | Да | **вне задачи (картинки, ручная правка)** |
| 14 | [BennyKok/comfyui-deploy](https://github.com/BennyKok/comfyui-deploy) / comfy-deploy/comfydeploy | 1532 / 458 | 2025-11-13 / 2025-09-19 | v2.0.0 (2025-03-24) | AGPL-3.0 / GPL-3.0 | «Vercel для ComfyUI» | Да | **мёртв/коммерциализирован, не брать** |
| 15 | [runpod-workers/worker-comfyui](https://github.com/runpod-workers/worker-comfyui) | 746 | 2026-09-21 | 5.11.0 (2026-09-21) | AGPL-3.0 | ComfyUI как serverless на RunPod | Облако | **идеи (облачный рендер)** |
| 16 | [bentoml/comfy-pack](https://github.com/bentoml/comfy-pack) | 216 | 2025-11-10 | 0.4.4 | Apache-2.0 | Упаковка workflow в BentoML-сервис | — | **архив (archived=true), не брать** |
| 17 | [Comfy-Org/comfy-cli](https://github.com/Comfy-Org/comfy-cli) | 1008 | 2026-10-09 | v1.22.0 (2026-09-30) | GPL-3.0 | Официальный CLI: запуск, jobs, узлы | Да | **куски (jobs wait/status)** |
| 18 | NousResearch/hermes-agent: skill `comfyui` и `kanban-video-orchestrator` (optional-skills/creative) | — | 2026-10 (ветка) | — | MIT (skill) | Skill ComfyUI (REST/WS + comfy-cli); мета-пайплайн видео на Kanban из агентских профилей | Да | **куски/идеи для нашего хоста Hermes** |
| 19 | n8n: community-ноды `n8n-nodes-comfyui`, `n8n-nodes-comfyui-toolkit` (submit/wait/fetch) | — | не проверено | не проверено | — | Нода ComfyUI для n8n; toolkit — неблокирующая | Да | **идеи (шаблон submit/wait/fetch)** |
| 20 | Dify: плагин ComfyUI ([marketplace](https://marketplace.dify.ai/plugin/langgenius/comfyui)) | — | не проверено | 0.3.8 (по листингу) | — | Инструмент в Dify: API-workflow, свой URL, LoadImage по порядку | Да | **не нужно** |
| 21 | [WhatDreamsCost/WhatDreamsCost-ComfyUI](https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI) (LTX Director) | 2079 | 2026-07-30 | — | GPL-3.0 | Таймлайн-нода для LTX 2.3 внутри ComfyUI | Внутри ComfyUI | **идеи** |
| 22 | [seesee75-commits/ComfyUI-MiniMaxH3-Director](https://github.com/seesee75-commits/ComfyUI-MiniMaxH3-Director) | 312 | 2026-10-01 | v0.3.2 (2026-10-01) | GPL-3.0 | Таймлайн H3 в ComfyUI: раскадровка, first/last, референсы, ретейки, цепочка шотов | Внутри ComfyUI | **куски (точно про H3)** |
| 23 | XmYx/ComfyStudio | 24 | 2026-07-20 | — | MIT | Видеоредактор с ComfyUI | Да | не брать (24 звезды) |
| 24 | itsjwill/vanta | 136 | 2026-07-26 | — | NOASSERTION | Remotion + Wan/LTX; в README много roadmap | Да | не брать |

Контрибьюторы сняты по API (список до 100): ArcReel 20, NodeTool 15, Nomi 10, artokun 19, SwarmUI 75; InvokeAI и ComfyUI упёрлись в потолок 100.

Только облако (для идей, критерий 1 не проходят): Comfy Cloud и Comfy Cloud MCP, ComfyHub (preview), ViewComfy Cloud, Comfy Deploy hosted, RunComfy, Graydient/Flow-подобные сервисы — по ним первоисточники не открывал, в отчёт как факты не включаю.

## 3. Разбор топ-5

### 3.1 ArcReel — кандидат на основу

Проверено по README, ADR 0081, docs.arc-reel.com (providers, architecture).
- Критерий 1: self-hosted, `docker compose up -d`, порт 1241, логин admin, SQLite (локально) или PostgreSQL. Linux+Mac через Docker подходит.
- Критерий 2: ComfyUI подключается как «кастомный провайдер»: импорт API-формата workflow, привязка prompt/ассетов/размеров/длительности/выхода к узлам, одна workflow может висеть на нескольких машинах; опрос `/history` (не WebSocket), отмена задачи в ComfyUI при отмене в UI, опциональный заголовок авторизации для обратного прокси. Это ровно «подключить свой ComfyUI по адресу». sglang напрямую не упомянут; OpenAI-совместимые и Google-совместимые эндпоинты — да; декларативные JSON-эндпоинты (submit + poll) — да, есть «Market».
- Критерий 3: проект→анализ→ассеты (персонажи/сцены/реквизит)→эпизоды и структурный сценарий→раскадровка/мультисетка→видео-клипы+озвучка→сборка; ассеты-референсы переиспользуются между шотами, перегенерация отдельного ассета, откат версий. Продолжение от последнего кадра — в доступных мне доках не подтверждено.
- Критерий 4: H3, Krea 2, Z-Image, LTX, Wan как конкретные модели не заявлены; поддерживается любой workflow, значит возможны, если привязки узлов выразимы. H3 в README упомянут только как спонсорский API (Metaso). LoRA — не проверено.
- Критерий 5: агент на Claude Agent SDK, схема «orchestration skill + узкие субагенты», детерминированные операции в тулах; текст — предустановленные Gemini/Ark/Grok/OpenAI/DashScope/MiniMax/Agnes и произвольный OpenAI-совместимый. Vision-оценка — «ревью материала» субагентом, детали не проверены.
- Критерий 6: REST (проекты, ассеты, задачи, диалоги агента), API-ключи `arc-`, SSE, синхронный диалоговый эндпоинт для внешних агентов, MCP в слое доставки (ADR 0065 про remote MCP), очередь с восстановлением и идемпотентностью. Мобильный клиент не заявлен (веб).
- Критерий 7: ★5405, push 2026-10-09, релиз v0.33.0 от 2026-10-08, релизы 09-05, 09-10, 09-23, 10-02, 10-08; репозиторий создан 2026-02-07 (8 месяцев), AGPL-3.0, CI+codecov+Docker Hub, двуязычная документация (есть i18n/en, README.en.md).
- Критерий 8: **основа** при условии: терпим AGPL (форк — сетевой копилефт), китайский акцент (Jianying, провайдеры, «漫剧»), агент на Anthropic-совместимом. Риск: молодой проект, быстрый дрейф, ADR-история показывает много багов формата ComfyUI combo/wire type (в `docs/fixes` за 08-09..09-24). Что проверить руками перед решением: поднять Docker, подключить alex-neuro ComfyUI, прогнать H3 workflow с привязками (ref-изображения, first/last), посмотреть, умеет ли «от последнего кадра».

### 3.2 ComfyUI + App Mode + comfy-cli/MCP — исполнитель и его интерфейсы

- Источники: docs.comfy.org/interface/app-mode, blog.comfy.org (запуск 2026-03-10), GitHub.
- App Mode (фронтенд ≥1.41.13): билдер из 4 шагов (входы, выходы, предпросмотр, вид по умолчанию), тот же бэкенд и очередь, оптимизирован под мобильные и узкие экраны (вкладки вход/выход/ассеты). Ограничения: ссылки-шеринг работают **только в Comfy Cloud**, на локальном нет; API для App Mode в доках не описан. Для нашей студии это дешёвый «телефонный пульт к одному workflow», но не проект/сцена.
- Исполнение: стандартный REST/WebSocket (/prompt, /history, /view, /upload/image), comfy-cli 1.22.0 (jobs status/wait/cancel), официальный comfy-mcp (локальный, v0.10.0, лицензия в GitHub NOASSERTION), community-MCP artokun (удалённый ComfyUI по URL, skill для H3/LTX 2.3/Wan/Z-Image, MIT, свежий).
- Важное для безопасности: сервер ComfyUI без аутентификации по умолчанию (официальные доки), нужен обратный прокси; у Salad-шлюза входящей авторизации тоже нет.
- Вывод: не заменять. Свой код должен жить над REST ComfyUI; MCP-слой можно взять artokun как есть для агентов (Claude/Hermes), а skill-тексты по H3 использовать как справочник.

### 3.3 SwarmUI — запасной UI и мульти-бэкенд

- v0.9.8-Beta от 2026-02-06, но коммиты свежие (2026-09-30 «swap h3 int8 vae for comfy-org variant», 2026-10-01); MIT; ~75 контрибьюторов; статус «Almost-Release».
- Модели: в docs/Video Model Support.md отдельная секция **MiniMax H3** (FL2AV и Ref2AV, int8-конверсии Comfy-Org, Turbo-LoRA 4/8 шагов lightx2v, текстовый энкодер Qwen3 VL 32B fp4 ставится сам), LTX Video 2, Wan 2.1/2.2; изображения — Krea 2, Z-Image, Flux. Это единственный найденный UI, где H3+Krea 2+Z-Image+LTX+Wan в одной документации.
- Архитектура: встроенный или внешний ComfyUI-бэкенд, несколько бэкендов, AutoScalingBackend, вебхуки, пресеты, вкладка Comfy Workflow; **полный сетевой API** (`POST /API/<route>`, WebSocket-маршруты, сессии, аккаунты и токены).
- Слабости для нас: нет слоя проекта/сцен/дублей, нет LLM-сценариста; Linux-установка работает, десктоп-режим на Linux не тестирован. Роль: «пульт ручной генерации и второй путь в H3», либо источник знаний о параметрах H3 (формат промпта `<d>[English] ...</d>`, `(S1)` и т.д.).

### 3.4 NodeTool — идеи UX и агентной раскадровки

- Проверено по README: v0.8.2 (2026-10-08), ★560, AGPL-3.0, ~15 контрибьюторов (API per_page), macOS/Windows/Linux и Docker, MCP-сервер (`npx @nodetool-ai/cli mcp install`).
- Есть: раскадровка по шотам (action/camera/motion/duration, стиль-библия, нарратив, музыка), «сначала дешёвые стадии» (стоп-кадр до видео), «сущности» (персонажи/места вставляют один и тот же дескриптор), таймлайн с привязкой workflow к клипу и пометкой stale, скрипт+голос, отдельная правка одного шота video-to-video.
- Нет/не подтверждено: ComfyUI как исполнитель, H3/Krea/Z-Image/LTX/Wan локально (упоминаются Ollama, MLX, GGUF, FAL, Replicate, KIE), продолжение от последнего кадра, консистентность по референс-изображениям.
- Роль: образец проектной модели (stale-флаги, «entities») для нашего видения; брать код целиком не стоит (свой нод-редактор, 7 редакторов, широкая поверхность).

### 3.5 Nomi — идеи 3D-режиссёра и мобильного видоискателя

- v0.23.1 (2026-10-08), ★555, AGPL-3.0, ~10 контрибьюторов (API), сборки только macOS (arm64/Intel) и Windows; Linux нет. Проект = папка на диске (канвас, раскадровка, таймлайн), MCP на 24 тула, модели per-shot (Seedance, Kling, Wan, Hailuo, Nano Banana, любые OpenAI-совместимые, локальный ComfyUI), референс-карточки для лиц/мест, «3D-режиссёр» с телефоном как видоискателем.
- Роль: единственное, что прямо перекликается с нашей «мобильной» идеей; код как основа не подходит (десктопное приложение под Mac/Win, нет серверного режима для CUDA-Linux, не подписан).

## 4. Что можно взять вместо своего (сводка по слоям)

| Слой | Взять | Комментарий |
|---|---|---|
| Исполнитель | ComfyUI (оставить) | Свежесть, нативный H3 |
| Агентный доступ | artokun/comfyui-mcp; comfy-cli; Hermes skill comfyui | Для Claude/Hermes; remote URL поддержан |
| Проект/сцены/дубли/очередь/стоимость | ArcReel (проверить), иначе своё | Единственный зрелый кандидат с ComfyUI-эндпоинтом |
| Ручная генерация/H3-пульт | SwarmUI | Параллельный путь, MIT, API |
| Монтаж | Vlo (куски), NodeTool (идеи) | Vlo — браузерный, ComfyUI-нативный |
| Шлюз/масштаб | comfyui-api (Salad) | Нужен только при нескольких машинах/вебхуках |
| Мобильный пульт | ComfyUI App Mode | Для одного workflow; шеринг ссылкой только в облаке |
| Автоматизация | n8n/Dify | Не нужны, если есть MCP |

## 5. Что не сделано / не проверено

- Не открывал страницы релизов n8n-нод и Dify-плагина (даты не сняты); облачные Graydient/Flow-подобные сервисы не изучал.
- Для ArcReel не подтверждены: продолжение от последнего кадра, LoRA, vision-оценка, реальная работа с H3-workflow. Нужен 1-2 часа ручной проверки в Docker.
- Контрибьюторы: для InvokeAI и ComfyUI известно только «≥100» (потолок страницы API).
- Звёзды и push сняты 2026-10-09 через GitHub API; «последний коммит» = pushed_at (может включать ветки).
- Лицензию Comfy-Org/comfy-mcp GitHub не определил (NOASSERTION), в доках — «fully open source».

## 6. Ссылки (первоисточники)

- ArcReel: https://github.com/ArcReel/ArcReel , https://docs.arc-reel.com/en/guide/providers , https://docs.arc-reel.com/en/dev/architecture
- ComfyUI App Mode: https://docs.comfy.org/interface/app-mode , https://blog.comfy.org/p/from-workflow-to-app-introducing
- Comfy MCP: https://docs.comfy.org/agent-tools/mcp , https://github.com/Comfy-Org/comfy-mcp
- artokun/comfyui-mcp: https://github.com/artokun/comfyui-mcp
- SwarmUI: https://github.com/mcmonkeyprojects/SwarmUI (docs/Video Model Support.md, docs/API.md)
- NodeTool: https://github.com/nodetool-ai/nodetool ; Nomi: https://github.com/aqm857886159/Nomi ; Vlo: https://github.com/PxTicks/vlo
- Salad comfyui-api: https://github.com/SaladTechnologies/comfyui-api
- Hermes skills: https://github.com/NousResearch/hermes-agent/tree/main/optional-skills/creative
