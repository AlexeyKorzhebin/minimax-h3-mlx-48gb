# Панель на alex-neuro: как развёрнута и как обновлять

Спека: `docs/superpowers/specs/2026-10-06-panel-on-alex-neuro-design.md`. Первое развёртывание —
2026-10-07 (задача 14 волны 1).

## Что где

| Что | Где |
|---|---|
| Код (клон ветки) | `/home/alex/Projects/h3-panel` |
| Данные панели (`H3_OUTDIR`) | `/home/alex/Outputs/h3-panel` (владелец `alex`, uid 1000) |
| Кадры LTX панели | `/home/alex/Outputs/comfy/output/h3panel` (создать до `compose up`, иначе Docker создаст от root) |
| Провайдеры чата | `/home/alex/Outputs/h3-panel/providers.json` |
| Пробы §6 | `/home/alex/Outputs/h3-panel/probes/` (`*.jsonl`, ролики и кадры) |
| Логи развёртывания и проб | `/home/alex/Projects/h3-panel/logs/` (в `.dockerignore`), `/home/alex/Outputs/h3-panel/logs/` |
| Диспетчер GPU | systemd `h3-gpu-dispatcher`, `127.0.0.1:8790`; логи движков — `h3-bench/logs/serve-panel-*.log`, `comfy/logs/comfy-panel-*.log` |
| Страница | `http://192.168.100.50:8765` (с Мака), контейнер `h3-panel-h3-panel-1`, `network_mode: host` |

## Доставка кода (на сервере нет GitHub)

```bash
# на Маке, из worktree ветки
git bundle create "$TMPDIR/h3-panel.bundle" feat/panel-alex-neuro
scp "$TMPDIR/h3-panel.bundle" alex-neuro:/home/alex/Projects/h3-panel.bundle
ssh alex-neuro 'cd /home/alex/Projects/h3-panel \
  && git fetch /home/alex/Projects/h3-panel.bundle feat/panel-alex-neuro \
  && git checkout -B feat/panel-alex-neuro FETCH_HEAD'
```
Первый раз — `git clone -b feat/panel-alex-neuro /home/alex/Projects/h3-panel.bundle /home/alex/Projects/h3-panel`.

## Образ, тесты, запуск

```bash
cd /home/alex/Projects/h3-panel
docker compose build
# --init обязателен: без него тест «внук» падает на зомби
docker run --rm --init --entrypoint python -e HOME=/tmp h3-panel:latest -m pytest -q -p no:cacheprovider
docker compose up -d && docker compose ps     # ждать (healthy): healthcheck спрашивает /api/state
```

## Диспетчер

```bash
sudo cp /home/alex/Projects/h3-panel/tools/gpu-dispatcher/h3-gpu-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now h3-gpu-dispatcher
systemctl is-active h3-gpu-dispatcher && curl -s 127.0.0.1:8790/status
```
Юнит запускает `dispatcher.py` прямо из клона: после обновления кода — `sudo systemctl restart
h3-gpu-dispatcher` (движки переживают рестарт, `KillMode=process`).

С финальной волны правок (07.10) у каждого `/acquire` и `/release` есть клиент — тело без
`"client"` получает 400 `client_required`:

```bash
curl -s -X POST 127.0.0.1:8790/acquire -d '{"engine": "h3", "client": "probes"}'
curl -s -X POST 127.0.0.1:8790/release -d '{"client": "probes"}'          # только свои движки
curl -s -X POST 127.0.0.1:8790/release -d '{"client": "me", "all": true}' # всё своё диспетчера
```
Воркер панели — `panel-worker`, кнопки страницы — `panel-web`, пробы — `probes`.
`/status` показывает `owner` у каждого движка.

Журнал переключений движков: `~/.local/state/h3-gpu-dispatcher/events.jsonl` (рядом со
`state.json`) и `journalctl -u h3-gpu-dispatcher` — строки `gpu-dispatcher: {"ts", "event",
"engine", "client", "pid", "seconds", ...}`: `starting`, `ready` (секунды подъёма), `stopping`,
`stopped` (секунды до смерти группы, `sigkill`), `failed`, `release`, `qwen_unload`,
`qwen_restore`.

## providers.json

Qwen на этой машине (`/home/alex/Projects/qwen`, `--served-model-name qwen3.8-27b`, порт 8000).
`base_url` — **без `/v1`**: код сам дописывает `/v1/chat/completions` и `/v1/models`.

