from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.core.config import settings
from aegis_trader.storage.bot_repository import _clean_dict
from aegis_trader.storage.db import normalize_async_database_url
from aegis_trader.storage.models import Base


def test_mariadb_url_is_async_and_schema_scoped() -> None:
    resolved = normalize_async_database_url(settings.database_url)

    assert resolved.startswith("mysql+aiomysql://")
    assert resolved.endswith("/bots")


def test_table_names_use_mytradingmind_operational_or_security_contract() -> None:
    names = {table.name for table in Base.metadata.tables.values()}
    security_tables = {
        "users",
        "roles",
        "permissions",
        "screens",
        "actions",
        "user_roles",
        "role_permissions",
        "role_screens",
        "subscriptions",
        "user_bot_subscriptions",
        "billing_history",
        "sessions",
        "audit_trail",
        "activation_tokens",
        "admin_bootstrap_credentials",
    }

    assert names
    assert all(name.startswith("myts_bot_table_") or name in security_tables for name in names)
    assert security_tables.issubset(names)


def test_repository_payload_cleaning_removes_dataframe_nulls() -> None:
    payload = _clean_dict({"deployed_at": pd.NaT, "capital": float("nan"), "name": "bot"})

    assert payload == {"deployed_at": None, "capital": None, "name": "bot"}


def test_bot_repository_exposes_delete_bot_instance() -> None:
    text = Path("aegis_trader/storage/bot_repository.py").read_text(encoding="utf-8")

    assert "async def delete_bot_instance" in text
    assert "delete(BotInstanceRow)" in text
    assert "BotInstanceRow.name == name" in text


def test_runtime_cagr_persistence_contract_is_in_schema() -> None:
    bot_columns = {column.name for column in Base.metadata.tables["app_schema.myts_bot_table_bot_instances"].columns}
    runtime_columns = {column.name for column in Base.metadata.tables["app_schema.myts_bot_table_runtime_events"].columns}

    assert {
        "bot_id",
        "cumulative_started_at",
        "cumulative_realized_pnl",
        "cumulative_trade_count",
        "runtime_position_state",
        "last_trade_event_type",
        "last_trade_event_at",
        "last_trade_event_reason",
    }.issubset(bot_columns)
    assert {"event_id", "bot_id", "event_type", "realized_pnl", "unrealized_pnl", "event_time"}.issubset(runtime_columns)
