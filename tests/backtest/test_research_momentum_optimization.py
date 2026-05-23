from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from aegis_trader.strategies.backtest_plugins import BacktestSignal, BacktestStrategy
from aegis_trader.analytics.replay_metrics import SymbolMetrics
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY, active_strategy_names, dormant_strategy_names
from aegis_trader.strategies.optimization import (
    OptimizationObjective,
    ParameterSpec,
    optimize_parameters,
    prepare_cross_sectional_features,
)


def test_research_momentum_volatility_strategy_is_registered() -> None:
    strategy = STRATEGY_REGISTRY["Research Momentum Volatility"]

    assert strategy.default_timeframe == "1d"
    assert "momentum" in strategy.description


def test_only_certified_strategies_are_active_for_bot_creation() -> None:
    assert active_strategy_names() == ["KCJ ATR Trend Burst 5m", "TradingView Mean Reversion ATR 1h", "Certified Risk Managed Composite"]
    assert "Academic Time-Series Momentum" in dormant_strategy_names()
    assert "Academic Short-Term Reversal" in dormant_strategy_names()


def test_research_momentum_volatility_entry_uses_paper_gates() -> None:
    strategy = STRATEGY_REGISTRY["Research Momentum Volatility"]
    previous = pd.Series({"close": 100.0})
    row = pd.Series(
        {
            "close": 110.0,
            "low": 109.0,
            "high": 111.0,
            "atr14": 2.0,
            "ema50": 105.0,
            "ema200": 95.0,
            "momentum63": 0.12,
            "atr_pct": 0.018,
            "momentum_volatility_ratio": 6.67,
            "top_momentum_eligible": True,
            "sentiment_label": "neutral",
        }
    )

    signal = strategy.entry_signal(row, previous)

    assert signal is not None
    assert signal.stop_price == 106.0
    assert signal.take_profit_price == 118.0
    assert "ATR risk control" in signal.reason


def test_research_momentum_volatility_blocks_negative_sentiment() -> None:
    strategy = STRATEGY_REGISTRY["Research Momentum Volatility"]
    row = pd.Series(
        {
            "close": 110.0,
            "atr14": 2.0,
            "ema50": 105.0,
            "ema200": 95.0,
            "momentum63": 0.12,
            "atr_pct": 0.018,
            "momentum_volatility_ratio": 6.67,
            "top_momentum_eligible": True,
            "sentiment_label": "negative",
        }
    )

    assert strategy.entry_signal(row, pd.Series({"close": 100.0})) is None


def test_academic_time_series_momentum_strategy_is_registered() -> None:
    strategy = STRATEGY_REGISTRY["Academic Time-Series Momentum"]

    assert strategy.default_timeframe == "1d"
    assert "time-series momentum" in strategy.description


def test_academic_time_series_momentum_uses_positive_lookback_and_volatility() -> None:
    strategy = STRATEGY_REGISTRY["Academic Time-Series Momentum"]
    row = pd.Series(
        {
            "close": 120.0,
            "atr14": 3.0,
            "ema50": 112.0,
            "ema200": 100.0,
            "tsmom252": 0.24,
            "atr_pct": 0.025,
            "tsmom_vol_score": 9.6,
            "top_momentum_eligible": True,
        }
    )

    signal = strategy.entry_signal(row, pd.Series({"close": 118.0}))

    assert signal is not None
    assert signal.stop_price == 114.0
    assert signal.take_profit_price == 132.0
    assert "time-series momentum" in signal.reason


def test_academic_short_term_reversal_strategy_is_registered() -> None:
    strategy = STRATEGY_REGISTRY["Academic Short-Term Reversal"]

    assert strategy.default_timeframe == "1d"
    assert "reversal" in strategy.description


def test_academic_short_term_reversal_requires_reclaim_after_prior_loss() -> None:
    strategy = STRATEGY_REGISTRY["Academic Short-Term Reversal"]
    row = pd.Series(
        {
            "close": 101.0,
            "atr14": 2.0,
            "ema20": 100.5,
            "ema200": 90.0,
            "reversal21": -0.12,
            "atr_pct": 0.0198,
            "rvol30": 1.1,
            "delta_ratio": -0.02,
        }
    )

    signal = strategy.entry_signal(row, pd.Series({"close": 99.0}))

    assert signal is not None
    assert signal.stop_price == 97.0
    assert signal.take_profit_price == 107.4
    assert "short-term reversal" in signal.reason


