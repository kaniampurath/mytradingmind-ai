#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "Preparing mytradingmind.ai local deployment workspace"

mkdir -p data reports logs backups

echo "Running Ubuntu pre-install checks"
if command -v python3 >/dev/null 2>&1; then
  python3 scripts/preinstall_check_ubuntu.py || {
    echo "Pre-install validation failed. Fix the actionable messages above, then rerun setup.sh."
    exit 1
  }
else
  echo "python3 is required before setup can continue."
  exit 1
fi

if [ ! -f ".env" ]; then
  cp deploy/ubuntu.env.example .env
  echo "Created .env from deploy/ubuntu.env.example"
  echo "Edit .env before starting services. Do not commit it."
else
  echo ".env already exists; leaving it unchanged"
fi

python3 scripts/validate_env.py --env-file .env || {
  echo ".env validation failed. Fix credentials/symbols first."
  echo "If MariaDB was already initialized with old credentials, run scripts/reset_docker_db.sh after correcting .env."
  exit 1
}

if command -v docker >/dev/null 2>&1; then
  echo "Docker detected"
else
  echo "Docker is not installed. Install Docker Engine and Compose before running docker compose."
fi

echo "Next commands:"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mariadb redis"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard python scripts/init_db.py --print-tables"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard python scripts/binance_backfill.py"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mytradingmind_runtime mytradingmind_dashboard scanner"
echo "  scripts/install_sanity_ubuntu.sh"
