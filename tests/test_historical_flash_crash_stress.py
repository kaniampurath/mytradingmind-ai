from __future__ import annotations

import pandas as pd

from aegis_trader.analytics.replay_metrics import Trade
from scripts.historical_flash_crash_stress import (
    CrashEvent,
    data_coverage,
    event_window,
    market_drop_pct,
    select_strategies,
    trade_overlaps_event,
)


def test_flash_crash_window_coverage_and_drop() -> None:
    event = CrashEvent("synthetic crash", "2026-02-05T00:00:00+00:00", "2026-02-05T03:00:00+00:00", "test", "")
    frame = pd.DataFrame(
        {
            "open_time": pd.date_range("2026-02-04T22:00:00Z", periods=8, freq="h"),
            "open": [100, 101, 100, 95, 90, 93, 96, 97],
            "high": [102, 102, 101, 96, 94, 97, 98, 99],
            "low": [99, 100, 94, 88, 86, 90, 95, 96],
            "close": [101, 100, 95, 90, 93, 96, 97, 98],
        }
    )

    assert data_coverage(frame, event) == "covered"
    assert len(event_window(frame, event, warmup_bars=1, cooldown_bars=1)) == 6
    assert market_drop_pct(frame, event) == -14.0


def test_trade_overlap_detects_position_open_during_crash() -> None:
    event = CrashEvent("synthetic crash", "2026-02-05T00:00:00+00:00", "2026-02-05T03:00:00+00:00", "test", "")
    overlapping = Trade("BTC/USDT", "2026-02-04T23:00:00+00:00", "2026-02-05T01:00:00+00:00", 100, 90, 88, 110, 2, -10, -100, "stop")
    before = Trade("BTC/USDT", "2026-02-04T18:00:00+00:00", "2026-02-04T20:00:00+00:00", 100, 101, 95, 110, 2, 1, 10, "time")

    assert trade_overlaps_event(overlapping, event)
    assert not trade_overlaps_event(before, event)


def test_flash_crash_strategy_selector_defaults_to_active_only() -> None:
    selected = select_strategies("active")

    assert list(selected) == ["KCJ ATR Trend Burst 5m", "TradingView Mean Reversion ATR 1h", "Certified Risk Managed Composite"]


def test_flash_crash_strategy_selector_accepts_named_subset() -> None:
    selected = select_strategies("all", "ATR Trend Burst,Certified Risk Managed Composite")

    assert list(selected) == ["ATR Trend Burst", "Certified Risk Managed Composite"]
