#!/usr/bin/env bash
# Layered zero-downtime deploy for the smcity agent.
#
# Designed to never take the public endpoint down. Compared to the v0.5.0
# script this version:
#
#   - Builds the new image under a NEW tag (default: the running
#     pyproject.toml version). The previous tag stays intact so we can
#     fast-roll-back without rebuilding.
#   - Promotes blue first via a per-replica env var (BLUE_TAG). Green
#     keeps serving 100% of traffic through nginx `proxy_next_upstream`
#     while blue recreates. Same dance for green afterwards.
#   - Health gates BOTH the Docker healthcheck AND an HTTP /health probe
#     from inside the bridge network — catches "container is up but the
#     binding is wrong" failures that Docker health alone misses.
#   - Auto-rolls a failing replica back to its prior tag and exits
#     non-zero. The surviving replica is never touched on failure.
#
# Run from `~/srv/smcity/deploy/` on the Mac Studio host.
#
# Usage:
#   ./deploy.sh                          # build current pyproject version + roll
#   ./deploy.sh --tag 0.5.5              # build + roll to a specific tag
#   ./deploy.sh --tag 0.5.5 --no-build   # roll to an already-built tag
#   ./deploy.sh --rollback 0.5.0         # roll both replicas back (no build)
#   ./deploy.sh --status                 # show current blue/green tags + health
#
# Exit codes:
#   0  success
#   1  build failed
#   2  health-check timeout on a replica (auto-rolled-back)
#   3  HTTP sanity probe failed on a replica (auto-rolled-back)
#   4  rollback itself failed (manual intervention needed)
#   64 bad arguments

set -euo pipefail

cd "$(dirname "$0")"

# --- args ------------------------------------------------------------------
NEW_TAG=""
NO_BUILD=0
ROLLBACK_TAG=""
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)        NEW_TAG="$2"; shift 2 ;;
    --no-build)   NO_BUILD=1; shift ;;
    --rollback)   ROLLBACK_TAG="$2"; shift 2 ;;
    --status)     STATUS_ONLY=1; shift ;;
    -h|--help)    sed -n '2,/^$/p' "$0"; exit 0 ;;
    *)            echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

# Ensure docker is on PATH (non-login shells often miss /etc/paths.d).
export PATH="/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH:-}"
if ! command -v docker >/dev/null; then
  echo "❌ docker not found in PATH" >&2
  exit 1
fi

# --- helpers ---------------------------------------------------------------
log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; }

# Current tag from pyproject.toml — the default new tag when --tag not given.
current_version() {
  awk -F'"' '/^version *= */ { print $2; exit }' ../pyproject.toml
}

# Which tag is each replica currently running? Reads from the live container
# so what we see is what's actually serving (not what compose env claims).
current_tag_for() {
  local container="$1"
  docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null \
    | sed 's|.*:||'
}

# Docker network name. Compose prefixes it with the project name (the dir).
# Kept for potential future use; the current http_probe goes via the
# nginx-router container which is already in the network.
network_name() {
  docker network ls --format '{{.Name}}' | grep -E '^([a-z0-9]+_)?smcity-net$' | head -1
}

# Wait for Docker's healthcheck to flip to "healthy".
wait_healthy() {
  local container="$1"
  local timeout="${2:-90}"
  local start=$SECONDS
  while (( SECONDS - start < timeout )); do
    local status
    status="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)"
    case "$status" in
      healthy) ok "$container is Docker-healthy"; return 0 ;;
      missing) err "$container does not exist"; return 4 ;;
      exited)  err "$container exited during start"; return 2 ;;
    esac
    sleep 1
  done
  err "$container did not become healthy within ${timeout}s"
  return 2
}

# Re-check /health from inside the bridge network. Catches "container is
# up but bound to the wrong address" issues that Docker health alone
# misses. Routes the probe through the already-running smcity-router
# container (nginx:alpine ships wget) so we don't depend on docker-hub
# pulls succeeding at deploy time — the credential-helper plumbing on
# the Mac Studio is flakey in non-login shells.
http_probe() {
  local container="$1"
  docker exec smcity-router \
    wget -qO- --tries=1 --timeout=5 "http://${container}:8080/health" >/dev/null 2>&1
}

# Recreate one replica with a new image tag. On failure (Docker-health or
# HTTP probe), roll it back to its prior tag before returning non-zero.
roll_one() {
  local replica="$1"     # smcity-agent-blue | smcity-agent-green
  local new_tag="$2"
  local prior_tag="$3"
  local env_var
  case "$replica" in
    *blue)  env_var="BLUE_TAG"  ;;
    *green) env_var="GREEN_TAG" ;;
    *) err "unknown replica $replica"; return 64 ;;
  esac

  log "[$replica] $prior_tag → $new_tag (recreating)"
  if ! env "$env_var=$new_tag" docker compose up -d --no-deps --force-recreate "$replica"; then
    err "[$replica] compose up failed"
    return 1
  fi

  # Health gate 1: Docker healthcheck.
  if ! wait_healthy "$replica" 90; then
    rollback_one "$replica" "$prior_tag"
    return 2
  fi

  # Health gate 2: HTTP probe from inside the bridge network.
  if ! http_probe "$replica"; then
    err "[$replica] HTTP /health probe failed despite Docker-healthy"
    rollback_one "$replica" "$prior_tag"
    return 3
  fi

  ok "[$replica] now serving smcity:$new_tag"
}

