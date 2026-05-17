from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from aegis_trader.core.config import settings
from aegis_trader.storage.db import normalize_async_database_url
from aegis_trader.storage.models import Base


REQUIRED_FILES = [
    "README.md",
    "pyproject.toml",
    "deploy/Dockerfile",
    "deploy/docker-compose.yml",
    "deploy/ubuntu.env.example",
    "docs/UBUNTU_DROPLET_DEPLOYMENT.md",
    "docs/INSTITUTIONAL_READINESS.md",
    "scripts/start_dashboard.py",
    "scripts/init_db.py",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deployment and institutional readiness checks.")
    parser.add_argument("--run-tests", action="store_true", help="Also execute pytest.")
    args = parser.parse_args()

    checks: list[dict[str, object]] = []
    checks.extend(_file_checks())
    checks.extend(_schema_checks())
    checks.extend(_env_checks())
    if args.run_tests:
        checks.append(_pytest_check())

    failed = [item for item in checks if item["status"] != "PASS"]
    report = {"status": "PASS" if not failed else "FAIL", "checks": checks}
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if failed else 0)


def _file_checks() -> list[dict[str, object]]:
    return [
        {
            "name": f"file:{path}",
            "status": "PASS" if Path(path).exists() else "FAIL",
            "detail": str(Path(path)),
        }
        for path in REQUIRED_FILES
    ]


def _schema_checks() -> list[dict[str, object]]:
    table_names = {table.name for table in Base.metadata.tables.values()}
    return [
        {
            "name": "mariadb_schema_name",
            "status": "PASS" if settings.database_schema == "bots" else "FAIL",
            "detail": settings.database_schema,
        },
        {
            "name": "mariadb_async_url",
            "status": "PASS" if normalize_async_database_url(settings.database_url).startswith("mysql+aiomysql://") else "FAIL",
            "detail": normalize_async_database_url(settings.database_url).split("@")[-1],
        },
        {
            "name": "table_prefix",
            "status": "PASS" if table_names and all(name.startswith("myts_bot_table_") for name in table_names) else "FAIL",
            "detail": sorted(table_names),
        },
    ]


def _env_checks() -> list[dict[str, object]]:
    return [
        {
            "name": "database_enabled",
            "status": "PASS" if settings.database_enabled else "WARN",
            "detail": settings.database_enabled,
        },
        {
            "name": "log_dir",
            "status": "PASS" if settings.log_dir else "FAIL",
            "detail": settings.log_dir,
        },
        {
            "name": "testnet_default",
            "status": "PASS" if settings.binance_testnet else "WARN",
            "detail": settings.binance_testnet,
        },
    ]


def _pytest_check() -> dict[str, object]:
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], capture_output=True, text=True, check=False)
    return {
        "name": "pytest",
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "detail": (result.stdout + result.stderr)[-2000:],
    }


if __name__ == "__main__":
    main()
