#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "Preparing mytradingmind.ai local deployment workspace"

mkdir -p data reports logs backups

if [ ! -f ".env" ]; then
  cp deploy/ubuntu.env.example .env
  echo "Created .env from deploy/ubuntu.env.example"
  echo "Edit .env before starting services. Do not commit it."
else
  echo ".env already exists; leaving it unchanged"
fi

if command -v docker >/dev/null 2>&1; then
  echo "Docker detected"
else
  echo "Docker is not installed. Install Docker Engine and Compose before running docker compose."
fi

echo "Next commands:"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mariadb redis"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env run --rm dashboard python scripts/init_db.py"
echo "  docker compose -f deploy/docker-compose.yml --env-file .env up -d --build dashboard scanner"