```json
{
  "active": "qwen-alex-neuro",
  "providers": {
    "qwen-alex-neuro": {"type": "openai", "base_url": "http://127.0.0.1:8000",
                        "model": "qwen3.8-27b", "max_tokens": 20000}
  }
}
```
`max_tokens` 20000: в режиме `start-shared32` контекст Qwen — 32768 токенов, а тяжёлый сценарий
стоит около 20 тыс. токенов вывода (`docs/LLM-PROVIDERS.md`). Ключ не нужен — `api_key_env` нет.

## Пробы §6

Пробы — клиент `probes`: idle-release воркера их H3 не гасит, а их `release` не гасит H3 панели.
Пока H3 у проб, задачи панели ждут с причиной «H3 занят клиентом probes», и наоборот.

```bash
cd /home/alex/Projects/h3-panel
docker compose run --rm \
  -v /home/alex/Projects/h3-bench/beach-jobs.json:/home/alex/Projects/h3-bench/beach-jobs.json:ro \
  --entrypoint python h3-panel tools/probes/sglang_probes.py --keep-h3 \
  keyframe_without_reference picture_numbering beach
# затем references:3 references:6 references:9, --together для замера POST под нагрузкой;
# --duration 10 --steps 2 --ref-size 512x683 — пик на 10-секундной сцене и портретах.
# Входные картинки — /home/alex/Outputs/h3-panel/probes/inputs/<запуск>/<проба>/; в каждой
# строке *.jsonl — "args" (steps/duration/ref_size/together) и "request" (target, шаги, размеры).
curl -s -X POST 127.0.0.1:8790/release -d '{"client": "probes"}'   # после --keep-h3
```

## Что пробы показали (2026-10-07)

Полные числа — `.superpowers/sdd/2026-10-07-panel-on-alex-neuro/task-14-report.md` и
`/home/alex/Outputs/h3-panel/probes/*.jsonl`.

- Нумерация: при кейфрейме `<Picture 1>` — первая картинка-**референс**, кейфрейм номера не
  получает (подтверждено генерацией и зеркальным контролем).
- Память (512 px, `peak_memory_mb` сервера; карта 63,4 ГиБ): 10 с — 3 кв. реф. 58,0 ГБ, 6 кв.
  61,9, 7 кв. 63,6, 9 кв. — CUDA OOM; 6 портретных 3:4 — 63,6, 5 портретных — 62,2. Сервер
  растягивает каждый референс до 2048 px по короткой стороне **без ограничения площади**, так что
  считать надо площадь, а не штуки.
- **`H3_MAX_REF_IMAGES=5`** (решение координатора 07.10; дефолт в `sglang_args.py` и в
  `compose.yaml`): шесть портретных 3:4 на 10 с дали 63,6 ГБ из ~64,9 ГБ карты — запаса нет; пять
  портретных — 62,2 ГБ. Горизонтальные 16:9 референсы (×1,78 площади квадрата) не проверены.
- Числа пиков однократные. Наблюдение: один и тот же `picture_numbering` (seed 42) дал 55 378 МБ
  первым запросом после подъёма H3 и 51 810 МБ через полчаса — разброс `peak_memory_mb` сервера
  порядка 3,5 ГБ, сравнимый с запасами выше.
- `POST /v1/videos` отвечает за 3–78 мс даже во время денойза (работа уходит в очередь);
  таймаут клиента 60 с с запасом в три порядка.

## Готовый сценарий без LLM (видеопроект)

`PUT /api/projects/<id>/scenes` (спека §4.1, «Готовый сценарий без LLM»): сцены с промптами,
длительностями, `@`-тегами референсов и стартовым кадром сцены 0 — без чата. Работает, пока в
проекте ничего не поставлено; после него проект ждёт «Утвердить», как сценарий из чата.

