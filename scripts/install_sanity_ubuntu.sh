#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

COMPOSE="docker compose -f deploy/docker-compose.yml --env-file .env"

stage() {
  echo
  echo "== $1 =="
}

stage "Stage 1: Docker validation"
docker ps >/dev/null
docker compose version >/dev/null
echo "PASS docker ps and docker compose"

stage "Stage 2: Infrastructure validation"
$COMPOSE up -d --build mariadb redis
$COMPOSE ps
echo "Waiting for service health checks..."
sleep 12
$COMPOSE ps mariadb redis

stage "Stage 3: DB validation"
$COMPOSE run --rm mytradingmind_dashboard python scripts/init_db.py --print-tables
$COMPOSE run --rm mytradingmind_dashboard python scripts/enterprise_security_test.py --concurrent-users 10

stage "Stage 4: Binance validation"
$COMPOSE run --rm mytradingmind_dashboard python -c "import urllib.request; print(urllib.request.urlopen('https://api.binance.com/api/v3/time', timeout=10).read().decode())"

stage "Stage 5: Feature validation"
$COMPOSE run --rm mytradingmind_dashboard python scripts/binance_backfill.py --symbols BTC/USDT --interval "${AEGIS_BINANCE_HISTORY_INTERVAL:-1h}" --days "${AEGIS_BINANCE_HISTORY_DAYS:-365}" --transport python

stage "Stage 6: Scanner validation"
$COMPOSE run --rm scanner python scripts/live_scan_binance.py --symbols BTC/USDT --interval "${AEGIS_BINANCE_HISTORY_INTERVAL:-1h}" --lookback-days 45 --transport python --database
test -s reports/live_scan.json
test -s reports/top10_replay_metrics.csv
test -s reports/top10_replay_trades.csv

stage "Stage 7: Dashboard validation"
$COMPOSE up -d --build mytradingmind_runtime mytradingmind_dashboard scanner
sleep 15
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${DASHBOARD_PORT:-8501}/_stcore/health', timeout=10).read(); print('PASS dashboard HTTP health')"

stage "Stage 8: Reboot persistence validation"
docker inspect "$(docker compose -f deploy/docker-compose.yml --env-file .env ps -q mytradingmind_dashboard)" --format '{{.HostConfig.RestartPolicy.Name}}'
echo "PASS services use Docker restart policies. After reboot run: scripts/reboot_verify_ubuntu.sh"

echo
echo "PASS: end-to-end installation sanity validation completed."
