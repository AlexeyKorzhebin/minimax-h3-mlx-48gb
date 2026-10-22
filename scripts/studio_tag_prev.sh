#!/usr/bin/env bash
# Before a studio deploy: tag the image the RUNNING studio container uses as h3-studio:prev (and a
# unique h3-studio:prev-<stamp>). Not `latest`: after a failed build `latest` may already be the
# new image while the container still runs the old one — a retry would then lose the way back.
set -euo pipefail

NAME=${STUDIO_CONTAINER:-h3-studio-studio-api-1}
stamp=${1:?usage: studio_tag_prev.sh <deploy-stamp>}

ids=$(docker ps -q --no-trunc --filter "name=^${NAME}\$")
if [ -z "$ids" ]; then
  printf 'no running studio container: first deploy, nothing to keep\n'
  exit 0
fi
count=$(printf '%s\n' "$ids" | grep -c .)
[ "$count" = 1 ] || { printf 'expected one studio container, found %s\n' "$count" >&2; exit 1; }
image=$(docker inspect -f '{{.Image}}' "$ids")
[ -n "$image" ] || { printf 'docker inspect gave no image id\n' >&2; exit 1; }
docker tag "$image" "h3-studio:prev-${stamp}"
docker tag "$image" h3-studio:prev
printf 'prev %s -> h3-studio:prev h3-studio:prev-%s\n' "$image" "$stamp"
