from __future__ import annotations

from pathlib import Path

from aegis_trader.analytics.replay_metrics import load_feature_file
import pandas as pd

from aegis_trader.analytics.strategy_reports import aggregate_strategy_matrix
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY


def test_certified_risk_managed_composite_is_available() -> None:
    strategy = STRATEGY_REGISTRY["Certified Risk Managed Composite"]

    assert strategy.name == "Certified Risk Managed Composite"
    assert "drawdown lock" in strategy.description


def test_certified_risk_managed_composite_replays_certified_symbol() -> None:
    strategy = STRATEGY_REGISTRY["Certified Risk Managed Composite"]
    features = load_feature_file(Path("data/binance/BTCUSDT_1h_365d_features.parquet"))

    metrics, trades = strategy.replay(features)

    assert metrics.symbol == "BTC/USDT"
    assert metrics.trades == len(trades)
    assert metrics.max_drawdown_pct < 12
    assert metrics.profit_factor >= 1.0


def test_certified_risk_managed_composite_reports_deployable_status() -> None:
    matrix = pd.DataFrame(
        [
            {
                "strategy": "Certified Risk Managed Composite",
                "symbol": "ETH/USDT",
                "trades": 100,
                "wins": 41,
                "losses": 59,
                "total_pnl": 100.0,
                "total_return_pct": 10.0,
                "max_drawdown_pct": 7.0,
                "avg_trade_return_pct": 0.1,
                "sharpe_proxy": 0.8,
                "confidence_score": 40.0,
            }
        ]
    )
    aggregate = aggregate_strategy_matrix(matrix)

    assert aggregate.iloc[0]["status"] == "DEPLOYABLE"
