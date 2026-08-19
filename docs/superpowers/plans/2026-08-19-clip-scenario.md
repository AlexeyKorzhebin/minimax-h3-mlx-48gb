# Сюжет клипа — план

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Произвольная mp3 → авто-лирика Whisper'ом → LLM-сценарий по секциям → гейт
согласования/правки сцен человеком → генерация. Спека: docs/superpowers/specs/2026-08-19-clip-scenario-design.md (читать целиком).

## Global Constraints

- Чекаут: /Users/aleksey.korzhebin/Yandex.Disk.localized/Projects/minimax-h3-mlx-48gb, ветка main.
  Питон `env -u NODE_OPTIONS .venv/bin/python`; тесты полным прогоном В ФОРГРАУНДЕ одним
  Bash-вызовом С ЯВНЫМ timeout 600000 (0 failed) перед каждым коммитом. Коммиты по-русски, НЕ пушить.
- Боевой сервер 8765 не трогать; GPU/LLM в тестах не запускать (Music3/Whisper/llama мокаются;
  ffmpeg-тесты на lavfi можно). Никаких фоновых ожиданий/Monitor.
- Гейты в стиле волны Проектов: побочный эффект ДО approve_stage; только локированные методы
  Project; note-индекс/job_id-авторитет не трогать.
- Правила сценария (KNOWLEDGE §2/§4, нарушение = дефект): чистые структурные теги; без поющих
  крупным планом лиц; visual bible дословно в каждой сцене; аудио-негативы не протекают в
  видео-промпты; длительности сцен 5–10 с в схеме С границами.

## Task 1: авто-лирика в align-пути (songrun + project)

Modify: h3_48gb/songrun.py, h3_48gb/project.py, tests/test_songrun.py, tests/test_project.py.
align_track получает режим без reference-лирики: lyrics=None → обычная транскрипция
(тот же mlx_whisper вызов), возврат raw_segments ([{start,end,text}]) и transcript (текст);
sections тогда НЕ строятся (пусто), undersung=False. _TRACK_FIELDS += lyrics_auto (str|None),
raw_segments (list|None). Воркер (worker.py) пишет их через update_track для import-пути без
лирики. web.py POST /api/projects: clip с track_source=import и БЕЗ lyrics — теперь валиден
(раньше 400) — гейт script approve'ится с пустой лирикой для этого случая (посмотри текущую
валидацию и ослабь точечно). Тесты: align без лирики (мок whisper), submit clip без лирики.

## Task 2: SCENARIO_SCHEMA и системный промпт сценария

Modify: h3_48gb/provider.py, docs/h3-prompt-system.md, tests/test_provider.py.
Новая SCENARIO_SCHEMA (отдельная от PROMPT_SCHEMA): {reply, scenario: {sections:
[{tag, start, end, scene: {prompt, duration}}], style_block}} — duration с minimum 5/maximum 10;
start/end числа. Новый раздел системного промпта «Clip scenario mode» (документ уже несёт
правила Music3 — дополни): вход лирика ИЛИ raw-транскрипт с таймстампами + caption; выход —
секции с границами, покрывающими 0→duration, по сцене на секцию; правила: без поющих
крупным планом лиц (липсинк невозможен — объясни модели почему), visual bible (style_block)
дословно, полный H3-формат промпта сцены, аудио-негативы не включать, образы из СМЫСЛА строк
секции. provider.chat_scenario(...) — вызов с этой схемой (по образцу существующего chat()).
Тесты: схема валидирует/отвергает (jsonschema), промпт несёт якорные правила.

## Task 3: этап scenario в project.json и гейт-механика

Modify: h3_48gb/project.py, h3_48gb/web.py (build_clip_scenes путь), tests/.
stages.scenario (kind=clip only; чтение старых project.json без поля → "approved" —
миграция «этап пройден», существующие проекты не ломаются). Новые данные: scenario_scenes
([{tag,start,end,prompt,duration}], редактируемые), update_scenario(...) локированный.
build_clip_scenes: режим from_scenario (утверждённые сцены → та же coverage-нарезка: снап
на сетку 17n+5, гэпы, style_block из сценария) и прежний процедурный как fallback. Тесты:
миграция, from_scenario нарезка (покрытие/сетка/границы), локированность.

## Task 4: веб-API сценария

Modify: h3_48gb/web.py, tests/test_web_projects.py.
POST /api/projects/<id>/scenario/generate: гейт (kind=clip, track approved, scenario
draft/awaiting_approval) → собрать вход (lyrics или lyrics_auto+raw_segments, caption,
duration) → provider.chat_scenario через существующую механику провайдера (ensure_up;
ошибки провайдера — существующие коды) → валидация покрытия секций → update_scenario +
stages.scenario=awaiting_approval (побочный эффект до approve!). PUT /api/projects/<id>/scenario:
правки сцен (prompt/duration в границах), 409 после утверждения. POST approve/scenario:
build_clip_scenes(from_scenario) → старт первой сцены → approve_stage (в конце). Кнопка
«сюжет без LLM»: POST scenario/generate {"procedural": true} — прежний синтез, тот же гейт.
approve/track для clip больше НЕ строит сцены (только переводит к этапу scenario). Тесты:
жизненный цикл с мок-LLM, гейты, правки, 409, procedural-путь, чинимость после упавшего LLM.

## Task 5: UI сюжета

Modify: h3_48gb/webui/index.html, app.js, style.css (+смоук на 18765).
Панель проекта: этап «Сюжет» между треком и сценами (kind=clip): кнопки «Сгенерировать сюжет» /
«Сюжет без LLM»; список сцен сюжета с редактируемыми textarea промптов и длительностью
(PUT при blur/кнопке «Сохранить»), тег+тайминг секции подписью; «Утвердить сюжет» с confirm;
после утверждения — read-only. Плашка про LLM (ensure_up может поднимать модель — существующий
паттерн чата). Обе темы, 360px. Смоук: сервер на 18765, mock-LLM недоступен → generate отдаёт
честную ошибку, procedural-путь проходит до сцен в очереди (воркер не запускать).

## Task 6: закрепление

README (маршруты сценария + авто-лирика), BACKLOG (волна реализована), KNOWLEDGE §4 (правила
сценария, контракты). Полный прогон. НЕ пушить, НЕ мержить (контроллер).

## Ворота (вне плана, контроллер, GPU): по спеке — suno-mp3 без лирики до финала с правкой сцены руками.
