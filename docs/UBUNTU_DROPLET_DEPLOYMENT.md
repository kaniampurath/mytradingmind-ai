# Ubuntu / DigitalOcean Deployment Guide

This guide installs mytradingmind.ai v1.2 on an Ubuntu droplet using Docker Compose, MariaDB 10.11, Redis 7, a headless bot runtime, scanner, and Streamlit operator dashboard.

## 1. Create The Droplet

Recommended starting size:

- Ubuntu 24.04 LTS
- 2 vCPU
- 4 GB RAM
- 50 GB disk

Firewall:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8501/tcp
sudo ufw enable
```

Keep MariaDB private. Do not expose port `3306` publicly unless you explicitly need remote administration.

## 2. Install System Packages

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

## 3. Clone From GitHub

Use the current release tag for a stable deployment. `v1.0` is the preserved baseline; `v1.2` is the current main release.

```bash
git clone https://github.com/kaniampurath/mytradingmind-ai.git
cd mytradingmind-ai
git checkout v1.2
```

## 4. Create Environment File

```bash
cp deploy/ubuntu.env.example .env
nano .env
```

Change at minimum:

```text
AEGIS_DATABASE_URL=mysql+pymysql://tradeuser:<strong_password>@mariadb:3306/bots
MARIADB_PASSWORD=<strong_password>
MARIADB_ROOT_PASSWORD=<strong_root_password>
```

Keep:

```text
AEGIS_MODE=PAPER_MODE
AEGIS_BINANCE_TESTNET=true
AEGIS_DATABASE_SCHEMA=bots
AEGIS_DATABASE_ENABLED=true
```

## 5. Start Core Services

```bash
mkdir -p data reports logs
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mariadb redis
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/init_db.py --print-tables
```

For a non-Docker Python install on Ubuntu, initialize the same schema with:

```bash
export DATABASE_URL='mysql+pymysql://tradeuser:<strong_password>@127.0.0.1:3306/bots'
sh scripts/create_ubuntu_database.sh
```

The database bootstrap creates/verifies:

- `myts_bot_table_bot_instances`
- `myts_bot_table_journal_events`
- `myts_bot_table_live_scan`
- `myts_bot_table_replay_metrics`
- `myts_bot_table_replay_trades`
- `myts_bot_table_risk_settings`
- `myts_bot_table_scanner_heartbeat`
- `myts_bot_table_validation_runs`

## 6. Load Market Data

Backfill one year of Binance public candle features:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/binance_backfill.py --symbols ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,TRX/USDT --transport python
```

Generate strategy metrics into MariaDB:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/generate_top10_metrics.py --database
```

## 7. Start Dashboard, Runtime, And Scanner

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build \
  mytradingmind_runtime mytradingmind_dashboard scanner
```

Open:

```text
http://<droplet-ip>:8501
```

## 8. Verify

```bash
docker compose -f deploy/docker-compose.yml --env-file .env ps
docker compose -f deploy/docker-compose.yml --env-file .env logs --tail=80 mytradingmind_dashboard
docker compose -f deploy/docker-compose.yml --env-file .env logs --tail=80 mytradingmind_runtime
docker compose -f deploy/docker-compose.yml --env-file .env exec mytradingmind_runtime \
  python -m mytradingmind.runtime status
tail -n 80 logs/mytradingmind.log
```

Expected:

- dashboard service is running
- headless runtime service is running or reports controlled `HEADLESS` state
- scanner service is running
- MariaDB service is healthy
- `logs/mytradingmind.log` has `logging_configured` and database diagnostic entries
- dashboard System Health shows database enabled

## 9. Headless Runtime Helpers

For non-Docker Ubuntu operation:

```bash
sh scripts/runtime_start.sh
sh scripts/runtime_monitor.sh --once
sh scripts/runtime_stop.sh
```

Runtime helper defaults:

- PID file: `reports/headless_runtime.pid`
- log file: `logs/headless_runtime.out`
- heartbeat: `HEARTBEAT_SECONDS=5`
- monitor interval: `MONITOR_INTERVAL_SECONDS=5`

## 10. Updating From GitHub

```bash
git pull
git checkout v1.2
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/init_db.py --print-tables
```

## 11. Backup

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec mariadb \
  mariadb-dump -u root -p"$MARIADB_ROOT_PASSWORD" bots > backups/bots_$(date +%F).sql
```

Create the `backups` directory before first use.

## 12. Version Rollback

Both release tags are independently deployable:

```bash
git fetch --tags
git checkout v1.0
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

Return to the current release:

```bash
git checkout v1.2
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

## 13. Live Trading Warning

This deployment guide is for paper/testnet operation. Keep `AEGIS_MODE=PAPER_MODE` and `AEGIS_BINANCE_TESTNET=true` until the institutional readiness gates in `docs/INSTITUTIONAL_READINESS.md` are complete.