def test_tradingview_mean_reversion_atr_strategy_is_registered_dormant() -> None:
    strategy = STRATEGY_REGISTRY["TradingView Mean Reversion ATR"]
    ten_min = STRATEGY_REGISTRY["TradingView Mean Reversion ATR 10m"]
    one_hour = STRATEGY_REGISTRY["TradingView Mean Reversion ATR 1h"]

    assert strategy.default_timeframe == "1h"
    assert "PineScript-derived" in strategy.description
    assert "TradingView Mean Reversion ATR" in dormant_strategy_names()
    assert ten_min.default_timeframe == "10m"
    assert one_hour.default_timeframe == "1h"
    assert "TradingView Mean Reversion ATR 10m" in dormant_strategy_names()
    assert "TradingView Mean Reversion ATR 1h" in active_strategy_names()


def test_tradingview_mean_reversion_atr_entry_uses_delayed_reject_and_drift_confirmation() -> None:
    strategy = STRATEGY_REGISTRY["TradingView Mean Reversion ATR"]
    row = pd.Series(
        {
            "close": 100.0,
            "atr14": 2.0,
            "kl_entry_signal": True,
            "kl_risk_amt": 4.0,
        }
    )

    signal = strategy.entry_signal(row, pd.Series({"close": 99.0}))

    assert signal is not None
    assert signal.stop_price == 96.0
    assert signal.take_profit_price == 112.0
    assert "ATR diversion" in signal.reason


def test_tradingview_mean_reversion_atr_prepare_rows_keeps_trigger_until_confirmation() -> None:
    strategy = STRATEGY_REGISTRY["TradingView Mean Reversion ATR"]
    rows = _base_feature_rows(80)
    rows.loc[:, "open"] = 100.0
    rows.loc[:, "high"] = 101.0
    rows.loc[:, "low"] = 99.0
    rows.loc[:, "close"] = 100.0
    rows.loc[:, "kl_atr_tsl"] = 2.0
    rows.loc[:, "kl_risk_amt"] = 2.0
    rows.loc[:, "kl_test_stat"] = 0.0
    rows.loc[:, "kl_drift"] = 0.0
    rows.loc[:, "kl_reject_h0"] = False
    rows.loc[:, "kl_drift_confirmation"] = False
    rows.loc[:, "kl_entry_signal"] = False
    rows.loc[60, "kl_reject_h0"] = True
    rows.loc[63, "kl_drift_confirmation"] = True
    rows = rows.drop(columns=["kl_entry_signal"])

    prepared = strategy._prepare_rows(rows)

    assert bool(prepared.loc[63, "kl_entry_signal"])


def test_tradingview_mean_reversion_atr_1h_is_scoped_to_validated_trx_universe() -> None:
    strategy = STRATEGY_REGISTRY["TradingView Mean Reversion ATR 1h"]
    row = pd.Series({"symbol": "BTC/USDT", "close": 100.0, "atr14": 2.0, "kl_entry_signal": True, "kl_risk_amt": 4.0})

    assert strategy.entry_signal(row, pd.Series({"close": 99.0})) is None


def test_tradingview_mean_reversion_atr_replay_models_three_stage_profit_taking() -> None:
    strategy = STRATEGY_REGISTRY["TradingView Mean Reversion ATR"]
    rows = _base_feature_rows(90)
    rows.loc[:, "close"] = 100.0
    rows.loc[:, "open"] = 100.0
    rows.loc[:, "high"] = 101.0
    rows.loc[:, "low"] = 99.0
    rows.loc[60, ["kl_entry_signal", "kl_risk_amt", "kl_atr_tsl"]] = [True, 3.0, 3.0]
    rows.loc[61, ["close", "high", "low", "kl_atr_tsl"]] = [103.5, 104.0, 101.0, 3.0]
    rows.loc[62, ["close", "high", "low", "kl_atr_tsl"]] = [106.5, 107.0, 104.0, 3.0]
    rows.loc[63, ["close", "high", "low", "kl_atr_tsl"]] = [109.5, 110.0, 107.0, 3.0]

    metrics, trades = strategy.replay(rows)

    assert metrics.trades == 1
    assert trades[0].exit_reason == "TP_LVL3"
    assert trades[0].pnl > 0


