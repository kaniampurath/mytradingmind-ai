from __future__ import annotations

import pandas as pd

from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY


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
            "close": 104.0,
            "atr14": 2.0,
            "kcj_ratio": 1.01,
            "kcj_ema_slope_fast": 1.0,
            "kcj_ema_slope_slow": 1.0,
            "kcj_in_session": True,
        }
    )

    signal = strategy.entry_signal(row, None)

    assert signal is not None
    assert round(signal.stop_price, 2) == 101.56
    assert "KCJ ATR burst parity" in signal.reason


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
