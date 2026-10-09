# D. ai-writer 2.0 как «режиссёр» студии — разведка (только чтение)

Дата: 2026-10-09. Репо: `/Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/ai-writer 2.0`
(в дальнейшем AW), main, HEAD `8dc6185f` от 2026-10-09 18:57, 3237 коммитов с 2026-05-05.
Worktree `.claude/worktrees/*` старее (оба на 2026-08-07) — игнорированы. Все пути ниже
относительно корня AW.

Главная находка: **владелец недооценил — в AW уже есть не «выгрузка куска главы в сценарий»,
а целая граница «Клип-сценарий» (ADR-128), написанная ПОД H3** (формат-спека выстрадана на
прогонах этого репо). Двухстадийный режиссёр, валидатор, экспорт в ingest-формат H3,
REST + MCP + UI. Это самый близкий к нужному готовый узел.

## 1. Модель данных и выгрузка в сценарий

| Сущность | Где (путь:строка) | Поля, существенные для «режиссёра» | Статус |
|---|---|---|---|
| Произведение `Project` | `backend/app/db/models/project.py:12` | `work_type` in {novel, novella, short_story, series_book, play} (`backend/app/schemas/project.py:10`), `genre`, `language`, `expected_chapters_count`, `target_words_per_chapter/scene` | работает |
| Идея/концепт | `backend/app/db/models/story_concept.py:16`, агент `backend/app/agents/story_concept.py:15` | `{title, logline, synopsis, long_arc[3], central_conflict, cast[{name, role, one_line, visual_tag}], setting_seeds}`, диалоговая доводка (ADR-064) | работает |
| Структура/арки | `backend/app/db/models/story_outline.py:25` (`acts_json`, `beats_json`, `motifs_json`), `:61` ChapterOutline (role, beat_anchor, summary, stakes), `:85` CharacterArc (arc_start/mid/end, internal_need) | сюжетные акты, биты, арки персонажей | работает |
| Арки отношений | ADR-068, `backend/app/db/models/character.py:66` | отношения как путь во времени | работает |
| Прочие рычаги драматургии | ADR-092/094 (ружья), ADR-098/101 (маршруты намерений), ADR-102 (сверхзадача персонажа `Character.superobjective`, `character.py:~30`) | сквозной вектор, ружьё, раскадровка пика | работает |
| Персонаж `Character` | `backend/app/db/models/character.py:19` | `appearance`, `portrait_json` (канон v2, 13 ключей: face/hair/figure/clothing/voice/habits_gestures…), `life_status`, `portrait_asset_id` | работает |
| Глава / версия | `backend/app/db/models/chapter.py:26` / `:106` (`scene_bodies_json`) | текст посценно | работает |
| План главы, Scene Card | `backend/app/db/models/chapter.py:57` (`scenes_json`), `backend/app/agents/chapter_planner.py:166` | pov, location, time, mission, obstacle, entry/exit_state, `emotional_turn`, `subtext`, `speech_modes`, `physical_business`, `sensory_anchors`, `beats[trigger, visible_action]`, `tension_target` | работает (ADR-039) |
| Клип-сценарий `ClipScenario` | `backend/app/db/models/clip.py:19` | `chapter_id`, `duration_seconds`, `status` (draft/pending_approval/approved), `scenario_json`, `scenario_markdown`, `validation_json`, `dialog_json`, `concept_text`, `outdated_status` | работает, в main |

Иерархия «произведение → арки → персонажи → главы → сцены» полная; **сцена — это Scene Card внутри
ChapterPlan, а не отдельная таблица**. Клип привязан к ГЛАВЕ целиком (не к куску): `chapter_id`
(`clip.py:~25`). Клип по нескольким главам или по книге — «после обкатки на одной главе»
(`docs/adr/128-clip-scenario.md:176-200`). Фрагмент главы (выделение текста) есть только у
иллюстраций (ADR-059 «fragment workbench»), у клипа нет.

### Формат выгрузки (машинная истина v1.1)
`backend/app/schemas/clip.py:140` `ClipScenarioFile`, схема сцены `:120` `ClipScene`:

