# Срез A: open-source «AI filmmaking studio» (09.10.2026)

Метод: WebSearch + GitHub API (`pushed_at`, коммиты, релизы, контрибьюторы) + README. «Последний коммит» взят из `commits`
API там, где указано; иначе это `pushed_at` (помечено «push»). Звёзды/форки на 09.10.2026. Всё, чего не удалось
подтвердить по первоисточнику, помечено «не проверено».

## Эталон: AI Movie Studio 2 (Heroesjouney/AIMovieStudiov2)

- 169 звёзд, 29 форков, 0 открытых issues, AGPL-3.0 (плюс коммерческая лицензия). Создан 2026-08-05.
- Последний коммит в ветке main: 2026-09-16 («September 15 update»); push в репо 2026-09-29 (вероятно, ветка `Errors-and-Bugs`).
  Ритм: 102 коммита за ~6 недель, потом тишина 3+ недели.
- Контрибьюторы: Heroesjouney 98, ghzgod 2, edasque 1 — по сути один автор. Релизов нет. Форки-копии под другими
  аккаунтами (edasque, benmaozsima) — не путать с оригиналом.
- Архитектура: Next.js 14 + React Three Fiber + Zustand; FastAPI; драйверы ComfyUI (локально) / Fal / Replicate.
  REST `/api/generate/*`, `/api/settings/*`, Swagger `/docs` на 8001. MCP и LLM нет.
- Модели: Z-Image, Qwen Image, Flux 2 (+Kontext), Krea 2; видео LTX 2.3, Wan, MiniMax H3, Seedance; звук Fish Speech, Chatterbox,
  HunyuanVideo Foley. LoRA есть. 3D-раскадровка, импорт Fountain/FDX, таймлайн с XML-экспортом.
- Вывод: живой, но молодой, один автор, остановился в сентябре. Единственный в срезе, кто сразу покрывает H3 + Krea 2 + Z-Image +
  LTX + Wan на ComfyUI.

## Таблица кандидатов (13 локальных + 3 для идей)

| # | Проект | Звёзды | Лиц. | Последний коммит | Исполнитель | LLM | MCP/API | Модели | Кратко |
|---|---|---|---|---|---|---|---|---|---|
| 0 | AI Movie Studio 2 (эталон) | 169 | AGPL | 2026-09-16 (push 09-29) | ComfyUI, Fal, Replicate | нет | REST | H3, Krea2, Z-Image, LTX, Wan | 3D-раскадровка, таймлайн |
| 1 | Inline Studio / OmniChar (inlineresearch) | 513 | GPL-3 | 2026-10-09 | свой Inline Core + ComfyUI-нода | не указан | REST `/api`, MCP нет | H3 (+обучение LoRA), Krea2, Z-Image, FLUX.2, LTX-2.5 | нод-канвас, `.char`, takes |
| 2 | Maestro (Blizaine) | 721 | не определена («Other») | 2026-10-05 | WanGP/Pinokio | локальная llama-server + OpenAI-совм. (эксп.) | не найдено | LTX 2.5/2.3, H3, Wan, Krea, Qwen Image 2.1 | Director/Studio/Editor |
| 3 | Vivijure (skyphusion-labs) | 7 | AGPL | 2026-10-08 | свои модули, RunPod/свой GPU/облако | Discord-бот slate | MCP-репо vivijure-mcp | SDXL, Wan 2.2, LTX, CogVideoX | модульный хост, раскадровка, LoRA героя |
| 4 | Toonflow (HBAI-Ltd) | 16 963 | MIT | 2026-10-09 | API провайдеров + ComfyUI + локальный LLM | да (агенты) | MCP, A2A | Seedance, GPT Image (в примерах) | канвас, агенты, 20+ релизов, v2.0.5 |
| 5 | Velorn/ComfyStudio (VelornLabs) | 500 | GPL-3 | push 2026-09-26 | ComfyUI (только loopback) | LM Studio | MCP 100+ инструментов | LTX 2.3, Wan 2.2, Qwen Edit | таймлайн-редактор, Linux AppImage |
| 6 | OpenMontage (calesthio) | 65 745 | AGPL | push 2026-10-03 | агентный (Claude Code и т.п.), 12 пайплайнов | через хост | через агента | не проверено | агентная видеопродукция, не студия-UI |
| 7 | openOii (Xeron2000) | 405 | нет | push 2026-09-27 | не указан | LangGraph | WebSocket | не указаны | демо/учебный, автор: «не для продакшна» |
| 8 | wind-comic (ChrisChen667788) | 670 | MIT | push 2026-10-09 | OpenAI/Claude/MJ/Veo/fal/ComfyUI | да, мультиагент | не проверено | облачные | one-line → драма; требует LLM 24B+ |
| 9 | Mix AI Cinema Studio | 0 | нет | push 2026-10-09 | ComfyUI + Ollama | Ollama | нет | Z-Image Turbo, Qwen-Edit, LTX-2.3 | 28 коммитов, создан 10-04 |
| 10 | ai-film-director (BitraAI) | 2 | нет | push 2026-08-26 | пользовательские графы ComfyUI | OpenCode-агенты | нет | Krea2/FLUX.2/Qwen → LTX-2.5/H3 | пайплайн YAML-схем; форк simodev25 |
| 11 | ComfyAgent (IvenKooLab) | 28 | MIT | push 2026-09-16 | ComfyUI localhost, Windows | LLM-перевод промптов | MCP (4 тула) | шаблоны ComfyUI | эпизодный конвейер, LoRA героя |
| 12 | OpenX-Inc/flow | 20 | MIT | 2026-07-23 | Wan 2.2 | LLM + OpenRouter | нет | Wan 2.2 | тема → сценарий → клипы, цепочка по последнему кадру |
| 13 | Pallaidium (tin2tin) | 1 543 | GPL-3 | push 2026-09-20 | Blender VSE | нет | нет | широкий | аддон Blender с 2023 |
| 14 | the-halleen-machine | 19 | AGPL | push 2026-07-17 | ComfyUI | нет | нет | — | таймлайны, библиотеки персонажей/поз |
| 15 | NodeTool | 560 | AGPL | push 2026-10-09 | свой + провайдеры | да | агенты | — | «agent-first workspace», с 2024 |
| 16 | FlixML | 2 | AGPL | push 2026-10-03 | ComfyUI-native | — | API-first | — | проекты/сцены/шоты, мёртво по звёздам |

