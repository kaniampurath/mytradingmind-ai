from __future__ import annotations

import pandas as pd

from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY
from aegis_trader.strategies.exits import DAILY_EXIT_PRIORITY, DailyExitConfig, daily_timeframe_exit


def test_daily_exit_priority_prefers_hard_stop_before_trailing_stop() -> None:
    active = {"entry": 100.0, "stop": 96.0, "take_profit": 120.0, "bars": 5, "highest_high": 115.0, "lowest_low": 99.0}
    row = pd.Series({"close": 89.0, "high": 116.0, "low": 88.0, "atr14": 5.0, "ema20": 95.0})

    decision = daily_timeframe_exit(row, active)

    assert DAILY_EXIT_PRIORITY == ("HARD_STOP", "ATR_TRAIL", "TREND_BREAK", "TIME_STOP")
    assert decision is not None
    assert decision.reason == "HARD_STOP"
    assert round(decision.mae_pct, 2) == -12.0
    assert round(decision.mfe_pct, 2) == 16.0


def test_daily_exit_time_stop_only_removes_stagnant_trades() -> None:
    config = DailyExitConfig(time_stop_bars=25, time_stop_min_pnl_pct=3.0)
    active = {"entry": 100.0, "stop": 96.0, "take_profit": 120.0, "bars": 26, "highest_high": 104.0, "lowest_low": 99.0}
    stagnant = pd.Series({"close": 102.0, "high": 103.0, "low": 101.0, "atr14": 6.0, "ema20": 99.0})
    working = pd.Series({"close": 104.0, "high": 105.0, "low": 103.0, "atr14": 6.0, "ema20": 99.0})

    assert daily_timeframe_exit(stagnant, dict(active), config).reason == "TIME_STOP"
    assert daily_timeframe_exit(working, dict(active), config) is None


def test_research_momentum_daily_strategy_uses_production_exit_stack() -> None:
    strategy = STRATEGY_REGISTRY["Research Momentum Volatility"]
    assert strategy.max_hold_bars == 40

    active = {"entry": 100.0, "stop": 96.0, "take_profit": 120.0, "bars": 5, "highest_high": 110.0, "lowest_low": 100.0}
    row = pd.Series({"close": 107.0, "high": 112.0, "low": 106.0, "atr14": 2.0, "ema20": 105.0})

    assert strategy.exit_reason(row, active) == "ATR_TRAIL"
    assert active["exit_price"] == 107.0
    assert active["mfe_pct"] == 12.0


def test_every_registered_strategy_has_timeframe_appropriate_exit_rules() -> None:
    for strategy in STRATEGY_REGISTRY.values():
        if strategy.default_timeframe == "1d":
            assert strategy._uses_daily_exit_stack()
            assert strategy.daily_exit_config.time_stop_bars == 25
        elif strategy.name.startswith("TradingView Mean Reversion ATR"):
            assert not strategy.use_lower_timeframe_sell_stack
            assert strategy.exit_reason(pd.Series({"low": 94.0, "close": 95.0}), {"stop": 96.0, "bars": 1}) == "ATR_TRAILING_STOP"
        else:
            assert strategy.use_lower_timeframe_sell_stack
