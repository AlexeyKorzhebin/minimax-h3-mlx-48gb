#!/usr/bin/env bash
# Build the studio image from a TEMPORARY copy of the committed tree (+ the built web/dist) with
# synthetic secrets planted in it, and fail if any of them reached the image. The working tree —
# and a real .env in it, if any — is never read or written.
# MUTATE_DOCKERIGNORE=1 strips every .env / providers.json rule from the COPY's .dockerignore: the
# check must then fail; that run is its own "seen red".
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
dist="$repo/h3_48gb/studio/web/dist"
[ -f "$dist/index.html" ] || { printf 'build the master first\n' >&2; exit 2; }
tmp=$(mktemp -d)
tag="h3-studio:secret-check-$$"
trap 'docker rmi -f "$tag" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

git -C "$repo" archive HEAD | tar -x -C "$tmp"
mkdir -p "$tmp/h3_48gb/studio/web/dist"
cp -R "$dist/." "$tmp/h3_48gb/studio/web/dist/"
marker="sk-SYNTHETIC-$$-$(date +%s)-CHECK"
# Decoys: every name a secret or state file could have, at the root and one directory down.
# The test reads this list back from the build context and matches it against .dockerignore.
mkdir -p "$tmp/conf"
for d in . conf; do
  for f in .env .env.local providers.json decoy.env decoy.pem decoy.key decoy.sqlite decoy.sqlite3 decoy.db; do
    printf 'KEY=%s\n' "$marker" > "$tmp/$d/$f"
  done
done
if [ "${MUTATE_DOCKERIGNORE:-0}" = 1 ]; then
  grep -v -E '^(\.env|\*\*/\.env|\*\*/\.env\.\*|\*\*/providers\.json|\*\*/\*\.(pem|key|sqlite|sqlite-\*|sqlite3|db|env))$' "$tmp/.dockerignore" \
    > "$tmp/.dockerignore.mutated" || true
  mv "$tmp/.dockerignore.mutated" "$tmp/.dockerignore"
fi

docker build -q -f "$tmp/Dockerfile.studio" -t "$tag" "$tmp" >/dev/null
# grep: 0 = found, 1 = not found, 2 = could not read. docker run: 125-127 = could not run at all.
# Only an explicit 1 is a pass; anything unexpected is a failed check, never a silent success.
set +e
docker run --rm --entrypoint sh "$tag" -c "grep -rqsF -- '$marker' /app"
rc=$?
set -e
case "$rc" in
  0) printf 'LEAK: a planted secret is inside the image\n' >&2; exit 1 ;;
  1) printf 'no planted secret in the image\n' ;;
  *) printf 'check failed: the search in the image exited %s\n' "$rc" >&2; exit 2 ;;
esac
