#!/bin/sh
# spec §3.2: `h3 web` and `h3 worker` under one simple supervisor; the container exits when
# either dies, and `restart: unless-stopped` brings both back. POSIX sh, no `wait -n`.
set -u
: "${H3_OUTDIR:?H3_OUTDIR must be set}"
mkdir -p "$H3_OUTDIR"
python -m h3_48gb web --host "${H3_WEB_HOST:-0.0.0.0}" --port "${H3_WEB_PORT:-8765}" --outdir "$H3_OUTDIR" &
web=$!
python -m h3_48gb worker --outdir "$H3_OUTDIR" &
worker=$!
trap 'kill -TERM "$web" "$worker" 2>/dev/null' TERM INT
while kill -0 "$web" 2>/dev/null && kill -0 "$worker" 2>/dev/null; do
    sleep 1
done
kill -TERM "$web" "$worker" 2>/dev/null
status=0
wait "$web" || status=$?
wait "$worker" || status=$?
exit "$status"
