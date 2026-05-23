from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards.app import (
    deployable_strategy_symbol_rows,
    deployment_readiness,
    strategy_backtest_ranking,
    strategy_symbol_is_certified,
)


def test_bot_framework_exposes_strategy_ranking_panel() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert "def strategy_ranking_panel" in text
    assert "Strategy Ranking" in text
    assert "Refresh strategy backtests" in text
    assert "strategy_ranking_panel()" in text
    assert "active_strategy_names()" in text
    assert "Dormant strategy research shelf" in text
    assert "Deploy Guidance" in text
    assert "strategy_symbol_is_certified" in text
    assert "def market_bucket_swim_lanes" in text
    assert "Top 10 Coin Backtesting From 2024-10-01" in text


def test_deploy_guidance_lists_passed_coins_by_strategy() -> None:
    rows = deployable_strategy_symbol_rows()
    guidance = {row["strategy"]: row["passed coins"] for row in rows}

    assert "BTC/USDT" in guidance["Certified Risk Managed Composite"]
    assert "SOL/USDT" in guidance["Certified Risk Managed Composite"]
    assert "TRX/USDT" in guidance["Certified Risk Managed Composite"]
    assert "ETH/USDT" in guidance["KCJ ATR Trend Burst 5m"]
    assert "XRP/USDT" in guidance["KCJ ATR Trend Burst 5m"]
    assert guidance["TradingView Mean Reversion ATR 1h"] == "TRX/USDT"


def test_strategy_symbol_certification_blocks_failed_pairs() -> None:
    assert strategy_symbol_is_certified("TradingView Mean Reversion ATR 1h", "TRX/USDT")
    assert not strategy_symbol_is_certified("TradingView Mean Reversion ATR 1h", "BTC/USDT")
    assert not strategy_symbol_is_certified("Certified Risk Managed Composite", "ETH/USDT")


def test_deployment_readiness_traffic_light_mapping() -> None:
    class Metrics:
        trades = 10
        total_pnl = 25.0
        max_drawdown_pct = 5.0
        profit_factor = 1.4

    assert deployment_readiness(Metrics()) == "Ready"
    Metrics.profit_factor = 1.05
    assert deployment_readiness(Metrics()) == "Watch"
    Metrics.total_pnl = -1.0
    assert deployment_readiness(Metrics()) == "Avoid"
    Metrics.trades = 0
    assert deployment_readiness(Metrics()) == "Needs Validation"


def test_strategy_backtest_ranking_prefers_stronger_backtest_metrics() -> None:
    aggregate = pd.DataFrame(
        [
            {
                "strategy": "Slow Strategy",
                "status": "WATCH",
                "trades": 4,
                "total_pnl": 10.0,
                "avg_return_pct": 1.0,
                "max_drawdown_pct": 12.0,
                "avg_trade_return_pct": 0.2,
                "sharpe_proxy": 0.4,
                "avg_profit_factor": 0.8,
                "confidence_score": 40.0,
                "win_rate": 35.0,
            },
            {
                "strategy": "Strong Strategy",
                "status": "DEPLOYABLE",
                "trades": 20,
                "total_pnl": 150.0,
                "avg_return_pct": 8.0,
                "max_drawdown_pct": 4.0,
                "avg_trade_return_pct": 0.8,
                "sharpe_proxy": 1.8,
                "avg_profit_factor": 2.2,
                "confidence_score": 72.0,
                "win_rate": 58.0,
            },
        ]
    )

    ranked = strategy_backtest_ranking(aggregate)

    assert ranked.iloc[0]["strategy"] == "Strong Strategy"
    assert ranked.iloc[0]["rank"] == 1
    assert ranked.iloc[0]["rank_score"] > ranked.iloc[1]["rank_score"]