Только облако / для идей (не проверялись глубоко): Storyboard Creator AI (iOS), Muse Studio Wan 2.2 (Gumroad, платный), LTX Studio, Google Flow (в брифе
Vivijure и flow названы «аналогами» этих).
Не найдено в ходе поиска: «ChangJi» (упомянут в одной выдаче, существование не подтверждено), «TakeBoard» — нашёлся лишь в awesome-списке
(Fourques/Takeboard), не проверял.

## Топ-5 (кроме эталона)

Критерий отбора: близость к нашей связке (H3 + ComfyUI/свой исполнитель, слой проекта/героев), живость, зрелость.

### 1. Inline Studio / OmniChar — inlineresearch/Inline-Studio
- Зрелость: 513 звёзд, 72 форка, 300 коммитов, GPL-3.0, создан 2026-06-11, последний коммит 2026-10-09 (README),
  до этого 10-07 «читать чекпойнт MiniMax H3 по диапазону байт», «nvfp4 text encoder для обучения H3». Контрибьюторы: imprsnst 295
  из ~299 — один основной автор. Версии пакетов omnichar-core 1.3.16 / frontend 1.3.15. Открытых issues 0.
- Исполнитель: свой движок Inline Core + отдельная нода ComfyUI-Omnichar; адрес чужого ComfyUI в README не описан.
- Слой проекта: персонаж как переносимый `.char` из референсных фото, оценка непрерывности (continuity score) каждого дубля,
  дубли (takes) на кадре, экспорт проекта zip, расширения из GitHub. Явных «сцен» и «таймлайна» нет — есть Video Director/Trim ноды.
- Модели: **H3 (включая обучение LoRA)**, Krea 2, Z-Image Turbo, FLUX.1/2, LTX-2.5. Wan не упомянут. LoRA обучается локально
  на картинках/клипах. Требования: Linux + NVIDIA протестирован; Krea 2 и обучение — ~40–48 ГБ (L40S), т.е. наша 64 ГБ карта подходит.
- LLM: нет. MCP: нет. API: REST `/api` без авторизации.
- Роль для нас: **самый близкий по моделям** (H3 + Krea 2 + Z-Image + LTX 2.5 + обучение LoRA героя). Брать как источник идей
  (`.char`, continuity score, takes) и/или как основу слоя персонажей; как целое не покрывает сценарий/сцены/LLM.

