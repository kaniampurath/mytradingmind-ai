#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

VOLUME="${COMPOSE_PROJECT_NAME:-deploy}_mariadb_data"

echo "WARNING: This deletes the Docker MariaDB data volume: ${VOLUME}"
echo "Use this only when first-run credentials changed or AEGIS_DATABASE_URL/MARIADB_PASSWORD were mismatched."
echo "The reset removes only ${VOLUME}; app data, reports, logs, and backups are not deleted."
read -r -p "Type RESET_DB to continue: " answer
if [ "$answer" != "RESET_DB" ]; then
  echo "Cancelled."
  exit 1
fi

docker compose -f deploy/docker-compose.yml --env-file .env down
docker volume rm "${VOLUME}"
echo "Database volume removed. Re-run setup/start so MariaDB initializes with the current .env credentials."
