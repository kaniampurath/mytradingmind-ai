# MariaDB Notes For Ubuntu/DigitalOcean

The preferred droplet path is Docker Compose. See:

```text
docs/UBUNTU_DROPLET_DEPLOYMENT.md
```

For a standalone MariaDB install, use schema/database `bots` and table prefix `myts_bot_table_`.

```bash
sudo apt update
sudo apt install -y mariadb-server
sudo mysql_secure_installation
```

Create database and user:

```sql
CREATE DATABASE bots CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'tradeuser'@'localhost' IDENTIFIED BY 'replace_with_strong_password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON bots.* TO 'tradeuser'@'localhost';
FLUSH PRIVILEGES;
```

App environment:

```bash
AEGIS_DATABASE_ENABLED=true
AEGIS_DATABASE_SCHEMA=bots
AEGIS_DATABASE_URL=mysql+pymysql://tradeuser:replace_with_strong_password@127.0.0.1:3306/bots
```

Initialize schema:

```bash
python scripts/init_db.py
```

Operational posture:

- Keep `.env` out of Git.
- Keep historical replay data in Parquet files or object storage.
- Use MariaDB for bot state, risk settings, journal events, validation runs, and live scan state.
- Add Alembic migrations before real-money live trading.
- Back up with `mariadb-dump bots > bots.sql`.
