#!/usr/bin/env bash
# Deploy LunaGPT on this host from the committed main branch.
# Builds an image of the current commit, keeps the running one as lunagpt-core:previous, migrates,
# restarts core and Luna, and rolls back to the previous image if Luna does not come up.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "error: uncommitted changes; deploy only what is committed" >&2
  exit 1
fi
sha=$(git rev-parse --short HEAD)

if docker image inspect lunagpt-core:local >/dev/null 2>&1; then
  docker tag lunagpt-core:local lunagpt-core:previous
fi
docker compose build --quiet core
docker tag lunagpt-core:local "lunagpt-core:$sha"

healthy() {
  local since=$1 i
  for i in $(seq 1 30); do
    if docker logs --since "$since" luna-assistant 2>&1 | grep -q "Luna AI Assistant started"; then
      curl -fs -m 3 -o /dev/null http://127.0.0.1:8010/health && return 0
    fi
    sleep 2
  done
  return 1
}

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose up -d migrate core luna-assistant
if healthy "$since"; then
  echo "deployed $sha"
  exit 0
fi

echo "error: $sha did not start; rolling back to the previous image" >&2
docker logs --since "$since" luna-assistant 2>&1 | tail -20 >&2
if docker image inspect lunagpt-core:previous >/dev/null 2>&1; then
  docker tag lunagpt-core:previous lunagpt-core:local
  docker compose up -d --no-deps core luna-assistant
fi
exit 1