| Поле файла | Тип | Комментарий |
|---|---|---|
| `format_version` "1.1", `duration_seconds` | | бюджет ролика |
| `style_bible` | str (EN) | вклеивается дословно в каждый prompt, БЕЗ портретов |
| `characters{имя: {portrait_phrase, latin_name}}` | | портрет-фраза вклеивается только в сцены с персонажем |
| `scenes[]`: `scene_id`, `start_seconds`, `duration_seconds`, `fresh_start`, `characters_present`, `state_in`, `state_out`, `prompt` (EN), `soundscape`, `music="N/A"`, `dialogue[{speaker,lang,text}]` | | `state_in/out` — паспорт непрерывности; `fresh_start` — разрыв цепочки |
| `fps`, `grid` | опц. | информационные, валидатор их не читает |

Три формата выдачи: `GET /api/v1/clips/{id}/file?format=json|md|h3` (`backend/app/api/routes/clips.py:168`).
`format=h3` = `render_h3_payload(finalize_scenario(...))` (`backend/app/domain/clip/h3_export.py:60`):
ровно `{scenario_scenes[], style_block}`, на сцену 8 ключей `tag,start,end,duration,prompt,fresh_start,state_in,state_out`;
`prompt` собирается из трёх лейблов `integrated_multimodal_description:` / `overall_soundscape:` /
`non_diegetic_music:` (`h3_export.py:42`). Целевой приёмник — `PUT /api/projects/<id>/scenario`
H3 (ingest-контракт `docs/reports/2026-09-03-h3-ingest-contract.md`).
Markdown-рендер для человека — `backend/app/domain/clip/markdown_render.py`.

**Статус выдачи в H3:** файл `.h3.json` сформирован и проверен по всем 7 правилам
`_validate_scenario_scenes` (К4 PASS, `docs/reports/2026-09-14-clip-h3-live-acceptance.md:108`),
но **реальный `PUT /scenario` в H3 не выполнялся** (К5, там же `:129`, «решение и время владельца»).
Т.е. совместимость доказана статически, не прогоном.

ADR: **128** (клип, доп.1–6), 054/057/058/059 (иллюстрации), 039 (scene cards), 064 (concept-first),
068 (арки отношений), 092/094/098/101/102 (режиссёрские рычаги), 104 (MCP), 087 (agent runner),
081 (write-contention), 107 (честность статусов). Контракт границы клипа:
`docs/architecture/74-clip-scenario-contract.md`; формат-спека: `specs/SPEC-scene-prompt-structure.md`
(237 строк, 10 разделов, каждое правило привязано к дефекту ночей 1–6 «Колыбельной»).

## 2. LLM-слой

| Что | Как устроено | Где |
|---|---|---|
| Провайдеры | Только OpenAI-совместимый клиент (AsyncOpenAI) по `base_url`: боевой — CAILA (Opus 4.7 / Sonnet), также локальный llama.cpp (мультимодальная gemma проверена, ADR-058). Токены провайдеров шифруются Fernet (ADR-026/124). Anthropic-ветка параметров (`max_tokens` vs `max_completion_tokens`) | `backend/app/integrations/llm/openai_compatible_client.py` (1309 стр.), `base.py:~70` интерфейс `LLMClient.complete_json` |
| Структурный вывод | `response_format: json_object` (для caila.io опускается), JSON-схема из Pydantic передаётся, парсинг `json-repair`, до 2 repair-попыток цикла `Agent.run`, `extra="forbid"` на схемах | `backend/app/agents/base.py:227` (класс `Agent`, `max_repair_attempts=2`), `openai_compatible_client.py:644-661` |
| Агенты | 74 файла в `backend/app/agents/` + 74 шаблона `backend/app/agents/prompts/*.md`; роль → профиль модели через AgentBinding проекта → default проекта → глобальный | `backend/app/services/agent_runner.py:267` `run_agent` (единственная санкционированная обвязка, ADR-087) |
| Промпт-шаблоны | Markdown + `{{ var }}` + `<!-- if:x -->` блоки, рендер `app.agents.prompts.renderer` | `backend/app/agents/prompts/` |
| Конструкции для сценариста | `story_concept`, `story_architect`, `world_builder`, `character_generator`, `chapter_planner` (Scene Cards), `chapter_writer`, `continuity_verifier`, `canon_extractor`, `scene_card_*`, 40+ оценщиков качества (tension, plot_grip, joy_beats, craft pairwise judge) | `backend/app/agents/*.py` |
| Агенты клипа (3 шт.) | `clip_director` (стадия 1, `backend/app/agents/clip_director.py:43`, промпт `prompts/clip_director.md` 124 стр.), `clip_scene_prompt_builder` (стадия 2, `clip_scene_prompt_builder.py`, промпт 148 стр.), `clip_continuity_critic` (advisory, `clip_continuity_critic.py`, промпт 24 стр.) | workflow `backend/app/workflows/clip_scenario_workflow.py:306` |
| Фон | ARQ + Redis, `WorkflowRun`, статусы queued/running/completed/failed | `backend/app/jobs/`, `docs/BT/15-workflow-state-machine.md` §9a |