```bash
P=http://127.0.0.1:8765
OUT=/home/alex/Outputs/h3-panel
# 1. картинки — внутрь outdir (карточки берут файлы только оттуда)
mkdir -p $OUT/uploads/battle
cp face.png opening.png $OUT/uploads/battle/
# 2. карточки библиотеки
curl -s -X POST $P/api/library -H 'Content-Type: application/json' -d '{"tag": "@amazon",
  "kind": "person", "description": "a woman in dark armor", "assets":
  ["/home/alex/Outputs/h3-panel/uploads/battle/face.png"]}'
curl -s -X POST $P/api/library -H 'Content-Type: application/json' -d '{"tag": "@arena",
  "kind": "environment", "description": "a sand arena under open sky", "assets":
  ["/home/alex/Outputs/h3-panel/uploads/battle/opening.png"]}'
# 3. пустой видеопроект -> id
ID=$(curl -s -X POST $P/api/projects -H 'Content-Type: application/json' \
  -d '{"kind": "video", "title": "fight-armored-40"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
# 4. сценарий: 5 сцен по 8 с, сцена 0 стартует с кадра @arena
curl -s -X PUT $P/api/projects/$ID/scenes -H 'Content-Type: application/json' -d '{
  "references": [{"tag": "@amazon"}, {"tag": "@arena"}],
  "scenes": [
    {"prompt": "@amazon walks into @arena ...", "duration": 8, "start_image": "@arena"},
    {"prompt": "@amazon ... on @arena ...", "duration": 8},
    {"prompt": "...", "duration": 8},
    {"prompt": "...", "duration": 8},
    {"prompt": "...", "duration": 8}]}'
# 5. утвердить: снап длительностей к сетке, проверка тегов, сцена 0 в очередь
curl -s -X POST $P/api/projects/$ID/approve/script -H 'Content-Type: application/json' -d '{}'
```
- `start_image` — только у сцены 0: `@тег` закреплённой карточки (берётся её первая картинка)
  или путь внутри `/home/alex/Outputs/h3-panel`. Это кейфрейм сцены 0 (`role: keyframe`).
- Длительность 3–15 с; «Утвердить» снапает к сетке: сцена 0 — 192 кадра (8,000 с), сцепленные
  — 191 доставленный (запрос 192).
- Необязательные поля сцены: `seed` (целое ≥ 0; порядок: seed сцены → seed проекта → 42) и `steps`
  (целое 2..100, по умолчанию 50; только sglang) — идут в payload (`seed`, `num_inference_steps`).
  Seed проекта: `PUT /api/projects/<id>/settings` `{"seed": 7}` (`null` снимает), вместе с `i2v_prefix`.
- Каждая сцена обязана назвать хотя бы один `@тег` (ref2va без референса сервер не берёт).
- Свой блок `subject_definitions:` в промпте панель не дублирует (спека §3.5, п. 5), но `@`-теги
  в нём и в тексте всё равно становятся `<Subject N>` в порядке первого упоминания, картинки —
  `<Picture k>` в том же порядке (кейфрейм не нумеруется). Свой блок пишется под эту нумерацию.
- Ответ — проект целиком; ошибка — `{"error": {"code", "message"}}` (409
  `project_stage_not_ready`, если сцены уже ставились).

## Отчёт боевого прогона

```bash
cd /home/alex/Projects/h3-panel
TZ=Europe/Moscow python3 tools/battle_report.py /home/alex/Outputs/h3-panel/projects/<id> \
  --out docs/BATTLE-$(date +%F).md
```
Только stdlib, на хосте. Читает `project.json` (`stage_times`, сцены), очередь
(`/home/alex/Outputs/h3-panel/queue`: `gpu_wait_s`, `engine_start_s`, начало/конец задач),
`<stem>.json` сцен (wall/inference/peak/сервер/скачивание/проверка), `upscale/report.json` и
`events.jsonl` диспетчера (`--events`, по умолчанию `~/.local/state/h3-gpu-dispatcher/`).
`TZ` — как у контейнера: метки очереди наивные московские, события диспетчера — epoch.

## Грабли

- **Idle-release гасит только своё** (с финальной волны 07.10; раньше гасил любой движок
  диспетчера, и пробы приходилось гонять при остановленном контейнере). Воркер шлёт `/release`
  от имени `panel-worker` и только если сам брал карту; срок на плашке — его собственный отсчёт
  (`queue/idle-since`). Движок, поднятый до этой версии диспетчера (без `owner`), гасит только
  кнопка «Освободить карту» или его усыновит первый `acquire`.
- `validate_workflow.py` для `ltx_workflow.json` даёт `VALID: False` только из-за файла-заглушки
  `input-pad.mp4` в `LoadVideo` (его нет в `ComfyUI/input`); с подставленным существующим
  роликом — `VALID: True`, `nodes with unknown inputs: 0`.
