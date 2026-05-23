#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
MODE="${RUNTIME_MODE:-headless}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-5}"
PID_FILE="${RUNTIME_PID_FILE:-reports/headless_runtime.pid}"
LOG_FILE="${RUNTIME_LOG_FILE:-logs/headless_runtime.out}"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Headless runtime already appears to be running with PID $OLD_PID"
    "$PYTHON_BIN" -m mytradingmind.runtime status
    exit 0
  fi
fi

nohup "$PYTHON_BIN" -m mytradingmind.runtime start \
  --mode "$MODE" \
  --heartbeat-seconds "$HEARTBEAT_SECONDS" \
  > "$LOG_FILE" 2>&1 &

RUNTIME_PID="$!"
echo "$RUNTIME_PID" > "$PID_FILE"
echo "Started mytradingmind.ai headless runtime with PID $RUNTIME_PID"
echo "Log: $LOG_FILE"
"$PYTHON_BIN" -m mytradingmind.runtime status