### Консистентность персонажей (как это сделано)
1. Канон-блок: `_clip_canon_block` (`clip_scenario_workflow.py:180`) берёт `appearance` и `portrait_json`
   (`APPEARANCE_PORTRAIT_FIELDS`) только присутствующих в главе (по `characters_involved` Scene Cards).
2. Режиссёр делает по канону дословную EN `portrait_phrase` + `latin_name` на персонажа (`clip_director.md:54-66`).
3. Код проверяет вклеиваемые поля (`backend/app/domain/clip/pasted_fields.py`, 215 стр.) и делает
   точечный repair — отрицания в портрете/библии («без бороды») размножаются на весь клип (ADR-128 доп.5, доп.6).
4. Валидатор классов 4/5 (`backend/app/domain/clip/validator.py:156` `validate_scenario`): отсутствующие
   персонажи не названы ни кириллицей, ни латиницей; библия и портрет дословно в `prompt`.
5. Паспорт состояний: `derive_passport` (`backend/app/domain/clip/normalize.py:501`) КОДОМ переписывает
   `state_in` каждой не-`fresh_start` сцены значением `state_out` предыдущей — не доверяет LLM.
6. Таймлайн сводит код, не LLM: `normalize_timeline` (`normalize.py:424`), число сцен и число «ударов»
   считает код (`target_scene_count :215`, `target_strike_count :258`) и подаёт в промпт — это
   результат трёх живых итераций ритма (ADR-128 доп.2–4).

## 3. Иллюстрации

| Что | Состояние | Где |
|---|---|---|
| Студия промптов (ADR-054/057) | Работает: выделил текст → N концептов (`illustration_concept`) → диалог-доводка (`illustration_prompt_builder`) → `prompt` + `params_text`. Стиль на проект (`IllustrationProfile`: style/template/rules/`zimage_params_text`, `image_model_name`), канон героев/локаций вплетается (`backend/app/services/illustration_canon.py`) | `backend/app/agents/illustration_*.py`, `backend/app/services/illustration_service.py`, `backend/app/db/models/illustration.py:14-78` |
| Z-Image | **Только текст.** Приложение НЕ вызывает Z-Image/любой генератор картинок; «Z-Image-параметры свободным текстом» (ADR-054 «Оговорки»). Автор генерит вовне и импортирует. Упоминания в коде: `illustration_defaults.py`, `illustration_prompt_builder.md`, `schemas/illustration.py` | grep `zimage` по `backend/app` |
| Импорт картинок (ADR-058) | Работает: загрузка → `Asset`/`IllustrationAsset` → «в книгу» (`TextIllustrationInsertion`) → экспорт (MD zip, PDF, EPUB) → галерея | `backend/app/db/models/illustration.py:49-77`, `GET /illustration-assets/{id}/file` |
| Vision-проверка (ADR-058 2.2c) | Работает: `illustration_reviewer` сравнивает картинку с промптом; `complete_json_vision` | `backend/app/agents/illustration_reviewer.py` |
| Портрет персонажа из картинки | `Character.portrait_asset_id`, `portrait_crop_json` (ручной выбор) | `character.py` |
| Устаревание при правке текста (ADR-130) | Работает и для иллюстраций, и для клипов | `ClipScenarioRepository.mark_outdated_for_chapters` |

