# Ubuntu / DigitalOcean Deployment Guide

This guide installs mytradingmind.ai on an Ubuntu droplet using Docker Compose, MariaDB 10.11, Redis 7, and Streamlit.

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

Replace the URL with your repository URL after uploading.

```bash
git clone https://github.com/<your-org-or-user>/mytradingmind-ai.git
cd mytradingmind-ai
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
docker compose -f deploy/docker-compose.yml --env-file .env run --rm dashboard python scripts/init_db.py
```

## 6. Load Market Data

Backfill one year of Binance public candle features:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env run --rm dashboard \
  python scripts/binance_backfill.py --symbols ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,TRX/USDT --transport python
```

Generate strategy metrics into MariaDB:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env run --rm dashboard \
  python scripts/generate_top10_metrics.py --database
```

## 7. Start Dashboard And Scanner

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build dashboard scanner
```

Open:

```text
http://<droplet-ip>:8501
```

## 8. Verify

```bash
docker compose -f deploy/docker-compose.yml --env-file .env ps
docker compose -f deploy/docker-compose.yml --env-file .env logs --tail=80 dashboard
tail -n 80 logs/mytradingmind.log
```

Expected:

- dashboard service is running
- scanner service is running
- MariaDB service is healthy
- `logs/mytradingmind.log` has `logging_configured` and database diagnostic entries
- dashboard System Health shows database enabled

## 9. Updating From GitHub

```bash
git pull
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.yml --env-file .env run --rm dashboard python scripts/init_db.py
```

## 10. Backup

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec mariadb \
  mariadb-dump -u root -p"$MARIADB_ROOT_PASSWORD" bots > backups/bots_$(date +%F).sql
```

Create the `backups` directory before first use.

## 11. Live Trading Warning

This deployment guide is for paper/testnet operation. Keep `AEGIS_MODE=PAPER_MODE` and `AEGIS_BINANCE_TESTNET=true` until the institutional readiness gates in `docs/INSTITUTIONAL_READINESS.md` are complete.
