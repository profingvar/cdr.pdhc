#!/usr/bin/env bash
# cdr.pdhc — single entry point (Rule 16). FULLY CONTAINERISED for every
# instance (cdr1..cdr5): app + db both run as Docker containers via
# docker-compose with `restart: unless-stopped` (reboot-survival, #154) and
# the app carries its own Python in the image (brew-immune, #153).
#
# History: pre-2026-05-25 cdr1 was the lone HYBRID instance (bare-metal
# gunicorn + dockerized db) while cdr2..cdr5 were already dockerized; the
# dispatcher branched on CDR_INSTANCE. That hybrid model is RETIRED
# (#157 Option C / #159) — there is no hybrid sibling left, so this is a
# single uniform dockerized path for all instances.
#
# The operator tarballs this repo to stamp cdrN.pdhc instances; each
# instance's cdr_app/.env pins CDR_INSTANCE / COMPOSE_PROJECT_NAME /
# APP_PORT / DB_PORT / DB_VOLUME so one file deploys all five without
# per-instance hand-editing (Rule 22, Rule 16).
set -euo pipefail

# Step-tracked failure logging — set -e exits on the first error
# but says nothing about *what* failed; the trap below logs the
# step that was active so the operator doesn't have to guess.
# Ticket #236.
CURRENT_STEP="(initialising)"
_on_exit() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo >&2
        echo "[start.sh] FAILED at step: ${CURRENT_STEP} (exit $rc)" >&2
        echo "[start.sh] Instance ${INSTANCE:-?} may be in DEGRADED state — verify containers + DB before walking away." >&2
    fi
}
trap _on_exit EXIT

# macOS ObjC fork-safety (legacy; harmless under containers).
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
APP_DIR="$PROJECT_DIR/cdr_app"

CURRENT_STEP='load .env'
if [ ! -f "$APP_DIR/.env" ]; then
  echo "ERROR: $APP_DIR/.env not found" >&2
  exit 1
fi
set -a; . "$APP_DIR/.env"; set +a

INSTANCE="${CDR_INSTANCE:-cdr_pdhc}"
APP_PORT="${APP_PORT:-9046}"
DB_PORT="${DB_PORT:-9047}"

CURRENT_STEP='verify docker context (colima) responding'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
unset DOCKER_HOST || true
docker context use colima >/dev/null 2>&1 || true
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker not responding. Start colima or check context." >&2
  exit 1
fi
DC="docker compose"
command -v docker-compose >/dev/null 2>&1 && DC="docker-compose"

cd "$APP_DIR"

CURRENT_STEP='docker-compose up -d (db + app)'
# `docker-compose up -d` is idempotent: already-running containers (Docker
# restart policy) are a no-op; `depends_on: service_healthy` makes compose
# wait for the db before (re)starting the app. NEVER kill the Colima DB
# forward ($DB_PORT) — that breaks the host<->VM bridge.
echo "[$INSTANCE] containerised: $DC up -d (db + app on 127.0.0.1:$APP_PORT, db $DB_PORT)"
$DC up -d

CURRENT_STEP='health check (/healthz)'
echo "[$INSTANCE] waiting for http://127.0.0.1:$APP_PORT/healthz"
for i in $(seq 1 30); do
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$APP_PORT/healthz" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "[$INSTANCE] healthy (attempt $i)"
    exit 0
  fi
  sleep 2
done

echo "[$INSTANCE] ERROR: /healthz never reached 200 after 60s" >&2
$DC logs app --tail 40 >&2 || true
exit 1