Принцип одинаков для картинок и видео (ADR-128 Решение 1): **граница производит текст/JSON, генерацию
делает внешняя система**; клиент видеогенерации — «смена предмета границы» (учёт стоимости, лимиты, хранение).
Это согласуется с нашим планом (студия исполняет), но значит: AW не знает про статус рендера, не получает
готовые кадры/видео обратно, не умеет связать сцену с отрендеренным клипом.

## 4. Стек, API, зрелость

| Параметр | Факт |
|---|---|
| Бэкенд | Python 3.12, FastAPI (`>=0.143`), SQLAlchemy 2.1 + aiosqlite (SQLite WAL, `.data/app.db`), Alembic (мигр. ≥0124 для клипа), ARQ + Redis, Pydantic 2, `mcp>=2.3` (`backend/requirements.txt`) |
| Фронт | React 19.3 + react-router 7 + TanStack Query + Radix/Tailwind, Vite 8, 637 vitest-тестов (по сообщению коммита `a1d19f24`); вкладка «Клип» `ClipsTab.jsx`, `ClipConversation.jsx` |
| HTTP API | 141+ эндпоинтов под `/api/v1`, сессия-cookie (itsdangerous, ADR-022), контракт `docs/BT/13-api-contracts.md` (клип: §21a, 8 эндпоинтов). Клип: `GET/POST /chapters/{id}/clips`, `POST /clips/{id}/generate` (202 + `workflow_run_id`), `/refine`, `PATCH /clips/{id}`, `GET /clips/{id}/file`, `DELETE` (`backend/app/api/routes/clips.py:51-217`) |
| MCP | Embedded `/mcp` (FastMCP) в том же процессе, fail-closed bearer `MCP_AUTH_TOKEN` (без токена `/mcp` не монтируется), 49 тулов; 6 клип-тулов `clip_create/generate/status/get_scenario(format=h3)/refine/approve` (`backend/app/mcp_server/tools/clip.py:24-179`); остальные: проекты, концепты, автопилот, чтение глав, экспорт. Подключение внешних агентов: `docs/HOWTO-connect-agents.md` |
| Вызов снаружи | Возможно двумя путями: MCP по HTTP (`http://host:8000/mcp`, bearer) или REST с логином. Асинхронная генерация (фон ARQ): надо опрашивать `clip_status`/`workflow_runs`. Нужны запущенные uvicorn + arq + redis и настроенный профиль модели у проекта |
| Тесты | `backend/tests`: 648 файлов, около 4857 `def test_` (grep, параметризация даст больше); по клипу — 177 тестов в `backend/tests/unit/clip`, `integration/test_clip_routes.py`, `test_mcp_clip_tools.py`; golden-тест промпта режиссёра; контрактные сторожа границ (`docs/architecture/boundaries.yaml`, 71 граница) |
| Зрелость клипа | В main с 2026-09-02, шесть доработок до 2026-09-25 (60 коммитов со словом clip). Живые замеры на реальной главе «Озеро» (75 с, 14 сцен): блокирующих 0, repair 0, ритм CV 0.22–0.25 (PASS), поля вклеиваемые PASS N=3. Откат: позитивные портреты (FAIL N=3, `revert`) — механизм отката дисциплинирован. Процесс: SDD (спека→план→субагенты→ревью), каждый ADR оплачен замером |
| Риски | Работа ведётся одним владельцем; правило №1 требует обновлять спеки в каждом коммите (высокий порог входа для чужих правок); SQLite + single-user (IDOR принят осознанно, ADR-058); код жёстко русскоязычный в промптах агентов, EN только в полях промптов видеомодели |

