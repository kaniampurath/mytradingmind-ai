#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
INTERVAL_SECONDS="${MONITOR_INTERVAL_SECONDS:-5}"
ONCE="${1:-}"

print_status() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
  "$PYTHON_BIN" -m mytradingmind.runtime status
}

if [ "$ONCE" = "--once" ]; then
  print_status
  exit 0
fi

while true; do
  print_status
  sleep "$INTERVAL_SECONDS"
done
