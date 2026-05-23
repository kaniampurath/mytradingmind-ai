from __future__ import annotations

from scripts.production_readiness_stress import select_strategy_names


def test_production_readiness_selector_defaults_to_active_only() -> None:
    assert select_strategy_names("active") == ["KCJ ATR Trend Burst 5m", "TradingView Mean Reversion ATR 1h", "Certified Risk Managed Composite"]


def test_production_readiness_selector_accepts_named_subset() -> None:
    selected = select_strategy_names("all", "KCJ ATR Trend Burst 5m,Certified Risk Managed Composite")

    assert selected == ["KCJ ATR Trend Burst 5m", "Certified Risk Managed Composite"]
