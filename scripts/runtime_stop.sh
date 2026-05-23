#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
PID_FILE="${RUNTIME_PID_FILE:-reports/headless_runtime.pid}"

"$PYTHON_BIN" -m mytradingmind.runtime stop

if [ -f "$PID_FILE" ]; then
  RUNTIME_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$RUNTIME_PID" ]; then
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$RUNTIME_PID" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if kill -0 "$RUNTIME_PID" 2>/dev/null; then
      echo "Runtime PID $RUNTIME_PID is still alive after graceful stop request; leaving process for supervisor inspection."
    fi
  fi
  rm -f "$PID_FILE"
fi

"$PYTHON_BIN" -m mytradingmind.runtime status
