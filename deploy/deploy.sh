#!/usr/bin/env bash
# Zero-downtime deploy for the smcity agent.
#
# Run from `~/srv/smcity/deploy/` on the Mac Studio host. Build once, then
# rolling-restart blue → green. Nginx's `proxy_next_upstream` failover
# routes traffic to the surviving replica during each step, so no client
# request 5xxs during a code update.
#
# Usage:
#   ./deploy.sh             # build + rolling restart of both replicas
#   ./deploy.sh --no-build  # skip rebuild, just rolling-restart
#
# Exit codes:
#   0  success
#   1  build failed
#   2  health-check timeout on a replica

set -euo pipefail

cd "$(dirname "$0")"

NO_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    -h|--help)  sed -n '2,/^$/p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 64 ;;
  esac
done

# Ensure docker / compose are on PATH (the Mac Studio's interactive shell
# adds them via /etc/paths.d but non-login invocations may miss them).
export PATH="/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH:-}"

if ! command -v docker >/dev/null; then
  echo "❌ docker not found in PATH" >&2
  exit 1
fi

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; }

wait_healthy() {
  local container="$1"
  local timeout="${2:-60}"
  local start=$SECONDS
  while (( SECONDS - start < timeout )); do
    local status
    status="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)"
    case "$status" in
      healthy) ok "$container is healthy"; return 0 ;;
      missing|exited) err "$container vanished or exited"; return 2 ;;
    esac
    sleep 1
  done
  err "$container did not become healthy within ${timeout}s"
  return 2
}

if (( NO_BUILD == 0 )); then
  log "Building new image (smcity:0.4.15)…"
  docker compose build smcity-agent-blue
  # The green replica reuses the same image — no separate build.
fi

log "Step 1/2 — recreate smcity-agent-blue (traffic flows to green via nginx)"
docker compose up -d --no-deps --force-recreate smcity-agent-blue
wait_healthy smcity-agent-blue 60

log "Step 2/2 — recreate smcity-agent-green (traffic flows to blue via nginx)"
docker compose up -d --no-deps --force-recreate smcity-agent-green
wait_healthy smcity-agent-green 60

log "Reloading nginx router (drops cached unhealthy markers, picks up both replicas)"
docker exec smcity-router nginx -s reload >/dev/null

ok "Deploy complete — both replicas on the new image, zero requests dropped."
log "Verify via /health:"
docker compose exec -T smcity-router wget -qO- http://127.0.0.1:8080/health || true
echo
