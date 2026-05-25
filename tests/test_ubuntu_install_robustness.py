from __future__ import annotations

from pathlib import Path
import shutil

import pandas as pd

from aegis_trader.analytics.replay_metrics import SymbolMetrics, Trade, write_reports
from aegis_trader.core.config import Settings
from scripts.validate_env import check_env, load_env


def test_empty_symbols_env_does_not_crash_settings() -> None:
    settings = Settings(symbols="", _env_file=None)

    assert settings.symbols == []


def test_symbols_accept_json_and_csv() -> None:
    assert Settings(symbols='["BTC/USDT","ETH/USDT"]', _env_file=None).symbols == ["BTC/USDT", "ETH/USDT"]
    assert Settings(symbols="BTC/USDT,ETH/USDT", _env_file=None).symbols == ["BTC/USDT", "ETH/USDT"]


def test_validate_env_catches_password_mismatch() -> None:
    errors, warnings = check_env(
        {
            "AEGIS_SYMBOLS": '["BTC/USDT"]',
            "AEGIS_DATABASE_URL": "mysql+pymysql://tradeuser:one@mariadb:3306/bots",
            "MARIADB_PASSWORD": "two",
            "MARIADB_DATABASE": "bots",
            "AEGIS_REDIS_URL": "redis://redis:6379/0",
            "AEGIS_BINANCE_HISTORY_INTERVAL": "1h",
            "AEGIS_BINANCE_HISTORY_DAYS": "365",
        }
    )

    assert warnings == []
    assert any("password differs" in error for error in errors)


def test_validate_env_example_is_valid() -> None:
    errors, _ = check_env(load_env(Path("deploy/ubuntu.env.example")))

    assert errors == []


def test_write_reports_handles_empty_metrics() -> None:
    out_dir = Path("reports/test_empty_reports")
    shutil.rmtree(out_dir, ignore_errors=True)
    write_reports([], [], out_dir)

    metrics = pd.read_csv(out_dir / "top10_replay_metrics.csv")
    trades = pd.read_csv(out_dir / "top10_replay_trades.csv")
    live_scan = (out_dir / "live_scan.json").read_text(encoding="utf-8")

    assert list(metrics.columns) == [field for field in SymbolMetrics.__dataclass_fields__]
    assert list(trades.columns) == [field for field in Trade.__dataclass_fields__]
    assert live_scan.strip() == "[]"
    shutil.rmtree(out_dir, ignore_errors=True)


def test_docker_compose_has_restart_healthchecks_and_configurable_ports() -> None:
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")

    for service in ["mytradingmind_dashboard", "mytradingmind_runtime", "scanner", "mariadb", "redis"]:
        assert service in compose
    assert compose.count("restart: unless-stopped") >= 5
    assert "${MARIADB_HOST_PORT:-3307}:3306" in compose
    assert "${DASHBOARD_PORT:-8501}:8501" in compose
    assert "redis-cli" in compose
    assert "_stcore/health" in compose


def test_setup_invokes_preinstall_and_env_validation() -> None:
    setup = Path("setup.sh").read_text(encoding="utf-8")

    assert "scripts/preinstall_check_ubuntu.py" in setup
    assert "scripts/validate_env.py --env-file .env" in setup
    assert "scripts/install_sanity_ubuntu.sh" in setup
    assert "scripts/upgrade_ubuntu.sh" in setup


def test_upgrade_script_checks_version_database_and_restarts_services() -> None:
    text = Path("scripts/upgrade_ubuntu.sh").read_text(encoding="utf-8")

    assert "git fetch --tags origin" in text
    assert "latest_release_tag" in text
    assert "scripts/validate_env.py --env-file .env" in text
    assert "python scripts/init_db.py --print-tables" in text
    assert "python scripts/enterprise_security_test.py --concurrent-users 10" in text
    assert "up -d mytradingmind_runtime mytradingmind_dashboard scanner" in text
    assert "python scripts/runtime_diagnostics.py" in text