### 2. Maestro — Blizaine/Maestro
- Зрелость: 721 звезда, 130 форков, 64 открытых issues, создан 2026-07-08, коммиты 2026-10-05 (v2.6.0 релиз 10-04). Один основной автор
  по свежим коммитам. Лицензия: файл LICENSE есть, SPDX не определён, плюс THIRD_PARTY_NOTICES (часть моделей — некоммерческие) — нужно
  читать вручную.
- Исполнитель: WanGP (через Pinokio), не ComfyUI. Подключить свой ComfyUI/sglang нельзя (не найдено).
- Слой проекта: режим Director (LLM-план: шот-план, сценарий, сцены, непрерывность героев, сохранение/перерендер проектов), Studio, Editor
  с мультитрековым таймлайном.
- Модели: LTX 2.5/2.3, **H3**, Wan, Hunyuan, Flux 2 Klein, Krea, Qwen Image 2.1, музыка ACE-Step/YuE2/MiniMax-Music3, TTS. Импорт «community H3 checkpoint».
- LLM: встроенная llama-server (Gemma 4 4B по умолчанию, Qwen3.6 27B), OpenAI/Anthropic/OpenAI-совместимые — экспериментально.
- Платформы: Windows, Linux CUDA; Mac не найден. API/MCP в видимой части README не найдены (README прочитан на 100 000 из 150 943 знаков).
- Роль: **единственный с LLM-режиссёром + H3**; но другой исполнитель и неясная лицензия. Только как образец UX Director-режима.

### 3. Vivijure — skyphusion-labs/vivijure
- Зрелость: 7 звёзд (мало внимания), но 733 коммита, релиз v1.0.0 2026-07-13 (до того v0.6.x 06-24), последний коммит 2026-10-08,
  несколько авторов-аккаунтов (команда Skyphusion). AGPL-3.0. Репо-инфраструктура зрелая (теги релизов, capability-матрица, документация).
- Архитектура: «тонкий хост модулей»: типизированный контракт, UI собирается из `GET /api/modules`; два рантайма — Cloudflare Workers
  или Node + SQLite + S3/MinIO (vivijure-local). Ядро владеет проектом, раскадровкой и кастом.
- Исполнитель: свои GPU-бэкенды (RunPod, своя машина, облачные i2v), ComfyUI не упомянут — но модульность допускает свой модуль.
- Модели: SDXL-кадры, Wan 2.2 A14B, LTX-Video, CogVideoX, Kling/Seedance/Hailuo/Veo/Vidu; RIFE, Real-ESRGAN; LoRA героя из портретов
  кастинга. H3/Krea 2/Z-Image не заявлены.
- LLM: Discord-бот-сценарист slate; детали не подтверждены. MCP: отдельный `vivijure-mcp` (планирование, каст, рендер, опрос) —
  точно в духе нашего видения. Продолжение от последнего кадра: не найдено.
- Роль: **эталон архитектуры «модули + контракт + MCP»**; можно взять как основу идей, но стек (Cloudflare/TS) чужой, моделей наших нет.

### 4. Toonflow — HBAI-Ltd/Toonflow-app
- Зрелость: самый «массовый»: 16 963 звезды, 3 007 форков, MIT, создан 2026-01-29, последний коммит 2026-10-09 (несколько в день),
  релиз v2.0.5 2026-10-09 (до этого 10-05, 10-01). Коммиты — один основной автор (ACT-Meteor) в сэмпле; в README 21 язык, CONTRIBUTING,
  AGENTS.md. Оговорка: в README спонсорская реклама и собственный релей, цифры «800+ коммитов, 20 версий» из письма команды.
- Слой проекта: канвас со сценарием, героями, сценами, видеоклипами; 3D-режиссёрская студия; раскадровка; таймлайн — только в
  дорожной карте. Desktop (Win/macOS), Android, Docker, Linux-сервер (Bun).
- Исполнитель: API провайдеров; README заявляет подключение локального ComfyUI и локальных LLM (адрес/модели — проверить в коде).
- LLM: агенты (Opus/GPT в примерах), «открытый агентный слой» и A2A. MCP: да (для внешних инструментов). Порт 3000 без логина.
- Модели: в примерах облачные (Seedance 2.0, GPT Image 2); H3/Krea/Z-Image/LTX/Wan локально — не подтверждено.
- Роль: **единственный зрелый MIT-проект с LLM-агентами, MCP и слоем героев/сцен**; кандидат «как основа», если готовы
  дописать драйвер под наш ComfyUI/sglang. Прямая стыковка с H3 не проверена.

