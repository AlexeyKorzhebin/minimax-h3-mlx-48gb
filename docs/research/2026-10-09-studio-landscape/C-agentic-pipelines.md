# Срез C: агентные и LLM-управляемые системы создания видео/картинок (09.10.2026)

Метод: GitHub API (`gh api`: звёзды, pushed_at, лицензия, релизы, README, дерево файлов) и WebSearch. «Дата» в таблице — `pushed_at` репозитория на 09.10.2026. Звёзды — на момент запроса. «Не проверено» помечено явно.

Критерии из брифа: 1 локально, 2 исполнитель/ComfyUI по адресу, 3 слой проекта, 4 модели, 5 LLM (OpenAI-совместимый), 6 интеграции (MCP/хосты), 7 зрелость, 8 взять целиком/основа/куски.

## Сводная таблица (22 кандидата)

| # | Проект | Звёзды | Последний push | Лицензия | Локально + свой ComfyUI | OpenAI-совместимый LLM | Вердикт |
|---|--------|-------:|----------------|----------|-------------------------|------------------------|---------|
| 1 | [artokun/comfyui-mcp](https://github.com/artokun/comfyui-mcp) | 802 | 05.10.2026 (v0.52.205, 01.10) | MIT | да, ComfyUI по адресу, LAN/VPS | да (Ollama, любой OpenAI-endpoint) | основа слоя MCP/агент |
| 2 | [HBAI-Ltd/Toonflow-app](https://github.com/HBAI-Ltd/Toonflow-app) | 16964 | 09.10.2026 (v2.0.5, 09.10) | MIT | да, «локальный ComfyUI и LLM» | да (сторонние API + локальный LLM) | основа / источник идей по проекту |
| 3 | [HKUDS/ViMax](https://github.com/HKUDS/ViMax) | 12594 | 30.09.2026 | MIT | нет ComfyUI; генераторы только облачные API | да, `base_url` в YAML | куски (VLM-выбор кадра, консистентность) |
| 4 | [AIDC-AI/Pixelle-Video](https://github.com/AIDC-AI/Pixelle-Video) (ATH-MaaS) | 28803 | 14.06.2026 (v0.1.15, 27.01) | Apache-2.0 | да, ComfyUI-воркфлоу из `workflows/`, Ollama | да | куски (шаблоны воркфлоу), не основа |
| 5 | [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) | 65745 | 03.10.2026 | AGPL-3.0 | частично: локальные Wan2.2/LTX2/Hunyuan через `VIDEO_GEN_LOCAL_*`, не ComfyUI | через хост-агент (Claude Code, Codex, Cursor) | идеи: skills по стадиям, quality gates |
| 6 | [chatfire-AI/huobao-drama](https://github.com/chatfire-AI/huobao-drama) | 15893 | 05.10.2026 (v4.0.8) | CC BY-NC-SA 4.0 (NOASSERTION) | нет ComfyUI; видео через API: Seedance 2.0, MiniMax H3, Wan 3.0 | да (OpenAI-compatible) | идеи (там уже есть H3), лицензия NC |
| 7 | [waooAI/waoowaoo](https://github.com/waooAI/waoowaoo) | 14450 | 21.09.2026 (v0.4.1, апрель) | Elastic License 2.0 с v0.5.0-beta.1 | Docker self-host, но провайдеры облачные | не проверено | только для идей, лицензия |
| 8 | [HITsz-TMG/VideoClaw](https://github.com/HITsz-TMG/VideoClaw) (бывший FilmAgent, репозиторий переименован — судя по редиректу API) | 1846 | 26.08.2026 | MIT | веб + OpenClaw skill; видео Wan/Kling через API, ComfyUI не заявлен | да, плюс отдельные `vlm`, `image_*` | куски: OpenClaw-интеграция, первый/последний кадр |
| 9 | [showlab/MovieAgent](https://github.com/showlab/MovieAgent) | 366 | 26.03.2025 | не указана | исследовательский код | не проверено | мёртвый, только как статья |
| 10 | [joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server) | 407 | 17.02.2026 | Apache-2.0 | да, воркфлоу JSON → MCP-инструменты (`PARAM_*`) | нужен LLM-хост | куски: идея авто-обнаружения воркфлоу |
| 11 | [AIDC-AI/Pixelle-MCP](https://github.com/ATH-MaaS/Pixelle-MCP) | 1127 | 17.12.2025 | MIT | да, ComfyUI→MCP | да | устарел (10 мес.), заменён Pixelle-Video |
| 12 | [Comfy-Org/comfy-skills](https://github.com/Comfy-Org/comfy-skills) + Comfy Cloud MCP | 219 | 08.10.2026 | MIT | только облако Comfy (`cloud.comfy.org/mcp`); локальный MCP — «private» | хост | «для идей», не наш сценарий |
| 13 | Hermes: skill `creative/comfyui` ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)) | 252223 | 09.10.2026 | MIT | да, comfy-cli + REST/WS, локально и облако | модельный слой Hermes | готовый skill для хоста |
| 14 | Hermes: `agent/video_gen_provider.py` (встроенный инструмент video_gen, провайдеры-плагины) | то же | то же | MIT | зависит от плагина; ComfyUI-провайдера не нашёл | — | точка расширения: написать свой провайдер |
| 15 | OpenClaw: встроенный плагин `comfy` ([docs](https://docs.openclaw.ai/providers/comfy)) + [clawhub @comfy-org/comfy](https://clawhub.ai/comfy-org/comfy) | 391544 | 09.10.2026 | MIT | плагин: да, локальный ComfyUI, воркфлоу JSON, t2v и i2v (1 референс); skill — только облако | хост | готовый путь для OpenClaw |
| 16 | [ATH-MaaS/ComfyUI-Copilot](https://github.com/AIDC-AI/ComfyUI-Copilot) | 5538 | 11.09.2026 | MIT | внутри ComfyUI, LLM пишет воркфлоу | да | не про видео-проект; Wan 2.2 LLM не знает (по README) |
| 17 | [yi1108/printfilm](https://github.com/yi1108/printfilm) | 5088 | 08.10.2026 (v0.2.0) | MIT | не проверено | не проверено | кандидат на ручную проверку (создан 10.09.2026) |
| 18 | [xuanyustudio/LocalMiniDrama](https://github.com/xuanyustudio/LocalMiniDrama) | 1990 | 02.10.2026 (v1.2.8) | MIT | «полностью офлайн», история → раскадровка → видео; детали не проверены | не проверено | проверить, если нужен офлайн |
| 19 | [zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills) и [eternityspring/shuohao-skills](https://github.com/eternityspring/shuohao-skills) | 2663 / 4302 | 03.10 / 08.10.2026 | MIT / Apache-2.0 | skills для Claude Code и Codex: сценарий, персонажи, раскадровка, промпты, ревью | хост | куски: шаблоны промптов сценариста |
| 20 | [Heroesjouney/AIMovieStudiov2](https://github.com/Heroesjouney/AIMovieStudiov2) (эталон из брифа) | 169 | 29.09.2026 | AGPL-3.0 | ComfyUI + Fal/Replicate | нет | для сравнения |
| 21 | [PhiloLabs/agentic-vbench](https://github.com/PhiloLabs/agentic-vbench) | 106 | 09.10.2026 | Apache-2.0 | бенчмарк агентов постпродакшна (Assembly, Repair, Sequencing, Repurpose) | — | не критик, а бенчмарк |
| 22 | [Vchitect/VBench](https://github.com/Vchitect/VBench) (+ VBench-2.0, arXiv 2503.21755) | 1811 | 21.08.2026 | не проверена | локально, метрики на моделях-специалистах | нет | метрики физики/анатомии, не драматургия |

## Топ-5 подробно

### 1. artokun/comfyui-mcp: лучший кандидат на слой «агент ↔ наш ComfyUI»
- Критерий 1/2: «local-first», ComfyUI на вашей установке, LAN, VPS или Comfy Cloud. Подходит под схему «Mac для разработки, CUDA-карта отдельно».
- Критерий 4: skills по семействам моделей: Flux, WAN, LTX 2.3, **MiniMax H3**, Qwen, Z-Image, плюс обучение LoRA для anime/WAN/Z-Image. Krea 2 в README не нашёл. Для нас прямое попадание: H3, Z-Image, LTX, Wan.
- Критерий 5: боковая панель в ComfyUI с агентом на любой модели: Claude, ChatGPT, Gemini, Ollama и любой OpenAI-совместимый endpoint. README рекомендует thinking и vision, иначе цепочки инструментов деградируют. Есть дообученные gemma4 для Ollama.
- Критерий 6: 38 MCP-инструментов, 42 skills, 4 автономных агента, 11 slash-команд; `npx -y comfyui-mcp setup hermes` и `setup openclaw` прописывают сервер в `~/.hermes/config.yaml` и `~/.openclaw/openclaw.json`. Это единственный найденный MCP, который заявляет оба нужных хоста. Работает и как плагин Claude Code.
- Видео-вывод: `generate_image action:"video"` (LTX-2.3), `get_image list_outputs` ищет и видео. `get_image view` возвращает инлайн-картинку агенту, то есть видео агент «увидит» только по кадрам. **MCP Apps (ui://) не нашёл ни здесь, ни у других ComfyUI-MCP** (README, WebSearch). Видеоплеера-виджета в готовом виде нет.
- Критерий 3: проекта/сцен/дублей нет — это уровень инструмента, а не студии. Консистентность персонажа, продолжение от последнего кадра — только через воркфлоу/skills.
- Зрелость: 802 звезды, 136 форков, 19 контрибьюторов, создан 15.02.2026, релиз v0.52.205 от 01.10.2026 (очень частые релизы), MIT. Риск: темп изменений высокий, API инструментов может плавать.
- Вердикт: брать как основу MCP-слоя или как образец; доделать недостающее (проект/сцены, ui://-виджеты) своим сервером.

### 2. Toonflow-app: самая зрелая «студия» с локальным ComfyUI и MCP
- Бесконечный холст: сценарий, персонажи, сцены и видеоклипы на одной доске; три вида персонажа; локальное развёртывание (десктоп macOS/Windows, Android, Docker); MCP, A2A, плагины, «открытые промпты и инструменты».
- «Свободное подключение моделей: сторонние API, локальный ComfyUI и LLM». Что именно ComfyUI делает (картинки/видео/сквозные воркфлоу) и как задаётся адрес — **не проверено**, нужен запуск.
- Лицензия MIT (в 2026 перешли с Apache-2.0 и сняли коммерческие ограничения). 16964 звезды, релиз v2.0.5 от 09.10.2026, создан 29.01.2026. Но API контрибьюторов вернул **2 человека**: фактически команда из двух людей (для 17k звёзд это риск). Документация и интерфейс китайские, 21 язык UI, включая русский.
- Отличается от AIMovieStudio: есть LLM-сценарий, мобильная версия (Android APK с локальным бэкендом Bun и «устройствами»), MCP/A2A.
- Вердикт: **первый кандидат на пробный запуск** как основа. Проверить: подключение нашего ComfyUI, дубли, продолжение от последнего кадра, расширяемость нашими воркфлоу H3.

### 3. ViMax (HKUDS): лучшая «логика режиссёра», но облачные генераторы
- Идея → сценарий → раскадровка → референсы → ключевые кадры → видео → сборка; консистентность, AutoCameo (человек из фото), режимы idea2video, script2video, novel2video (RAG с эмбеддингами). Есть TUI и Web UI на 127.0.0.1.
- **Vision-критик внутри пайплайна**: генерирует 2 кандидата ключевого кадра параллельно и выбирает лучший vision-способным чатом (`image_selection.num_candidates`); коммит от 30.09.2026 «restore concurrent keyframe generation and VLM selection». Это единственный найденный open-source пример автоотбора в пайплайне; остальные «ревью» либо проверяют схему и ffprobe (OpenMontage), либо пишут промпты.
- LLM: `model_provider: openai` + `base_url` → любой OpenAI-совместимый локальный сервер (проверено по `agent.example.yaml`). Требуется vision в LLM.
- Генераторы изображений/видео: Nano Banana, Seedream, OpenRouter, Veo, Seedance, Omni (Yunwu API). **ComfyUI и локальных моделей нет**; подключение H3/ComfyUI потребует написать класс по `tools/protocols.py` (в репозитории есть тест протокола генератора).
- Зрелость: 12594 звезды, 1899 форков, 12 контрибьюторов, MIT, создан 30.03.2025, активен (коммит 30.09.2026). Заметка: статья arXiv 2606.07649 (WebSearch).
- Вердикт: **куски** (промпты агентов, схема консистентности, VLM-выбор кандидата), как каркас — нет.

### 4. Pixelle-Video (+ Pixelle-MCP): ComfyUI + Ollama = «0 рублей» по их README
- Ядро: «полностью автоматический движок коротких видео»; атомарные возможности (картинка, видео, TTS, VLM) заменяются воркфлоу ComfyUI/RunningHub или прямым API. Адрес ComfyUI настраивается (по умолчанию 127.0.0.1:8188); свои воркфлоу кладутся в `workflows/`. Модели: Wan 2.1, Wan/HappyHorse через DashScope, Kling, Seedance. LLM: GPT, Qwen, DeepSeek, Ollama.
- Жанр — короткие ролики-шаблоны (текст → видео с озвучкой), не драматургия по сценам с консистентными персонажами. Проекта/дублей нет.
- Зрелость: 28803 звезды, 4187 форков, Apache-2.0, создан 07.11.2025, релиз v0.1.15 (27.01.2026), последний push 14.06.2026 — **почти 4 месяца тишины**. Pixelle-MCP (1127 звёзд) не обновлялся с 17.12.2025.
- Вердикт: куски (идея «воркфлоу-шаблон = атомарная возможность»), не основа.

### 5. OpenMontage: агент-хост как режиссёр, skills по стадиям
- «Превратите AI-ассистента в видеостудию»: 10+ пайплайнов, 60+ провайдеров, каждая стадия описана markdown-skill «режиссёр», манифест пайплайна в YAML, чекпоинты, самопроверка рецензентом, ворота качества (запрет слайдшоу, проверка плана до траты GPU, ffprobe после рендера), локальная панель Backlot. Композиция — Remotion/HyperFrames + FFmpeg.
- Хосты: Claude Code, Cursor, Copilot, Windsurf, Codex; в README есть секция для агентов в стиле OpenClaw.
- Локально: `VIDEO_GEN_LOCAL_ENABLED=true`, модели wan2.2-ti2v-5b, wan2.1, hunyuan-1.5, ltx2-local, cogvideo-5b — встроенными скриптами, **не через ComfyUI**; MiniMax доступен через fal.
- Зрелость: 65745 звёзд за 6 месяцев (создан 29.03.2026), форков 8348, push 03.10.2026, **AGPL-3.0**, релизов нет (API вернул 404). Скорость роста звёзд нетипична для 6 месяцев; к метрике относиться осторожно, качество не оценивал.
- Критерий 5: LLM = тот, на котором работает хост-агент; прямого поля OpenAI-endpoint нет.
- Вердикт: идеи по организации skills/ворот качества; код не брать (AGPL, стек Remotion).

## Что по требованиям среза

- **MCP-серверы ComfyUI**: живой лидер один (artokun, 802★). joenorton (407★) последний push в феврале — умеренно жив; Pixelle-MCP, shawnrushefsky и остальные мелкие — 2025/начало 2026. Официальный Comfy Cloud MCP — облачный, ранний доступ/бета. «First-party Comfy Local MCP» упомянут в README artokun как private. **MCP Apps-виджеты у ComfyUI-серверов не найдены**; наш MCP-слой с виджетами (плеер дублей, выбор кандидата) — незанятая ниша.
- **Хосты**: Hermes — готовый skill `creative/comfyui` (comfy-cli, REST/WS; версии в доках расходятся: 5.0.0 «bundled» и 5.1.0 «optional»), у Hermes есть абстракция video_gen-провайдера (плагины); OpenClaw — встроенный плагин `comfy` (воркфлоу JSON, t2v, i2v с 1 референсом, размер/длительность в воркфлоу вручную) и 169 community-skills на сторонней витрине. Claude — плагины artokun и Comfy-Org (облако), skills для сценариста (drama-skills, shuohao-skills).
- **Vision-критики**: в пайплайнах — ViMax (VLM-выбор из 2 кандидатов кадра), OpenMontage (рецензент по схеме + ffprobe, видео по содержанию не оценивает), VideoClaw (отдельная модель `vlm` в конфиге; что именно проверяет — не проверено). Бенчмарки: VBench / VBench-2.0 (физика, анатомия, commonsense), VideoScience-Bench (checklist от LLM-агента, оценка 4 балла — arXiv 2512.02942), «Is Your Video Language Model a Reliable Judge?» (arXiv 2503.05977, Analyze-then-Judge). **Готового open-source цикла «VLM-оценка → перегенерация» для видео не нашёл**, и согласно памяти проекта драматургию клипа всё равно судит человек до генерации.

## Итоговая рекомендация
1. Пробный запуск Toonflow (Docker, подключить наш ComfyUI/sglang и OpenAI-совместимый LLM) — единственный претендент на «основу студии».
2. artokun/comfyui-mcp — взять как MCP-слой для Hermes/OpenClaw/Claude и источник skills (H3, LTX, Z-Image, Wan уже описаны); виджеты MCP Apps писать самим.
3. ViMax — украсть паттерн «N кандидатов → VLM выбирает» и схему консистентности.
4. Остальное — для идей. Мёртвые: MovieAgent (03.2025), Pixelle-MCP (12.2025).

## Не проверено
Конфигурация ComfyUI в Toonflow; детали printfilm и LocalMiniDrama; лицензия VBench; работа Hermes video_gen с ComfyUI; наличие у waoowaoo OpenAI-совместимого LLM; число контрибьюторов у остальных проектов, кроме указанных.

## Ссылки
- ViMax: https://github.com/HKUDS/ViMax, https://huggingface.co/papers/2606.07649
- artokun/comfyui-mcp: https://github.com/artokun/comfyui-mcp
- Toonflow: https://github.com/HBAI-Ltd/Toonflow-app
- Pixelle: https://github.com/AIDC-AI/Pixelle-Video, https://github.com/ATH-MaaS/Pixelle-MCP
- OpenMontage: https://github.com/calesthio/OpenMontage
- Hermes skill: https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/creative/creative-comfyui
- OpenClaw comfy: https://docs.openclaw.ai/providers/comfy, https://clawhub.ai/comfy-org/comfy
- Comfy Cloud MCP: https://docs.comfy.org/development/cloud/mcp-server
- VBench-2.0: https://arxiv.org/html/2503.21755v1; VideoScience-Bench: https://arxiv.org/pdf/2512.02942; Video judge: https://arxiv.org/pdf/2503.05977
- MovieAgent: https://github.com/showlab/MovieAgent; AgenticVBench: https://arxiv.org/abs/2605.27705
