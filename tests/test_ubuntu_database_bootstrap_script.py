from __future__ import annotations

from pathlib import Path

from aegis_trader.storage.models import Base


def test_ubuntu_database_bootstrap_script_uses_application_schema_creator() -> None:
    text = Path("scripts/create_ubuntu_database.sh").read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env sh")
    assert "DATABASE_URL is required" in text
    assert "scripts/init_db.py --database-url \"$DATABASE_URL\" --print-tables" in text
    assert "mysql+pymysql://tradeuser:CHANGE_ME@127.0.0.1:3306/${SCHEMA_NAME}" in text


def test_init_db_prints_all_application_tables_from_models() -> None:
    text = Path("scripts/init_db.py").read_text(encoding="utf-8")
    expected_tables = sorted(table.name for table in Base.metadata.tables.values())

    assert "--print-tables" in text
    assert "Base.metadata.tables" in text
    assert expected_tables == [
        "myts_bot_table_bot_instances",
        "myts_bot_table_journal_events",
        "myts_bot_table_live_scan",
        "myts_bot_table_replay_metrics",
        "myts_bot_table_replay_trades",
        "myts_bot_table_risk_settings",
        "myts_bot_table_scanner_heartbeat",
        "myts_bot_table_validation_runs",
    ]
