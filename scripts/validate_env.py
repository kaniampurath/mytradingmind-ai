from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


VALID_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    merged = {key: value for key, value in os.environ.items() if key.startswith(("AEGIS_", "MARIADB_", "REDIS_", "DASHBOARD_"))}
    merged.update(values)
    return merged


def parse_symbols(value: str) -> list[str]:
    if not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(decoded, list):
        raise ValueError("AEGIS_SYMBOLS must be a JSON list or comma-separated list")
    symbols = [str(item).strip() for item in decoded if str(item).strip()]
    bad = [symbol for symbol in symbols if not re.match(r"^[A-Z0-9]+/[A-Z0-9]+$", symbol)]
    if bad:
        raise ValueError(f"invalid symbol format: {', '.join(bad)}")
    return symbols


def check_env(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        symbols = parse_symbols(values.get("AEGIS_SYMBOLS", ""))
        if not symbols:
            errors.append("AEGIS_SYMBOLS is empty. Set it to a JSON list, for example [\"BTC/USDT\",\"ETH/USDT\"].")
    except ValueError as exc:
        errors.append(str(exc))

    database_url = values.get("AEGIS_DATABASE_URL", "")
    parsed_db = urlparse(database_url)
    if not parsed_db.scheme or not parsed_db.hostname or not parsed_db.path.strip("/"):
        errors.append("AEGIS_DATABASE_URL is invalid. Expected mysql+pymysql://user:password@host:port/schema.")
    else:
        mariadb_password = values.get("MARIADB_PASSWORD", "")
        if parsed_db.password and mariadb_password and parsed_db.password != mariadb_password:
            errors.append("AEGIS_DATABASE_URL password differs from MARIADB_PASSWORD. Make both values identical or reset the initialized DB volume.")
        mariadb_database = values.get("MARIADB_DATABASE", "")
        if mariadb_database and parsed_db.path.strip("/") != mariadb_database:
            errors.append("AEGIS_DATABASE_URL schema differs from MARIADB_DATABASE.")
        if parsed_db.port == 3306 and parsed_db.hostname in {"127.0.0.1", "localhost"}:
            warnings.append("AEGIS_DATABASE_URL uses host port 3306. Prefer 3307 on Ubuntu hosts that may already run MariaDB.")

    redis_url = values.get("AEGIS_REDIS_URL", "")
    parsed_redis = urlparse(redis_url)
    if parsed_redis.scheme != "redis" or not parsed_redis.hostname:
        errors.append("AEGIS_REDIS_URL is invalid. Expected redis://host:6379/0.")

    interval = values.get("AEGIS_BINANCE_HISTORY_INTERVAL", "1h")
    if interval not in VALID_INTERVALS:
        errors.append(f"AEGIS_BINANCE_HISTORY_INTERVAL={interval} is unsupported. Use one of {sorted(VALID_INTERVALS)}.")
    try:
        days = int(values.get("AEGIS_BINANCE_HISTORY_DAYS", "365"))
        if days < 30 or days > 3000:
            errors.append("AEGIS_BINANCE_HISTORY_DAYS must be between 30 and 3000.")
    except ValueError:
        errors.append("AEGIS_BINANCE_HISTORY_DAYS must be an integer.")

    bootstrap_email = values.get("AEGIS_BOOTSTRAP_ADMIN_EMAIL", "").strip()
    bootstrap_password = values.get("AEGIS_BOOTSTRAP_ADMIN_TEMP_PASSWORD", "").strip()
    if bool(bootstrap_email) != bool(bootstrap_password):
        errors.append(
            "Bootstrap admin setup is incomplete. Set both AEGIS_BOOTSTRAP_ADMIN_EMAIL and "
            "AEGIS_BOOTSTRAP_ADMIN_TEMP_PASSWORD, or leave both empty."
        )
    if bootstrap_password and len(bootstrap_password) < 12:
        errors.append("AEGIS_BOOTSTRAP_ADMIN_TEMP_PASSWORD must be at least 12 characters.")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate mytradingmind.ai .env before Docker startup.")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    errors, warnings = check_env(load_env(Path(args.env_file)))
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        print("FAIL: .env validation failed.")
        for error in errors:
            print(f"- {error}")
        print("Remediation: edit .env, then rerun python scripts/validate_env.py --env-file .env")
        return 1
    print("PASS: .env validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
