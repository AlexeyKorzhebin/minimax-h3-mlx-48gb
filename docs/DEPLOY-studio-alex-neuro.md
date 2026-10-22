# Студия на alex-neuro: выкладка, обновление, откат

Спека: `docs/superpowers/specs/2026-10-10-studio-part1-core-design.md` §4.2–4.3. Старая панель
(`/home/alex/Projects/h3-panel`, контейнер `h3-panel-h3-panel-1`, :8765) этой процедурой не
затрагивается — это доказывает отпечаток до и после (`scripts/check_panel_untouched.sh`, любой
сбой проверки — ненулевой код).

| Что | Где |
|---|---|
| Код (клон ветки) | `/home/alex/Projects/h3-studio` |
| Данные (`H3_OUTDIR=/home/alex/Outputs`) | `/home/alex/Outputs/studio/` — `studio.sqlite`, `backups/`, `assets/`, `providers.json`, `.env` |
| Страница | `http://alex-neuro:8766/studio` или `http://192.168.100.50:8766/studio` (с Мака); другие имена — 400 (R26) |
| Контейнер | `h3-studio-studio-api-1`, `network_mode: host` |

Пароля нет (R26): студия отвечает только своим именам и отклоняет изменяющие запросы с чужих
сайтов. Если LAN-IP сервера сменился — поправить `STUDIO_ALLOWED_HOSTS` в `compose.studio.yaml`.

## 0. Только закоммиченное

Все файлы выкладки и кода — в коммите ветки до bundle (bundle несёт коммиты, не рабочее дерево).
`git status --short` в worktree пуст, кроме `h3_48gb/studio/web/dist/` (он в `.gitignore`).

## 1. Отпечаток старой панели — до

    ssh alex-neuro "mkdir -p /home/alex/Outputs/studio/tools"
    scp scripts/check_panel_untouched.sh alex-neuro:/home/alex/Outputs/studio/tools/check_panel_untouched.sh
    ssh alex-neuro 'bash /home/alex/Outputs/studio/tools/check_panel_untouched.sh' > ~/Research/TestVideo/_логи/studio-s0-panel-before.txt \
      && cat ~/Research/TestVideo/_логи/studio-s0-panel-before.txt

Ожидание: код 0, последняя строка `http 200`. Ненулевой код — стоп, панель уже нездорова.

## 2. Доставка кода и мастера (на сервере нет GitHub)

    # на Маке, из ~/worktrees/studio-s0-s1
    npm --prefix h3_48gb/studio/web ci && npm --prefix h3_48gb/studio/web run build
    SHA=$(git rev-parse HEAD)
    git bundle create "$TMPDIR/h3-studio.bundle" studio/s0-s1
    scp "$TMPDIR/h3-studio.bundle" alex-neuro:/home/alex/Projects/h3-studio.bundle
    ssh alex-neuro 'test -d /home/alex/Projects/h3-studio \
      || git clone -b studio/s0-s1 /home/alex/Projects/h3-studio.bundle /home/alex/Projects/h3-studio'
    ssh alex-neuro 'cd /home/alex/Projects/h3-studio \
      && git fetch /home/alex/Projects/h3-studio.bundle studio/s0-s1 \
      && git checkout -B studio/s0-s1 FETCH_HEAD'
    test "$(ssh alex-neuro 'git -C /home/alex/Projects/h3-studio rev-parse HEAD')" = "$SHA" && echo "sha ok"
    ssh alex-neuro 'test -f /home/alex/Projects/h3-studio/compose.studio.yaml && echo compose ok'
    rsync -a --delete h3_48gb/studio/web/dist/ alex-neuro:/home/alex/Projects/h3-studio/h3_48gb/studio/web/dist/

Ожидание: `sha ok`, `compose ok`.

## 3. Тег отката, секреты, образ, запуск

    STAMP=$(date +%Y%m%d-%H%M%S)
    ssh alex-neuro "cd /home/alex/Projects/h3-studio && bash scripts/studio_tag_prev.sh $STAMP"
    ssh alex-neuro 'mkdir -p /home/alex/Outputs/studio && ls -ld /home/alex/Outputs/studio'   # владелец alex
    ssh alex-neuro 'cd /home/alex/Projects/h3-studio && bash scripts/check_image_secrets.sh'
    ssh alex-neuro 'cd /home/alex/Projects/h3-studio && docker compose -f compose.studio.yaml build'
    ssh alex-neuro 'docker run --rm --entrypoint python -e HOME=/tmp h3-studio:latest -m pytest -q \
      -p no:cacheprovider tests/test_studio_db.py tests/test_studio_events.py tests/test_studio_api.py \
      tests/test_studio_security.py'
    ssh alex-neuro 'cd /home/alex/Projects/h3-studio && docker compose -f compose.studio.yaml up -d \
      && sleep 25 && docker compose -f compose.studio.yaml ps'
    ssh alex-neuro 'curl -s 127.0.0.1:8766/api/studio/health; echo; \
      curl -s -o /dev/null -w "foreign host %{http_code}\n" -H "Host: evil.example" 127.0.0.1:8766/api/studio/health'

Ожидание: `prev … -> h3-studio:prev h3-studio:prev-<stamp>` (или «first deploy»); `no planted
secret in the image`; тесты зелёные; `(healthy)`; `{"ok":true,"schema_version":1}` и `foreign host 400`.

## 4. Отпечаток старой панели — после

    ssh alex-neuro 'bash /home/alex/Outputs/studio/tools/check_panel_untouched.sh' > ~/Research/TestVideo/_логи/studio-s0-panel-after.txt
    diff ~/Research/TestVideo/_логи/studio-s0-panel-before.txt ~/Research/TestVideo/_логи/studio-s0-panel-after.txt && echo "panel untouched"

## Откат

`h3-studio:prev` — образ, на котором студия работала перед этой выкладкой (`studio_tag_prev.sh`
берёт его у работающего контейнера, а не у `latest`, поэтому неудачная сборка и повтор его не
затирают; каждая выкладка оставляет ещё и `prev-<stamp>`). Миграции S0–S1 только добавляют.

    ssh alex-neuro 'cd /home/alex/Projects/h3-studio && docker tag h3-studio:prev h3-studio:latest \
      && docker compose -f compose.studio.yaml up -d --no-build'

Выключить студию: `docker compose -f compose.studio.yaml down` в `/home/alex/Projects/h3-studio`;
данные в `/home/alex/Outputs/studio` остаются.
