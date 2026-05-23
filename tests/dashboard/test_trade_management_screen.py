from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards.app import trade_lifecycle_state, trade_management_summary
from aegis_trader.dashboards.app import merge_bot_frames


def test_trade_management_screen_is_registered_in_navigation() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    screen_options = text[text.index("SCREEN_OPTIONS = [") : text.index("def resolve_screen_request")]

    assert '"TRADE MANAGEMENT"' in screen_options
    assert "def trade_management_screen" in text
    assert 'elif page == "TRADE MANAGEMENT"' in text
    assert "trade_management_screen(data)" in text


def test_ui_bot_merge_prefers_headless_fresh_persistent_state() -> None:
    db_frame = pd.DataFrame(
        [
            {
                "name": "Headless Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "BTC/USDT",
                "state": "BACKTESTED",
                "updated_at": "2026-05-22T00:00:00+00:00",
            }
        ]
    )
    file_frame = pd.DataFrame(
        [
            {
                "name": "Headless Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "BTC/USDT",
                "state": "RUNNING",
                "updated_at": "2026-05-23T00:00:00+00:00",
            }
        ]
    )

    merged = merge_bot_frames(db_frame, file_frame)

    assert merged.iloc[0]["state"] == "RUNNING"


def test_trade_lifecycle_state_keeps_order_position_and_trade_separate() -> None:
    assert trade_lifecycle_state("RUNNING", "ACKNOWLEDGED", 100.0, 0.0) == "Active"
    assert trade_lifecycle_state("RUNNING", "PARTIALLY_FILLED", 100.0, 0.0) == "Partially Filled"
    assert trade_lifecycle_state("STOPPED", "CANCELLED", 0.0, 0.0) == "Cancelled"
    assert trade_lifecycle_state("FAILED", "REJECTED", 0.0, 0.0) == "Failed"


def test_trade_management_summary_is_nan_safe() -> None:
    active = pd.DataFrame(
        [
            {
                "Position State": "Open",
                "Exposure": 250.0,
                "Unrealized P&L": float("nan"),
                "Drawdown %": -2.5,
            }
        ]
    )
    closed = pd.DataFrame([{"Realized P&L": 12.0}])
    alerts = pd.DataFrame([{"level": "WARNING"}])

    summary = trade_management_summary(active, closed, alerts)

    assert summary["Active P&L"] == 0.0
    assert summary["Daily P&L"] == 12.0
    assert summary["Exposure"] == 250.0
    assert summary["Active Trades"] == 1
    assert summary["Alerts"] == 1


def test_trade_management_reuses_existing_sources_without_new_master_trade_table() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    screen_body = text[text.index("def trade_management_screen") : text.index("def critical_log_events")]

    assert "load_runtime_order_audit" in text
    assert "load_runtime_trade_events" in text
    assert "load_runtime_trade_pnl_snapshots" in text
    assert "load_journal_events" in screen_body
    assert "load_validation_runs_frame" in screen_body
    assert "load_backtest_trades" in screen_body
    assert "runtime_trade_events.json" in screen_body
    assert "runtime_trade_pnl_snapshots.json" in screen_body


def test_trade_management_uses_live_numbers_without_page_refresh() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    screen_body = text[text.index("def trade_management_screen") : text.index("def critical_log_events")]

    assert "calm_auto_refresh(10)" not in screen_body
    assert "trade_management_live_summary_component" in screen_body
    assert "trade_management_live_analytics_component(active)" in screen_body
    assert "Page does not auto-refresh" in text
    assert "Live Runtime Analytics" in text
    assert "streaming live analytics" in text
    assert "st.tabs" in screen_body
    assert "Active Desk" in screen_body
    assert "Audit Trail" in screen_body
    assert "persist_trade_pnl_snapshots(active)" in screen_body
