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
| Логи развёртывания и проб | `/home/alex/Projects/h3-panel/logs/`, `/home/alex/Outputs/h3-panel/logs/` |
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

```bash
cd /home/alex/Projects/h3-panel
docker compose stop        # см. «Грабли»: idle-release воркера погасит H3 пробы через 15 мин
docker compose run --rm \
  -v /home/alex/Projects/h3-bench/beach-jobs.json:/home/alex/Projects/h3-bench/beach-jobs.json:ro \
  --entrypoint python h3-panel tools/probes/sglang_probes.py --keep-h3 \
  keyframe_without_reference picture_numbering beach
# затем references:3 references:6 references:9, --together для замера POST под нагрузкой
curl -s -X POST 127.0.0.1:8790/release && docker compose up -d
```

## Что пробы показали (2026-10-07)

Полные числа — `.superpowers/sdd/2026-10-07-panel-on-alex-neuro/task-14-report.md` и
`/home/alex/Outputs/h3-panel/probes/*.jsonl`.

- Нумерация: при кейфрейме `<Picture 1>` — первая картинка-**референс**, кейфрейм номера не
  получает (подтверждено генерацией и зеркальным контролем).
- Память (512 px, `peak_memory_mb` сервера; карта 63,4 ГиБ): 10 с — 3 кв. реф. 58,0 ГБ, 6 кв.
  61,9, 7 кв. 63,6, 9 кв. — CUDA OOM; 6 портретных 3:4 — 63,6, 5 портретных — 62,2. Сервер
  растягивает каждый референс до 2048 px по короткой стороне **без ограничения площади**, так что
  считать надо площадь, а не штуки. `H3_MAX_REF_IMAGES=6` — край для квадратных/портретных.
- `POST /v1/videos` отвечает за 3–60 мс даже во время денойза (работа уходит в очередь);
  таймаут клиента 60 с с запасом в три порядка.

## Грабли

- **Idle-release воркера гасит любой движок диспетчера.** Воркер панели через
  `H3_IDLE_RELEASE_MIN` минут пустой очереди шлёт `/release`, а диспетчер не различает, кто
  поднял H3 — панель или скрипт проб. Пока работает контейнер панели, пробы и любые ручные
  `/acquire` живут не дольше 15 минут от старта воркера. Пробы — при остановленном контейнере.
- `validate_workflow.py` для `ltx_workflow.json` даёт `VALID: False` только из-за файла-заглушки
  `input-pad.mp4` в `LoadVideo` (его нет в `ComfyUI/input`); с подставленным существующим
  роликом — `VALID: True`, `nodes with unknown inputs: 0`.
