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
# The container's code is the code of whoever died FIRST; decided before the TERM below, so the
# survivor's own (143) status cannot overwrite it.
if kill -0 "$web" 2>/dev/null; then first=$worker; second=$web; else first=$web; second=$worker; fi
kill -TERM "$web" "$worker" 2>/dev/null
status=0
wait "$first" || status=$?
wait "$second" 2>/dev/null || true
exit "$status"
