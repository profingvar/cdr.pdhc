#!/usr/bin/env bash
# safe_restart.sh — Graceful restart of cdr.pdhc on the server
set -e

# macOS ObjC fork-safety: CoreFoundation in parent poisons fork()s; setting
# this env var before gunicorn prevents the SIGKILL spiral after worker recycles.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$PROJECT_DIR/cdr_app"
VENV_DIR="$APP_DIR/venv"

PORTS=(9046)  # narrowed from (9046 9047 9048 9049) — 9047 was the colima ssh-mux proxy; killing it breaks the host↔VM bridge (see feedback memory)

echo "=== cdr.pdhc safe restart ==="
echo "Project: $PROJECT_DIR"

# Step 1: Kill existing processes on CDR ports (unquoted $PIDS so multi-pid newline-joined strings word-split into separate kill args)
for PORT in "${PORTS[@]}"; do
  PIDS=$(lsof -ti :$PORT 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "Stopping process(es) on port $PORT (PIDs: $PIDS)"
    kill -TERM $PIDS 2>/dev/null || true
    sleep 2
    PIDS=$(lsof -ti :$PORT 2>/dev/null || true)
    [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null || true
    sleep 1
  fi
done

# Step 2: Activate venv
if [ -d "$VENV_DIR" ]; then
  source "$VENV_DIR/bin/activate"
  echo "Activated venv: $VENV_DIR"
else
  echo "ERROR: venv not found at $VENV_DIR"
  exit 1
fi

# Step 3: Install/update dependencies
cd "$APP_DIR"
pip install -q -r requirements.txt

# Step 4: Load .env
if [ -f "$APP_DIR/.env" ]; then
    set -a
    source "$APP_DIR/.env"
    set +a
fi

# Step 5: Ensure Docker DB is running
if ! docker ps --format '{{.Names}}' | grep -q cdr_pdhc_db; then
  echo "Starting PostgreSQL container..."
  docker compose up -d db 2>/dev/null || docker-compose up -d db 2>/dev/null
  sleep 3
fi

# Step 6: Run migrations
export FLASK_APP=app
flask db upgrade 2>/dev/null || echo "No pending migrations"

# Step 7: Start gunicorn
echo "Starting gunicorn on port 9046..."
mkdir -p "$APP_DIR/logs"
gunicorn --bind 127.0.0.1:9046 --workers 2 --timeout 120 \
  "wsgi:app" \
  --daemon --pid /tmp/cdr_pdhc.pid \
  --access-logfile "$APP_DIR/logs/access.log" \
  --error-logfile "$APP_DIR/logs/error.log"

echo "=== cdr.pdhc started on port 9046 ==="
echo "PID: $(cat /tmp/cdr_pdhc.pid 2>/dev/null || echo 'unknown')"
echo "Logs: $APP_DIR/logs/"
