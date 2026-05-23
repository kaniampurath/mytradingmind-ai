from __future__ import annotations

from pathlib import Path


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
