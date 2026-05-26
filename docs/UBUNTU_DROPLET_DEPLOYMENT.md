# Ubuntu / DigitalOcean Deployment Guide

This guide installs mytradingmind.ai v1.2.11 on an Ubuntu droplet using Docker Compose, MariaDB 10.11, Redis 7, a headless bot runtime, scanner, and Streamlit operator dashboard.

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
sudo apt install -y git docker.io python3
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in, or reboot, after adding the user to the `docker` group. On Ubuntu variants where `docker-compose-plugin` is unavailable, install Docker from the official Docker apt repository, then verify with `docker compose version`.

## 3. Clone From GitHub

Use the current release tag for a stable deployment. `v1.0` is the preserved baseline; `v1.2.11` is the current main release.

```bash
git clone https://github.com/kaniampurath/mytradingmind-ai.git
cd mytradingmind-ai
git checkout v1.2.11
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
MARIADB_HOST_PORT=3307
DASHBOARD_PORT=8501
```

Keep:

```text
AEGIS_MODE=PAPER_MODE
AEGIS_BINANCE_TESTNET=true
AEGIS_DATABASE_SCHEMA=bots
AEGIS_DATABASE_ENABLED=true
AEGIS_SYMBOLS=["BTC/USDT","ETH/USDT","BNB/USDT","XRP/USDT","SOL/USDT","DOGE/USDT","ADA/USDT","TRX/USDT","LINK/USDT","AVAX/USDT"]
AEGIS_BOOTSTRAP_ADMIN_EMAIL=
AEGIS_BOOTSTRAP_ADMIN_TEMP_PASSWORD=
```

Validate before starting containers:

```bash
scripts/preinstall_check_ubuntu.sh
python3 scripts/validate_env.py --env-file .env
```

If validation reports a DB password mismatch after MariaDB already initialized, fix `.env` first, then run:

```bash
scripts/reset_docker_db.sh
```

## 5. Start Core Services

```bash
mkdir -p data reports logs
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mariadb redis
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/init_db.py --print-tables
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/enterprise_security_test.py --concurrent-users 10
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
- `users`, `roles`, `permissions`, `screens`, `actions`
- `user_roles`, `role_permissions`, `role_screens`
- `subscriptions`, `user_bot_subscriptions`, `billing_history`
- `sessions`, `audit_trail`, `activation_tokens`
- `admin_bootstrap_credentials`

## 5A. Upgrade An Existing Ubuntu Install

For a droplet that already has `.env`, Docker volumes, and services, use the
upgrade path instead of deleting or recreating the database:

```bash
cd mytradingmind-ai
bash setup.sh --upgrade --target-version latest
```

The upgrade flow:

- fetches GitHub tags and checks out the latest release tag
- preserves `.env` and adds only missing keys from `deploy/ubuntu.env.example`
- validates Docker, ports, symbols, Redis, and MariaDB credentials
- starts MariaDB/Redis and runs `scripts/init_db.py --print-tables`
- applies additive schema changes without dropping existing data
- rebuilds and restarts dashboard, headless runtime, and scanner
- runs runtime diagnostics and prints the running version

To install a specific release:

```bash
bash setup.sh --upgrade --target-version v1.2.11
```

To refresh historical Binance feature files during upgrade:

```bash
bash setup.sh --upgrade --target-version latest --backfill
```

Security bootstrap is idempotent. Default roles are `BASIC_USER`, `POWER_USER`, and `ADMIN`. Passwords, activation tokens, reset tokens, and bootstrap credentials are stored only as hashes. To create a first-use admin bootstrap credential, set both `AEGIS_BOOTSTRAP_ADMIN_EMAIL` and `AEGIS_BOOTSTRAP_ADMIN_TEMP_PASSWORD` before DB initialization.

## 6. Load Market Data

Backfill one year of Binance public candle features:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard \
  python scripts/binance_backfill.py --transport python
```

The backfill writes dashboard-compatible files such as `data/binance/BTCUSDT_1h_365d_features.parquet` and matching CSV files.

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
scripts/install_sanity_ubuntu.sh
docker compose -f deploy/docker-compose.yml --env-file .env ps
docker compose -f deploy/docker-compose.yml --env-file .env logs --tail=80 mytradingmind_dashboard
docker compose -f deploy/docker-compose.yml --env-file .env logs --tail=80 mytradingmind_runtime
docker compose -f deploy/docker-compose.yml --env-file .env exec mytradingmind_runtime \
  python -m mytradingmind.runtime status
python3 scripts/runtime_diagnostics.py --dashboard-url http://127.0.0.1:8501/_stcore/health
tail -n 80 logs/mytradingmind.log
```

Expected:

- dashboard service is running
- headless runtime service is running or reports controlled `HEADLESS` state
- scanner service is running
- MariaDB service is healthy
- dashboard health endpoint returns HTTP 200
- scanner heartbeat and replay report files are present

## 9. Reboot Verification

Services use `restart: unless-stopped`. After reboot:

```bash
scripts/reboot_verify_ubuntu.sh
```

Expected:

- Docker is enabled and active
- containers restart automatically
- dashboard is reachable
- diagnostics show fresh scanner/report state
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
git checkout v1.2.11
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
git checkout v1.2.11
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

## 13. Live Trading Warning

This deployment guide is for paper/testnet operation. Keep `AEGIS_MODE=PAPER_MODE` and `AEGIS_BINANCE_TESTNET=true` until the institutional readiness gates in `docs/INSTITUTIONAL_READINESS.md` are complete.