## 5. Вывод: что брать для «режиссёра» студии

### Выбор из (а) сервис / (б) перенос модулей / (в) идеи

| Компонент AW | Вариант | Обоснование |
|---|---|---|
| Идея → концепт → арки → персонажи → главы → Scene Cards (писательский конвейер) | **(а) вызывать как сервис через MCP/REST** | Тяжёлый, на 74 агента, SQLite+ARQ+Redis, свои спеки и границы; переносить нечего, переписывать дороже. Нам нужен его выход (сценарий/персонажи), не внутренности |
| Двухстадийный клип-режиссёр + нормализатор + валидатор + экспорт H3 | **(а) сервис, при необходимости (б) точечно** | Домен `backend/app/domain/clip/*` (≈1650 строк) — чистый Python без БД: `normalize.py`, `validator.py`, `h3_export.py`, `pasted_fields.py`, `markdown_render.py`. Переносим как библиотеку, если студия будет работать БЕЗ книги (идея → клип) |
| Формат-спека `specs/SPEC-scene-prompt-structure.md` | **(в) идеи/правила целиком** | Это и наш документ (выстрадан на H3); уже содержит паспорт состояний, fresh_start, запрет негатива, свет-глаголы, счёт ≤3 |
| Канон персонажей: portrait_json + дословные portrait_phrase + проверка кодом | **(б) перенести идею и `pasted_fields.py`** | Переиспользуемо для картинок Krea 2 и видео |
| Принцип «код считает арифметику и число сцен/ударов, LLM только содержание» | **(в) идея** | Подтверждён тремя замерами ритма |
| Студия иллюстраций (концепты → диалог-доводка → промпт) | **(в) идея + (а)** | Свободный текст Z-Image, автор сам генерит; нам нужна автоматическая отправка в Krea 2/H3 — этого в AW нет |
| Агентный каркас `Agent`/`run_agent`/json-repair/профили моделей | **(в) идея** | Хорошо сделан, но завязан на БД проекта и workflow-run |

### Рекомендация
Один быстрый путь: **вызывать AW как сервис через MCP** (`clip_create → clip_generate → clip_status →
clip_get_scenario(format="h3")`) для режима «глава романа → клип», **параллельно вынести в студию
как библиотеку `domain/clip`** для режима «идея → клип», где главы нет. Для режима «идея → сценарий
без книги» MCP-путь обязывает завести проект и главу с текстом (`ValidationError`, если у главы нет версии:
`clip_scenario_workflow.py:~130`) — это обходной крюк.

### Пробелы до видео-сценария студии

