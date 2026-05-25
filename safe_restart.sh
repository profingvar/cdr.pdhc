#!/usr/bin/env bash
# cdr.pdhc safe_restart.sh — thin wrapper.
# start.sh is now idempotent and model-aware (cdr1 hybrid vs cdr2..5
# dockerized), so safe_restart is just an alias. Kept as a separate
# entry point because restart_all.sh and operator habit reach for it.
exec "$(cd "$(dirname "$0")" && pwd)/start.sh" "$@"
