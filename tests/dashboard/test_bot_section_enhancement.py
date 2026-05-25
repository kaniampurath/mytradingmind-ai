from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards import app
from aegis_trader.dashboards.app import runtime_trade_position_status


def test_bot_framework_exposes_creation_defaults_and_persists_overrides() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert "def strategy_deployment_defaults" in text
    assert "Strategy Deployment Defaults" in text
    assert "Bot Creation Panel Defaults" in text
    assert "stop_loss_type" in text
    assert "take_profit_type" in text
    assert "trailing_enabled" in text
    assert "emergency_stop_enabled" in text
    assert "strategy_defaults_used" in text
    assert "risk_allocation_category" in text


def test_runtime_profile_contains_required_instance_level_metrics() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    runtime_profile = text[text.index("def runtime_instance_profile") : text.index("def risk_gate_for_bot")]
    for key in [
        "bot_instance_name",
        "strategy_type",
        "bot_version",
        "runtime_status",
        "trade_position_state",
        "trade_position_reason",
        "in_trade",
        "last_entry_at",
        "last_exit_at",
        "validation_status",
        "deployment_timestamp",
        "runtime_duration_hours",
        "real_time_pnl",
        "realized_pnl",
        "unrealized_pnl",
        "roi_pct",
        "current_exposure",
        "current_allocated_capital",
        "available_unallocated_capital",
        "capital_utilization_pct",
        "qty_per_order",
        "current_drawdown_pct",
        "peak_drawdown_pct",
        "current_bucket",
        "signal_status",
        "current_strategy_state",
        "backtest_data_range",
        "api_connectivity",
        "feed_status",
        "order_execution_status",
        "recovery_state",
        "execution_queue_state",
    ]:
        assert key in runtime_profile


def test_validation_lab_surfaces_deployment_outputs() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    validation_body = text[text.index("def validation_screen") : text.index("def run_bot_validation")]
    assert "Deployment Defaults And Runtime Compatibility" in validation_body
    assert "Recommended stop-loss" in validation_body
    assert "Recommended take-profit" in validation_body
    assert "Recommended capital allocation" in validation_body
    assert "deployment_readiness" in validation_body
    assert "Deployment validation output" in validation_body


def test_runtime_tiles_have_readable_boundaries_and_nan_safe_formatting() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert ".runtime-bot-boundary" in text
    assert "font-size: 1.02rem" in text
    assert "font-size: 0.82rem" in text
    assert "def safe_number" in text
    assert "def money_text" in text
    assert "def pct_text" in text
    assert 'return "$0.00"' in text
    assert "price unavailable" in text
    assert "IN_TRADE" in text
    assert "OUT_OF_TRADE" in text
    assert "trade_state_class" in text
    assert "class=\"pill {trade_state_class}\"" in text
    assert "class='pill {'" not in text
    assert "Trade state:" in text
    assert "In/out of trade" in text


def test_runtime_screen_merges_persisted_trade_state_after_ui_restart() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    runtime_body = text[text.index("def bot_runtime_screen") : text.index("def bot_admin_screen")]

    assert "RuntimeManager().list_bot_states()" in runtime_body
    assert '"status"' in runtime_body
    assert '"runtime_mode"' in runtime_body
    assert '"runtime_position_state"' in runtime_body
    assert '"last_entry_at"' in runtime_body
    assert '"last_exit_at"' in runtime_body
    assert '"last_trade_event_type"' in runtime_body
    assert '"last_trade_event_at"' in runtime_body
    assert '"last_trade_event_reason"' in runtime_body


def test_live_scan_without_symbol_column_falls_back_to_default_symbols() -> None:
    malformed = pd.DataFrame([{"status": "scanner_failed"}])

    normalized = app.ensure_default_live_symbols(malformed)

    assert "symbol" in normalized.columns
    assert set(app.DEFAULT_LIVE_SYMBOLS).issubset(set(normalized["symbol"].astype(str)))
    assert "scanner_failed" not in set(normalized["symbol"].astype(str))


def test_runtime_trade_position_status_shows_out_of_trade_after_exit_signal() -> None:
    bot = {"bot_id": "bot-1", "name": "Runtime Bot", "state": "RUNNING", "parameters": {}}
    events = pd.DataFrame(
        [
            {
                "bot_id": "bot-1",
                "event_type": "TradeEntered",
                "event_time": "2026-05-24T08:00:00+00:00",
                "position_state": "OPEN",
                "lifecycle_state": "Active",
            },
            {
                "bot_id": "bot-1",
                "event_type": "TradeExited",
                "event_time": "2026-05-24T09:00:00+00:00",
                "position_state": "FLAT",
                "lifecycle_state": "Closed",
            },
        ]
    )

    status = runtime_trade_position_status(bot, events)

    assert status["trade_position_state"] == "OUT_OF_TRADE"
    assert status["in_trade"] is False
    assert "exit signal recorded" in status["trade_position_reason"]
    assert str(status["last_exit_at"]).startswith("2026-05-24T09:00:00")


def test_runtime_trade_position_status_shows_in_trade_without_exit_signal() -> None:
    bot = {"bot_id": "bot-1", "name": "Runtime Bot", "state": "RUNNING", "parameters": {}}
    events = pd.DataFrame(
        [
            {
                "bot_id": "bot-1",
                "event_type": "TradeEntered",
                "event_time": "2026-05-24T08:00:00+00:00",
                "position_state": "OPEN",
                "lifecycle_state": "Active",
            }
        ]
    )

    status = runtime_trade_position_status(bot, events)

    assert status["trade_position_state"] == "IN_TRADE"
    assert status["in_trade"] is True


def test_runtime_trade_position_status_uses_persisted_runtime_status() -> None:
    bot = {
        "bot_id": "ETHUSDT_bot",
        "name": "ETHUSDT bot",
        "state": "BACKTESTED",
        "status": "RUNNING",
        "runtime_entry_price": 2039.21,
        "started_at": "2026-05-25T15:13:20+00:00",
        "parameters": {},
    }

    status = runtime_trade_position_status(bot, pd.DataFrame())

    assert status["trade_position_state"] == "IN_TRADE"
    assert status["in_trade"] is True
    assert "active entry price" in status["trade_position_reason"]


def test_trade_created_event_does_not_override_runtime_entry_price() -> None:
    bot = {
        "bot_id": "ETHUSDT_bot",
        "name": "ETHUSDT bot",
        "state": "BACKTESTED",
        "status": "RUNNING",
        "runtime_entry_price": 2039.21,
        "started_at": "2026-05-25T15:13:20+00:00",
        "parameters": {},
    }
    events = pd.DataFrame(
        [
            {
                "bot_id": "ETHUSDT_bot",
                "event_type": "TradeCreated",
                "event_time": "2026-05-25T15:13:20+00:00",
                "position_state": "FLAT",
                "lifecycle_state": "Pending",
            }
        ]
    )

    status = runtime_trade_position_status(bot, events)

    assert status["trade_position_state"] == "IN_TRADE"
    assert status["in_trade"] is True
    assert "active entry price" in status["trade_position_reason"]
