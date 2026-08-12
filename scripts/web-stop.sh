#!/bin/bash
# Останавливает работника (`h3 worker`), локальный LLM-сервер (`llama-server`) и веб-морду
# (`h3 web`), поднятые web-start.sh.
#
# Работнику — SIGTERM, а не -9. По docstring `main_loop`/`_stop_signals` (h3_48gb/worker.py):
# первый SIGTERM только запрещает брать новые задания из очереди — задача, которая уже
# генерирует (часы GPU-времени, свои чекпойнты по шагам), остаётся нетронутой и доигрывается
# штатно; воркер завершится сам, когда она закончится, и корректно закроет лизу/маркер в очереди.
# SIGKILL этого не даст: процесс-родитель умрёт мгновенно, а сам генератор (отдельная process
# group под caffeinate) осиротеет и останется висеть в памяти без присмотра, а лиза в очереди
# зависнет как «running» без объяснения до ручной уборки. Поэтому только один SIGTERM и без
# ожидания — если задача идёт, воркер закончит её сам, это может занять часы.
#
# llama-server гасится безусловно (pkill -f, как в restart-llama.sh) — веб-морда его не
# останавливает сама, а зависший локальный LLM держит GPU и блокирует следующую генерацию
# (`_llm_holds_gpu` в worker.py).
#
# h3 web — обычный HTTP-сервер без выгрузки состояния на диск, поэтому его можно остановить
# обычным SIGTERM без двухстадийной логики воркера.
#
# Спокоен, если ничего не запущено.
set -u

WEB_PATTERN="h3_48gb.cli web"
WORKER_PATTERN="h3_48gb.cli worker"

worker_pids=$(pgrep -f "$WORKER_PATTERN")
if [ -n "$worker_pids" ]; then
  echo "останавливаю h3 worker (SIGTERM, pid $(echo "$worker_pids" | tr '\n' ' ')) — "\
"текущая задача доиграется штатно, это может занять время"
  kill -TERM $worker_pids 2>/dev/null || true
else
  echo "h3 worker не запущен"
fi

if pgrep -f llama-server > /dev/null; then
  echo "останавливаю llama-server"
  pkill -f llama-server 2>/dev/null || true
else
  echo "llama-server не запущен"
fi

web_pids=$(pgrep -f "$WEB_PATTERN")
if [ -n "$web_pids" ]; then
  echo "останавливаю h3 web (SIGTERM, pid $(echo "$web_pids" | tr '\n' ' '))"
  kill -TERM $web_pids 2>/dev/null || true
else
  echo "h3 web не запущен"
fi

exit 0