def test_prepare_cross_sectional_features_adds_reusable_momentum_rank() -> None:
    rows = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
            "open_time": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"] * 2, utc=True),
            "close": [100.0, 105.0, 110.0, 100.0, 120.0, 140.0],
            "atr14": [2.0, 2.0, 2.0, 2.0, 2.0, 2.0],
        }
    )

    features = prepare_cross_sectional_features(rows, momentum_window=1, top_n=1)
    latest = features[features["open_time"] == pd.Timestamp("2026-01-03", tz="UTC")]

    leader = latest.sort_values("momentum_rank").iloc[0]
    laggard = latest.sort_values("momentum_rank").iloc[1]
    assert leader["symbol"] == "BBB"
    assert bool(leader["top_momentum_eligible"])
    assert not bool(laggard["top_momentum_eligible"])


def test_optimize_parameters_ranks_by_validation_weighted_objective() -> None:
    base = _metrics(total_return_pct=0.0)

    class FixedStrategy:
        name = "fixed"

        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Any]]:
            return replace(base, total_return_pct=float(self.params["edge"]), total_pnl=float(self.params["edge"])), []

    rows = pd.DataFrame(
        {
            "symbol": "BTC/USDT",
            "open_time": pd.date_range("2026-01-01", periods=240, freq="1h", tz="UTC"),
            "close": 100.0,
        }
    )

    results = optimize_parameters(
        FixedStrategy,
        [rows],
        [ParameterSpec("edge", (1.0, 5.0))],
        objective=OptimizationObjective(sharpe_weight=0.0, profit_factor_weight=0.0, drawdown_penalty=0.0, min_trades=0),
    )

    assert results[0].parameters == {"edge": 5.0}
    assert results[0].rank == 1


def test_backtest_strategy_drawdown_lock_stops_new_entries() -> None:
    class AlwaysLosesStrategy(BacktestStrategy):
        name = "Always Loses"
        description = "Test strategy for drawdown lock."
        runtime_drawdown_lock_pct = 3.0

        def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
            close = float(row["close"])
            return BacktestSignal(True, close - 1.0, close + 10.0, "test entry")

    rows = pd.DataFrame(
        {
            "symbol": "BTC/USDT",
            "open_time": pd.date_range("2026-01-01", periods=260, freq="1h", tz="UTC"),
            "open": 100.0,
            "high": 100.2,
            "low": 98.5,
            "close": 100.0,
            "volume": 1000.0,
            "atr14": 1.0,
            "ema20": 99.0,
            "ema50": 98.0,
            "ema200": 97.0,
            "vwap": 99.0,
            "rvol30": 1.0,
            "volatility": 0.01,
            "delta_ratio": 0.1,
        }
    )

    metrics, trades = AlwaysLosesStrategy().replay(rows)

    assert metrics.max_drawdown_pct >= 3.0
    assert len(trades) < 10


def _metrics(*, total_return_pct: float) -> SymbolMetrics:
    return SymbolMetrics(
        symbol="BTC/USDT",
        candles=240,
        trades=8,
        wins=5,
        losses=3,
        win_rate=62.5,
        total_pnl=total_return_pct,
        total_return_pct=total_return_pct,
        profit_factor=1.5,
        max_drawdown_pct=4.0,
        avg_trade_return_pct=0.5,
        sharpe_proxy=1.0,
        last_close=100.0,
        scan_bucket="NO SIGNAL",
        scan_reason="test",
        active_entry=None,
        active_pnl=None,
        active_pnl_pct=None,
        watch_score=0.0,
        buy_score=0.0,
        sell_score=0.0,
        orderflow_score=0.0,
        confidence_score=0.0,
        watch_missing="test",
        buy_missing="test",
        sell_missing="test",
        orderflow_reason="test",
        confidence_reason="test",
    )


def _base_feature_rows(periods: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": "BTC/USDT",
            "open_time": pd.date_range("2026-01-01", periods=periods, freq="1h", tz="UTC"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
            "atr14": 2.0,
            "ema20": 99.0,
            "ema50": 98.0,
            "ema200": 97.0,
            "vwap": 99.0,
            "rvol30": 1.0,
            "volatility": 0.02,
            "delta_ratio": 0.1,
            "kl_entry_signal": False,
            "kl_risk_amt": 3.0,
            "kl_atr_tsl": 3.0,
            "kl_test_stat": 2.1,
            "kl_drift": 0.001,
        }
    )
