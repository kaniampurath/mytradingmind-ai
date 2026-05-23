#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_FILE="${ENV_FILE:-.env}"
SCHEMA_NAME="${DATABASE_SCHEMA:-bots}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

DATABASE_URL="${DATABASE_URL:-${MYSQL_DATABASE_URL:-}}"

if [ -z "$DATABASE_URL" ]; then
  echo "DATABASE_URL is required. Example:"
  echo "DATABASE_URL=mysql+pymysql://tradeuser:CHANGE_ME@127.0.0.1:3306/${SCHEMA_NAME}"
  exit 2
fi

echo "Creating/verifying mytradingmind.ai database schema: ${SCHEMA_NAME}"
"$PYTHON_BIN" scripts/init_db.py --database-url "$DATABASE_URL" --print-tables

echo "Database bootstrap complete."
