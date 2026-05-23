from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards.app import calculate_position_size_decision, certification_gate_result, resolve_screen_request


def test_live_trading_and_signal_flow_are_migrated_to_dashboard_navigation() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    screen_options = text[text.index("SCREEN_OPTIONS = [") : text.index("def resolve_screen_request")]

    assert '"LIVE TRADING"' not in screen_options
    assert '"SIGNAL FLOW"' not in screen_options
    assert resolve_screen_request("LIVE TRADING") == ("DASHBOARD", "")
    assert resolve_screen_request("SIGNAL FLOW") == ("DASHBOARD", "")
    assert "market_bucket_swim_lanes(scan, bots)" in text[text.index("def dashboard_screen") : text.index("def strategy_backtest_ranking")]


def test_position_size_decision_caps_quantity_and_audits_reasoning() -> None:
    decision = calculate_position_size_decision(
        bot_id="bot-1",
        strategy_id="ATR Trend Burst",
        symbol="BTC/USDT",
        sizing_method="volatility-linked sizing",
        capital=10_000.0,
        risk_per_trade=0.01,
        max_allocation=1_000.0,
        stop_loss_distance=100.0,
        price=50_000.0,
        volatility_value=0.05,
        max_portfolio_exposure=800.0,
        maximum_concurrent_trades=5,
        lot_size=0.0001,
    )

    assert decision["final_quantity"] <= 800.0 / 50_000.0
    assert decision["risk_amount"] == 100.0
    assert "max_portfolio_exposure" in decision["cap_applied_json"]
    assert "volatility_throttle" in decision["cap_applied_json"]
    assert decision["allocation_percentage"] >= 0.0


def test_marketplace_gate_requires_human_approval() -> None:
    bot = pd.Series(
        {
            "name": "Approval Bot",
            "strategy": "Certified Risk Managed Composite",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "capital": 500.0,
            "parameters": {"human_approval_status": "PENDING"},
        }
    )

    result = certification_gate_result(bot)

    assert result["Marketplace visible"] is False
    assert "human approval" in result["Gate detail"]


def test_bot_admin_contains_emergency_controls_and_audit_persistence() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    body = text[text.index("def bot_admin_screen") : text.index("def runtime_bot_rankings")]

    assert "Emergency controls" in body
    assert "STOP ALL BOTS" in body
    assert "DISABLE NEW LAUNCHES" in body
    assert "ENABLE RISK LOCK" in body
    assert "append_action_audit" in body
