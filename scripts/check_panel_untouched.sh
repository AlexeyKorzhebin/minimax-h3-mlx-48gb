#!/usr/bin/env bash
# Fingerprint of the old panel on alex-neuro (studio spec §4.3: the studio must not touch it).
# Run before and after a studio deploy; the two outputs must be byte-identical. Every probe is
# checked on its own and any failure exits non-zero — a diagnostic that cannot fail proves nothing.
set -euo pipefail

PANEL_NAME=${PANEL_NAME:-h3-panel-h3-panel-1}
PANEL_REPO=${PANEL_REPO:-/home/alex/Projects/h3-panel}
PANEL_URL=${PANEL_URL:-http://127.0.0.1:8765/api/state}

fail() { printf 'panel check failed: %s\n' "$1" >&2; exit 1; }

ids=$(docker ps -q --no-trunc --filter "name=^${PANEL_NAME}\$")
count=$(printf '%s\n' "$ids" | grep -c . || true)
[ "$count" = 1 ] || fail "expected exactly one running container ${PANEL_NAME}, found ${count}"
cid=$ids
image=$(docker inspect -f '{{.Image}}' "$cid")
[ -n "$image" ] || fail "docker inspect gave no image id"
code=$(git -C "$PANEL_REPO" rev-parse HEAD)
body=$(mktemp)
trap 'rm -f "$body"' EXIT
http=$(curl -s -o "$body" -w '%{http_code}' "$PANEL_URL") || fail "curl ${PANEL_URL} failed"
[ "$http" = 200 ] || fail "GET ${PANEL_URL} answered ${http}"
python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$body" 2>/dev/null \
  || fail "${PANEL_URL} did not answer JSON"
printf 'container %s\nimage %s\ncode %s\nhttp %s\n' "$cid" "$image" "$code" "$http"
