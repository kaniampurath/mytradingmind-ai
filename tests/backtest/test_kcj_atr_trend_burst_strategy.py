from __future__ import annotations

import pandas as pd

from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY
from aegis_trader.strategies.exits import (
    five_min_atr_burst_params,
    five_min_emergency_exit_decision,
    five_min_final_exit_decision,
    five_min_partial_tp_decision,
    initial_five_min_stop,
)


def test_kcj_atr_trend_burst_5m_is_registered() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]

    assert strategy.name == "KCJ ATR Trend Burst 5m"
    assert "5-minute" in strategy.description
    assert strategy.default_timeframe == "5m"


def test_kcj_atr_trend_burst_entry_signal_matches_core_pine_gates() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]
    row = pd.Series(
        {
            "symbol": "BTC/USDT",
            "open": 100.0,
            "high": 104.2,
            "low": 99.8,
            "close": 104.0,
            "atr14": 2.0,
            "vwap": 101.0,
            "rvol30": 1.2,
            "delta_ratio": 0.08,
            "volatility": 0.006,
            "kcj_ratio": 1.01,
            "kcj_ema_fast": 102.0,
            "kcj_ema_slope_fast": 1.0,
            "kcj_ema_slope_slow": 1.0,
            "kcj_in_session": True,
        }
    )

    signal = strategy.entry_signal(row, None)

    assert signal is not None
    assert round(signal.stop_price, 2) == 101.0
    assert "KCJ ATR burst parity" in signal.reason


def test_kcj_atr_trend_burst_rejects_weak_5m_orderflow() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]
    row = pd.Series(
        {
            "symbol": "BTC/USDT",
            "open": 100.0,
            "high": 104.2,
            "low": 99.8,
            "close": 104.0,
            "atr14": 2.0,
            "vwap": 101.0,
            "rvol30": 0.8,
            "delta_ratio": -0.02,
            "volatility": 0.006,
            "kcj_ratio": 1.01,
            "kcj_ema_fast": 102.0,
            "kcj_ema_slope_fast": 1.0,
            "kcj_ema_slope_slow": 1.0,
            "kcj_in_session": True,
        }
    )

    assert strategy.entry_signal(row, None) is None


def test_kcj_atr_trend_burst_rejects_immediate_post_crash_bounce() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]
    previous = pd.Series({"open": 104.0, "close": 100.0})
    row = pd.Series(
        {
            "symbol": "BTC/USDT",
            "open": 100.0,
            "high": 104.2,
            "low": 99.8,
            "close": 104.0,
            "atr14": 2.0,
            "vwap": 101.0,
            "rvol30": 1.2,
            "delta_ratio": 0.08,
            "volatility": 0.006,
            "kcj_ratio": 1.01,
            "kcj_ema_fast": 102.0,
            "kcj_ema_slope_fast": 1.0,
            "kcj_ema_slope_slow": 1.0,
            "kcj_in_session": True,
        }
    )

    assert strategy.entry_signal(row, previous) is None


def test_kcj_atr_trend_burst_rejects_uncertified_symbol_universe() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]
    row = pd.Series(
        {
            "symbol": "SOL/USDT",
            "open": 100.0,
            "high": 104.2,
            "low": 99.8,
            "close": 104.0,
            "atr14": 2.0,
            "vwap": 101.0,
            "rvol30": 1.2,
            "delta_ratio": 0.08,
            "volatility": 0.006,
            "kcj_ratio": 1.01,
            "kcj_ema_fast": 102.0,
            "kcj_ema_slope_fast": 1.0,
            "kcj_ema_slope_slow": 1.0,
            "kcj_in_session": True,
        }
    )

    assert strategy.entry_signal(row, None) is None


def test_kcj_atr_trend_burst_replay_accepts_5m_feature_rows() -> None:
    strategy = STRATEGY_REGISTRY["KCJ ATR Trend Burst 5m"]
    opens = [100 + (idx * 0.01) for idx in range(260)]
    closes = [value + 0.03 for value in opens]
    rows = pd.DataFrame(
        {
            "symbol": "BTC/USDT",
            "open_time": pd.date_range("2026-01-01 05:30", periods=260, freq="5min", tz="UTC"),
            "open": opens,
            "high": [value + 0.15 for value in closes],
            "low": [value - 0.15 for value in opens],
            "close": closes,
            "volume": 1000.0,
            "atr14": 0.25,
            "ema20": closes,
            "ema50": closes,
            "ema200": closes,
            "vwap": closes,
            "rvol30": 1.0,
            "volatility": 0.0025,
            "delta_ratio": 0.05,
        }
    )

    metrics, trades = strategy.replay(rows)

    assert metrics.symbol == "BTC/USDT"
    assert metrics.candles > 200
    assert metrics.trades == len(trades)


def test_five_min_sell_rules_symbol_specific_parameters() -> None:
    assert five_min_atr_burst_params("BTCUSDT").atr_mult == 1.5
    assert five_min_atr_burst_params("SOLUSDT").ema_fast_len == 10
    assert five_min_atr_burst_params("ETHUSDT").atr_mult == 2.0
    assert five_min_atr_burst_params("ETHUSDT").ema_stop_len == 21
    assert five_min_atr_burst_params("DOGEUSDT").tp_rr == 0.0
    assert initial_five_min_stop(100.0, 2.0, 1.5) == 97.0


def test_five_min_partial_tp_moves_remaining_stop_to_breakeven() -> None:
    row = pd.Series({"high": 104.0})
    active = {"entry": 100.0, "stop": 97.0, "remaining": 1.0, "notional": 1_000.0, "partial_taken": False}

    decision = five_min_partial_tp_decision(row, active, tp_rr=1.0)

    assert decision is not None
    assert decision.signal_type == "PARTIAL_TP"
    assert decision.partial_qty == 0.5
    assert decision.stop_price == 100.0
    assert decision.replace_protection


def test_five_min_final_exit_prioritizes_atr_stop_over_ma_exit() -> None:
    row = pd.Series({"low": 96.5, "close": 95.0, "kcj_ema_fast": 99.0})
    active = {"entry": 100.0, "stop": 97.0}

    decision = five_min_final_exit_decision(row, active)

    assert decision is not None
    assert decision.reason == "ATR_STOP"
    assert decision.exit_price == 97.0
    assert decision.cancel_protection


def test_five_min_emergency_exit_bypasses_daily_pnl_limits_and_shutdowns() -> None:
    row = pd.Series({"is_complete": False, "open": 100.0, "close": 96.0, "low": 95.5})
    active = {"entry": 101.0, "stop": 98.0}

    decision = five_min_emergency_exit_decision(row, active, previous_completed_atr=2.0, atr_mult=1.5)

    assert decision is not None
    assert decision.signal_type == "EMERGENCY_EXIT"
    assert decision.force_shutdown
    assert decision.bypass_daily_pnl_limits
    assert decision.cancel_protection