| Нужное студии | Что в AW | Разрыв |
|---|---|---|
| Сцена 3–15 с | Жёстко 5–10 с: `SCENE_MIN_SECONDS=5.0`, `SCENE_MAX_SECONDS=10.0` (`normalize.py:29-30`), схема режиссёра `ge=5, le=10` (`clip_director.py:28`), расчёт числа сцен ⌈T/10⌉..⌊T/5⌋ | Менять константы + схему + промпты + ритм-зоны «удар 5–6 / выдержка 9–10» (`clip_director.md:70-85`); границы 3–15 вынести в данные профиля модели. Верхняя граница 10 с — правило H3 «цикл дублируется» (SPEC §1), 15 с требует проверки на H3 |
| Режим сцены (i2v / flf / t2v) | Нет: `mode:` запрещён в `prompt`, режим ставит H3-конвейер (`clip_scene_prompt_builder.md:44`); есть только `fresh_start` | Добавить поле `mode` в `ClipScene`, но H3 ingest его не принимает (8 ключей) — зависит от нашего API |
| Эмоция и игра актёра | В прозе есть (`emotional_turn`, `subtext`, `speech_modes`, `physical_business` в Scene Card, `chapter_planner.py:166+`), но до режиссёра клипа доезжают только `entry/exit_state` и `beats[trigger→visible_action]` (`clip_scenario_workflow.py:81-157`) | Прокинуть эмоцию/подтекст/мимику в `dramatic_content` и в поле сцены `performance` (микро-игра: взгляд, пауза, жест) |
| Звук через походку/материал | `soundscape` (EN, диегетический звук) есть, правил физического описания нет: промпт сборщика просит «шаги, ветер, скрип» (`clip_scene_prompt_builder.md:91-92`) | Вписать правило из нашей памяти «писать материал и контакт, а не „bare footsteps“; указывать, что НЕ звучит» и пробу звука походки (`docs/h3-modes`) в `soundscape` |
| Промпт под H3 | Есть (формат H3 ingest, три лейбла, speech-тег `<d>[lang]…</d>`) | Закрыто, но без живого `PUT` |
| Промпт под Krea 2 (картинки/кейфреймы) | Нет вовсе (`grep -i krea` по AW пуст); картинки только как текст «Z-Image params» | Новый агент/шаблон: кейфрейм-промпт на сцену из `style_bible` + `portrait_phrase` + `state_in` |
| Обратная связь рендера | Нет: AW не знает про выполнение, не получает видео/кадры; vision-ревью есть только для иллюстраций | Нужна петля «сцена отрендерена → оценка → доводка сцены»; `illustration_reviewer` — прототип |
| Несколько глав / книга | Только одна глава на запись | Нужен вышестоящий слой (серия клипов, сквозные персонажи) |
| Идея → сценарий без книги | Нет пути без главы | Либо фиктивная глава, либо перенос `domain/clip` и вход «идея/логлайн» |
| Музыкальный клип | Отложен владельцем (ADR-128 Решение 6, BACKLOG) | Отдельная волна |
| Живая приёмка с H3 | `PUT /scenario` не выполнялся (К5) | Первым делом прогнать `*.h3.json` из `docs/reports/2026-09-14-clip-live-acceptance-ozero.h3.json` на нашем H3 |

## 6. Итог для брифа (10 строк)

1. В AW есть готовая граница «Клип-сценарий» (ADR-128): `clip_director` → `clip_scene_prompt_builder` → код-нормализатор → валидатор 8 классов → `clip_continuity_critic`, экспорт `format=h3`; в main, 60 коммитов, 177 тестов клипа.
2. Формат уже H3-родной: паспорт состояний, `fresh_start`, три лейбла `prompt`, speech-тег; оси «персонаж/арка» полные (CharacterArc, портрет-фразы, канон-блок).
3. LLM-слой: только OpenAI-совместимые (CAILA, llama.cpp), `json_object` + Pydantic + json-repair, 74 агента, роль→профиль модели; всё через `run_agent`.
4. Иллюстрации: студия готова (концепты, диалог-доводка, импорт, vision-ревью), но Z-Image только текст — генератора не вызывает; Krea 2 в AW нет.
5. Снаружи вызывается: MCP `/mcp` (bearer, 49 тулов, 6 клип-тулов) и REST `/api/v1`; фон ARQ+Redis, SQLite; нужны запущенные uvicorn+arq+redis и профиль модели.
6. Зрелость высокая в клипе: ритм, портреты, вклеиваемые поля прошли живые замеры N=3 на «Озере» (PASS, 0 блокирующих); есть дисциплинированные откаты.
7. Не проверено: реальный `PUT /scenario` в H3 не делали (К5) — совместимость доказана только статически.
8. Рекомендация: (а) вызывать AW как сервис для «глава → клип»; (б) вынести `backend/app/domain/clip/*` библиотекой для «идея → клип»; (в) формат-спека и принцип «код считает арифметику» — как правила студии.
9. Пробелы: сцены жёстко 5–10 с (константы `normalize.py:29-30`), нет режима сцены, эмоция/игра не доезжают до клипа, звук не про материал/походку, нет Krea 2 и петли рендера, привязка только к одной главе.
10. Риск: тяжёлый стек и правило «спека в каждом коммите» делают чужие правки в AW дорогими; для студии безопаснее держать свой тонкий слой над сервисом.
