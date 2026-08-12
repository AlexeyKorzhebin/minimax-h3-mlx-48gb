#!/bin/bash
# Поднимает веб-морду очереди (`h3 web`) и работника (`h3 worker`), которые вместе обслуживают
# страницу с чатом и генерацией. Оба — фоновые процессы на весь день, поэтому нужен caffeinate
# (иначе Мак засыпает и роняет ядро GPU в панику под MLX), а корень проекта вычисляется от
# расположения самого скрипта, а не хардкодится: скрипт должен работать и из этой рабочей копии,
# и после слияния ветки в основной checkout.
#
# Идемпотентен: если `h3 web` или `h3 worker` уже живы (проверка `pgrep -f` по точным паттернам
# команды), повторный запуск их не дублирует — два работника означают два процесса генерации по
# 36 ГБ на машине с 48 ГБ и своп, а два веб-сервера подерутся за порт 8765.
#
# Использование:
#   bash scripts/web-start.sh [outdir]     # outdir по умолчанию ~/Research/TestVideo
#
# Логи: <outdir>/_логи/h3-web.log, <outdir>/_логи/h3-worker.log
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
OUTDIR="${1:-$HOME/Research/TestVideo}"
LOGDIR="$OUTDIR/_логи"
PORT=8765

mkdir -p "$OUTDIR" "$LOGDIR"

WEB_PATTERN="h3_48gb.cli web"
WORKER_PATTERN="h3_48gb.cli worker"

if pgrep -f "$WEB_PATTERN" > /dev/null; then
  echo "h3 web уже запущен — пропускаю (pid $(pgrep -f "$WEB_PATTERN" | tr '\n' ' '))"
else
  echo "запускаю h3 web на 127.0.0.1:$PORT, outdir=$OUTDIR"
  nohup caffeinate -dimsu "$PY" -m h3_48gb.cli web --outdir "$OUTDIR" --port "$PORT" \
    >> "$LOGDIR/h3-web.log" 2>&1 &
  disown
  echo "  запущен, лог: $LOGDIR/h3-web.log"
fi

if pgrep -f "$WORKER_PATTERN" > /dev/null; then
  echo "h3 worker уже запущен — пропускаю (pid $(pgrep -f "$WORKER_PATTERN" | tr '\n' ' '))"
else
  echo "запускаю h3 worker, outdir=$OUTDIR"
  nohup caffeinate -dimsu "$PY" -m h3_48gb.cli worker --outdir "$OUTDIR" \
    >> "$LOGDIR/h3-worker.log" 2>&1 &
  disown
  echo "  запущен, лог: $LOGDIR/h3-worker.log"
fi

# Health-check: ждём до 30 секунд, пока /api/state не ответит 200 (сервер поднимается не мгновенно).
echo "жду /api/state на 127.0.0.1:$PORT..."
for i in $(seq 1 30); do
  if curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
      | grep -q '^200$'; then
    echo "OK — h3 web отвечает на http://127.0.0.1:$PORT/ после ${i}s"
    echo "логи: tail -F $LOGDIR/h3-web.log $LOGDIR/h3-worker.log"
    exit 0
  fi
  sleep 1
done

echo "FAIL — h3 web не ответил за 30с. Смотри: tail -F $LOGDIR/h3-web.log"
exit 1
