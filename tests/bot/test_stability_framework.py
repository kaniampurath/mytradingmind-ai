from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd

from aegis_trader.bot.framework import BotDeployment, StrategyAgnosticBot
from aegis_trader.bot.stability_framework import BotStabilityConfig, ExecutionSymbolRules, ProductionStabilityFramework
from aegis_trader.runtime.bot_registry import BotRegistry
from aegis_trader.runtime import runtime_manager as runtime_manager_module
from aegis_trader.runtime.runtime_manager import RuntimeManager
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY


def workspace_tmp() -> Path:
    path = Path("tmp") / "tests" / str(uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_execution_rules_sanitize_qty_and_price_without_exchange_calls() -> None:
    rules = ExecutionSymbolRules(min_qty=0.01, step_size=0.01, min_notional=10.0, tick_size=0.05, max_capital_usd=100.0)

    qty, reason = rules.sanitize_qty(price=20.0, qty=0.1)

    assert qty == 0.5
    assert reason == "NOTIONAL_BELOW_MIN"
    assert rules.sanitize_price(20.123) == 20.1
    assert rules.sanitize_qty(price=20.0, qty=10.0) == (None, "CAPITAL_EXCEEDED")


def test_framework_blocks_duplicate_and_overlapping_operations() -> None:
    framework = ProductionStabilityFramework()

    accepted, reason = framework.begin_operation("entry", event_key="BTC:bar-1:entry")
    duplicate, duplicate_reason = framework.begin_operation("entry", event_key="BTC:bar-1:entry")
    overlap, overlap_reason = framework.begin_operation("exit", event_key="BTC:bar-1:exit")
    framework.end_operation("entry")
    exit_ok, _ = framework.begin_operation("exit", event_key="BTC:bar-1:exit")

    assert accepted is True
    assert reason == "operation accepted"
    assert duplicate is False
    assert duplicate_reason == "duplicate event blocked"
    assert overlap is False
    assert overlap_reason == "operation already in flight"
    assert exit_ok is True


def test_framework_applies_production_risk_lockouts() -> None:
    framework = ProductionStabilityFramework(BotStabilityConfig(max_trades_day=2, max_consecutive_losses=2, max_daily_profit_usd=50.0))

    framework.record_trade_result(-5.0, equity=1_000.0)
    ok, _ = framework.can_open_trade(equity=1_000.0)
    framework.record_trade_result(-5.0, equity=1_000.0)
    blocked, reason = framework.can_open_trade(equity=1_000.0)

    assert ok is True
    assert blocked is False
    assert reason == "max trades per day reached"
    assert framework.state.risk_state == "MAX_TRADES_DAY"


def test_framework_detects_stale_heartbeat_for_restart() -> None:
    framework = ProductionStabilityFramework(BotStabilityConfig(heartbeat_stale_seconds=120))

    stale, reason = framework.detect_stale_heartbeat(datetime.now(UTC) - timedelta(seconds=121), datetime.now(UTC))

    assert stale is True
    assert reason == "heartbeat stale > 120s"
    assert framework.state.restart_required is True
    assert framework.state.supervisor_action == "RESTART"
    assert framework.state.alert_level == "WARNING"


def test_strategy_agnostic_bot_always_has_stability_framework() -> None:
    strategy = STRATEGY_REGISTRY["Certified Risk Managed Composite"]
    bot = StrategyAgnosticBot(BotDeployment("Stable Bot", strategy=strategy))

    assert isinstance(bot.framework, ProductionStabilityFramework)
    assert bot.framework.config.framework_name == "PRODUCTION_STABILITY_V2"


def test_framework_reconciles_restart_before_resume() -> None:
    framework = ProductionStabilityFramework()

    plan = framework.restart_reconciliation_plan(
        exchange_position_qty=1.0,
        local_position_qty=0.0,
        protection_verified=True,
        open_orders_verified=True,
    )

    assert plan.action == "HALT_AND_RECONCILE"
    assert framework.state.reconciliation_state == "MISMATCH"
    assert framework.state.alert_code == "POSITION_RECONCILIATION_MISMATCH"


def test_framework_blocks_stale_market_data_and_exposure() -> None:
    framework = ProductionStabilityFramework(BotStabilityConfig(market_data_stale_seconds=60, max_portfolio_exposure_pct=80.0))

    unhealthy = framework.data_feed_is_unhealthy(datetime.now(UTC) - timedelta(seconds=61), now=datetime.now(UTC))
    approved, reason = framework.check_portfolio_exposure(equity=1_000.0, total_exposure=900.0, symbol_exposure=100.0)

    assert unhealthy is True
    assert framework.state.data_state == "STALE"
    assert approved is False
    assert reason == "portfolio exposure limit reached"


def test_framework_records_order_lifecycle_and_alert_payloads() -> None:
    framework = ProductionStabilityFramework()

    event = framework.record_order_lifecycle(
        bot_id="Stable_Runtime_Bot",
        client_order_id="order-1",
        symbol="BTC/USDT",
        side="BUY",
        status="SUBMITTED",
        quantity=0.1,
        price=100.0,
        reason="test",
    )
    payloads = framework.alert_payloads(("webhook", "telegram"))

    assert event.bot_id == "Stable_Runtime_Bot"
    assert framework.seen_event("Stable_Runtime_Bot:order-1:SUBMITTED")
    assert [payload["channel"] for payload in payloads] == ["webhook", "telegram"]


def test_runtime_manager_attaches_framework_state_to_each_running_bot() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Stable Runtime Bot", "strategy": "Certified Risk Managed Composite", "symbol": "BTC/USDT", "state": "BACKTESTED"}]))
    manager = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json")

    state = manager.start_bot("Stable_Runtime_Bot", source="TEST")
    manager.runtime_heartbeat("HEADLESS")
    recovered = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json").list_bot_states()[0]

    assert state["framework"] == "PRODUCTION_STABILITY_V2"
    assert state["framework_status"] == "READY"
    assert state["started_at"]
    assert state["pnl_started_at"] == state["started_at"]
    assert recovered["framework"] == "PRODUCTION_STABILITY_V2"
    assert recovered["risk_state"] == "OK"
    assert recovered["supervisor_action"] == "NONE"
    assert recovered["alert_level"] == "INFO"


def test_runtime_manager_persists_trade_events_and_pnl_snapshots(monkeypatch) -> None:
    tmp_path = workspace_tmp()
    monkeypatch.setattr(runtime_manager_module, "RUNTIME_ORDER_AUDIT_PATH", tmp_path / "runtime_order_audit.json")
    monkeypatch.setattr(runtime_manager_module, "RUNTIME_TRADE_EVENTS_PATH", tmp_path / "runtime_trade_events.json")
    monkeypatch.setattr(runtime_manager_module, "RUNTIME_TRADE_PNL_SNAPSHOTS_PATH", tmp_path / "runtime_trade_pnl_snapshots.json")
    manager = RuntimeManager(state_path=tmp_path / "runtime_state.json")

    manager.record_order_lifecycle(
        bot_id="Stable_Runtime_Bot",
        client_order_id="order-2",
        symbol="BTC/USDT",
        side="BUY",
        status="SUBMITTED",
        quantity=0.1,
        price=100.0,
        reason="test persistence",
    )
    manager.record_trade_pnl_snapshot(
        bot_id="Stable_Runtime_Bot",
        trade_id="Stable_Runtime_Bot:BTCUSDT:order-2",
        symbol="BTC/USDT",
        current_price=101.0,
        unrealized_pnl=1.0,
        realized_pnl=0.0,
        roi_pct=1.0,
        exposure=100.0,
        drawdown_pct=0.0,
        lifecycle_state="Active",
    )

    events = json.loads(runtime_manager_module.RUNTIME_TRADE_EVENTS_PATH.read_text(encoding="utf-8"))
    snapshots = json.loads(runtime_manager_module.RUNTIME_TRADE_PNL_SNAPSHOTS_PATH.read_text(encoding="utf-8"))

    assert events[-1]["event_type"] == "TradeEntered"
    assert events[-1]["order_state"] == "SUBMITTED"
    assert snapshots[-1]["unrealized_pnl"] == 1.0
    assert snapshots[-1]["lifecycle_state"] == "Active"


def test_runtime_manager_reconciliation_surfaces_recovery_plan() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Recovery Bot", "strategy": "ATR Trend Burst", "symbol": "BTC/USDT", "state": "BACKTESTED"}]))
    manager = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json")
    manager.start_bot("Recovery_Bot", source="TEST")

    state = manager.reconcile_bot(
        "Recovery_Bot",
        exchange_position_qty=1.0,
        local_position_qty=0.0,
        protection_verified=True,
        open_orders_verified=True,
    )

    assert state["reconciliation_plan"]["action"] == "HALT_AND_RECONCILE"
    assert state["reconciliation_state"] == "MISMATCH"


def test_runtime_supervisor_restarts_from_fresh_heartbeat_evaluation() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Supervisor Bot", "strategy": "ATR Trend Burst", "symbol": "BTC/USDT", "state": "BACKTESTED"}]))
    manager = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json")
    manager.start_bot("Supervisor_Bot", source="TEST")
    stale_state = manager.list_bot_states()[0]
    stale_state["last_heartbeat"] = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    manager._upsert_state(stale_state)

    manager.runtime_heartbeat("HEADLESS")
    recovered = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json").list_bot_states()[0]

    assert recovered["status"] == "RUNNING"
    assert recovered["restart_required"] is False
    assert recovered["supervisor_action"] == "NONE"