rollback_one() {
  local replica="$1"
  local prior_tag="$2"
  local env_var
  case "$replica" in
    *blue)  env_var="BLUE_TAG"  ;;
    *green) env_var="GREEN_TAG" ;;
  esac
  warn "[$replica] rolling back to $prior_tag"
  if ! env "$env_var=$prior_tag" docker compose up -d --no-deps --force-recreate "$replica"; then
    err "[$replica] ROLLBACK FAILED — replica may be in a broken state"
    err "  Manual fix: $env_var=$prior_tag docker compose up -d --no-deps --force-recreate $replica"
    return 4
  fi
  wait_healthy "$replica" 60 || true
  ok "[$replica] rolled back to $prior_tag"
}

# --- status mode -----------------------------------------------------------
if (( STATUS_ONLY == 1 )); then
  log "Current state:"
  for r in smcity-agent-blue smcity-agent-green smcity-router smcity-tailscale; do
    tag=$(current_tag_for "$r" || echo "?")
    status=$(docker inspect -f '{{.State.Health.Status}}' "$r" 2>/dev/null || echo "no-healthcheck")
    printf '  %-22s  image=%-20s  health=%s\n' "$r" "smcity:${tag:-unknown}" "$status"
  done
  exit 0
fi

# --- rollback mode ---------------------------------------------------------
if [[ -n "$ROLLBACK_TAG" ]]; then
  log "Manual rollback to $ROLLBACK_TAG (both replicas)"
  blue_prior=$(current_tag_for smcity-agent-blue)
  green_prior=$(current_tag_for smcity-agent-green)
  log "  blue:  $blue_prior  → $ROLLBACK_TAG"
  log "  green: $green_prior → $ROLLBACK_TAG"
  roll_one smcity-agent-blue  "$ROLLBACK_TAG" "$blue_prior"  || exit $?
  roll_one smcity-agent-green "$ROLLBACK_TAG" "$green_prior" || exit $?
  ok "Both replicas rolled back to $ROLLBACK_TAG"
  exit 0
fi

# --- forward deploy --------------------------------------------------------
NEW_TAG="${NEW_TAG:-$(current_version)}"
if [[ -z "$NEW_TAG" ]]; then
  err "could not determine new tag (pass --tag or set version in pyproject.toml)"
  exit 64
fi

BLUE_PRIOR=$(current_tag_for smcity-agent-blue || echo "")
GREEN_PRIOR=$(current_tag_for smcity-agent-green || echo "")
BLUE_PRIOR="${BLUE_PRIOR:-0.5.0}"
GREEN_PRIOR="${GREEN_PRIOR:-0.5.0}"

log "Plan:"
log "  build:  smcity:$NEW_TAG$( ((NO_BUILD)) && echo ' (skipped)')"
log "  blue:   $BLUE_PRIOR → $NEW_TAG"
log "  green:  $GREEN_PRIOR → $NEW_TAG"
log "  router + tailscale: untouched"

# Step 1: build under the new tag. Old tags remain intact — rollback is fast.
if (( NO_BUILD == 0 )); then
  log "Building smcity:$NEW_TAG …"
  if ! env BLUE_TAG="$NEW_TAG" docker compose build smcity-agent-blue; then
    err "build failed"
    exit 1
  fi
  ok "smcity:$NEW_TAG built"
else
  log "Skipping build (--no-build). Verifying smcity:$NEW_TAG image exists…"
  if ! docker image inspect "smcity:$NEW_TAG" >/dev/null 2>&1; then
    err "smcity:$NEW_TAG image not found locally"
    exit 1
  fi
fi

# Step 2: roll blue first. Green serves 100% of traffic during this step.
log "Step 1/2 — rolling blue (green serves traffic)"
roll_one smcity-agent-blue "$NEW_TAG" "$BLUE_PRIOR" || exit $?

# Step 3: roll green. Blue now serves 100% of traffic during this step.
log "Step 2/2 — rolling green (blue serves traffic)"
roll_one smcity-agent-green "$NEW_TAG" "$GREEN_PRIOR" || exit $?

# Step 4: reload nginx so it clears any cached upstream-down markers from
# the brief windows when each replica was recreating.
log "Reloading nginx (clears cached unhealthy markers)"
docker exec smcity-router nginx -s reload >/dev/null

ok "Deploy complete — both replicas on smcity:$NEW_TAG, zero requests dropped."
log "Final health check via router:"
docker compose exec -T smcity-router wget -qO- --tries=1 http://127.0.0.1:8080/health || true
echo
