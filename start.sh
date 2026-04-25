#!/usr/bin/env bash
set -e

# macOS ObjC fork-safety: CoreFoundation in parent poisons fork()s; setting
# this env var before gunicorn prevents the SIGKILL spiral after worker recycles.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

PORTS=(9046)  # narrowed from (9046 9047 9048 9049) — 9047 was the colima ssh-mux proxy; killing it breaks the host↔VM bridge (see feedback memory)
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$PROJECT_DIR/cdr_app"
VENV_DIR="$APP_DIR/venv"
DC="docker-compose"
if [ -x /opt/homebrew/bin/docker-compose ]; then
    DC="/opt/homebrew/bin/docker-compose"
fi

# --- Load .env ---
if [ -f "$APP_DIR/.env" ]; then
    set -a
    source "$APP_DIR/.env"
    set +a
fi

# --- Kill processes on project ports ---
echo "Clearing ports ${PORTS[*]}..."
for port in "${PORTS[@]}"; do
    PIDS=$(lsof -ti :"$port" 2>/dev/null) && [ -n "$PIDS" ] && echo "$PIDS" | xargs kill -9 2>/dev/null || true
done

# --- Ensure Docker is running ---
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not running. Start Colima first:"
    echo "  colima start && docker context use colima"
    exit 1
fi

# --- Create venv if needed ---
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# --- Activate venv ---
source "$VENV_DIR/bin/activate"

# --- Install dependencies ---
echo "Installing dependencies..."
pip install -q -r "$APP_DIR/requirements.txt"

# --- Start PostgreSQL via Docker ---
echo "Starting PostgreSQL on port 9047..."
cd "$APP_DIR"
$DC up -d db
echo "Waiting for PostgreSQL..."
until $DC exec -T db pg_isready -U "${POSTGRES_USER:-cdr_user}" >/dev/null 2>&1; do
    sleep 1
done
echo "PostgreSQL is ready."

# --- Run migrations ---
cd "$APP_DIR"
export FLASK_APP=app
if [ ! -d "migrations/versions" ]; then
    echo "Initializing migrations..."
    flask db init
    flask db migrate -m "Initial migration"
fi
echo "Running migrations..."
flask db upgrade

# --- Start gunicorn (background) ---
echo "Starting gunicorn on port 9046..."
mkdir -p "$APP_DIR/logs"
nohup gunicorn \
    --bind 127.0.0.1:9046 \
    --workers 2 \
    --timeout 120 \
    --access-logfile "$APP_DIR/logs/access.log" \
    --error-logfile "$APP_DIR/logs/error.log" \
    --pid "$APP_DIR/gunicorn.pid" \
    "wsgi:app" >> "$APP_DIR/logs/gunicorn.out" 2>&1 &

sleep 2
echo ""
echo "=== cdr.pdhc is running ==="
echo "  App:      http://localhost:9046"
echo "  Database: localhost:9047"
echo "  PID:      $APP_DIR/gunicorn.pid"
echo "  Logs:     $APP_DIR/logs/"
echo "  Stop DB:  cd $APP_DIR && $DC down"
echo ""
