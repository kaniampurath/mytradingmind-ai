#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

systemctl is-enabled docker >/dev/null
systemctl is-active docker >/dev/null
systemctl is-enabled containerd >/dev/null || true
systemctl is-active containerd >/dev/null || true

docker compose -f deploy/docker-compose.yml --env-file .env ps
python3 scripts/runtime_diagnostics.py --dashboard-url "http://127.0.0.1:${DASHBOARD_PORT:-8501}/_stcore/health"