### 5. Velorn (ранее ComfyStudio) — VelornLabs/velorn
- Зрелость: 500 звёзд, 70 форков, 27 открытых issues, GPL-3.0, создан 2026-03-08, push 2026-09-26, 421 коммит. Установщики Win/macOS/Linux
  (AppImage, deb).
- Исполнитель: ComfyUI, **только localhost/loopback** (в десктоп-приложении) — для нашего удалённого alex-neuro нужен SSH-туннель.
- Слой проекта: проекты, мультитрековый таймлайн с трим/переходами/кейфреймами, шот-сборка музыкальных клипов, бета-режим «short film».
  Сцен и дублей как сущностей нет.
- Модели: собственные workflow JSON (LTX 2.3, Wan 2.2, Qwen Image Edit). H3/Krea 2/Z-Image — не заявлены.
- LLM: локальный LM Studio-ассистент. MCP: **локальный сервер, 100+ инструментов**, режим «preview-first» для записи — лучший в срезе по
  агентному управлению таймлайном.
- Роль: как кусок — идея MCP-управления таймлайном и undo-политики; как основа не подходит (редактор, а не студия сценариев).

## Остальное коротко
- OpenMontage (65 745 звёзд, AGPL, push 10-03): не студия, а набор пайплайнов/скиллов для кодинг-агентов. Для нас — образец «агент как оператор».
  Содержимое глубоко не разбирал.
- wind-comic (MIT, 670, push 10-09) и openOii: мультиагентные «漫剧»-конвейеры на облачных API; ComfyUI у wind-comic в списке провайдеров. Не наш стек.
- mixaicinemastudio: ближайший по стеку к нам (ComfyUI + Ollama + LTX-2.3, FastAPI/React, SQLite), но 28 коммитов и 0 звёзд за 5 дней, лицензии нет — прототип.
- ai-film-director: хорошая декомпозиция артефактов (сценарий → персонажи → раскадровка → шотлист → промпты Krea 2/FLUX.2/Qwen → LTX-2.5/H3), YAML-схемы;
  идеи для схемы данных; код — агентные скиллы OpenCode, лицензии нет.

## Вывод по срезу
1. Готового «всё в одном» под наш замысел (H3 + удалённый ComfyUI + LLM-сценарист + MCP + мобильный) нет. AI Movie Studio 2 по моделям и
   ComfyUI остаётся ближайшим, но один автор и пауза с 16 сентября.
2. По частям: персонажи/LoRA/H3 — Inline Studio; LLM-режиссёр — Maestro; модульная архитектура и MCP — Vivijure; агенты/MCP/зрелость — Toonflow;
   MCP-таймлайн — Velorn.
3. Лицензии: AMS2, Vivijure, OpenMontage — AGPL; Inline Studio, Velorn — GPL-3; Toonflow, wind-comic — MIT; у Maestro непонятная.
4. Мобильного доступа и Hermes/OpenClaw-интеграции не нашёл ни у кого; у Toonflow есть Android-APK.

## Ссылки (первоисточники)
- https://github.com/Heroesjouney/AIMovieStudiov2
- https://github.com/inlineresearch/Inline-Studio
- https://github.com/Blizaine/Maestro
- https://github.com/skyphusion-labs/vivijure
- https://github.com/HBAI-Ltd/Toonflow-app
- https://github.com/VelornLabs/velorn
- https://github.com/calesthio/OpenMontage
- https://github.com/mharisali-hash/mixaicinemastudio
- https://github.com/simodev25/ai-film-director (апстрим BitraAI/ai-film-director)
- https://github.com/OpenX-Inc/flow
- https://github.com/ChrisChen667788/wind-comic
- https://github.com/Xeron2000/openOii
- https://github.com/IvenKooLab/comfy-agent
- https://github.com/tin2tin/Pallaidium
- https://github.com/mikehalleen/the-halleen-machine
- https://github.com/nodetool-ai/nodetool
- https://github.com/light-and-ray/awesome-alternative-uis-for-comfyui
- https://comfyui-wiki.com/en/news/2026-09-09-ai-movie-studio-2

Ограничения: Reddit/HN/X поиск выдачи не дал (только агрегаторы); звёзды/даты взяты из GitHub API на 09.10.2026; проекты, где указан push,
а не коммит, — дата последней записи в репо, не обязательно в main.
