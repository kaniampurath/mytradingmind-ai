from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS, load_feature_file
from aegis_trader.bot.framework import BotDeployment, StrategyAgnosticBot
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY


def run_strategy_matrix(
    strategy_names: list[str],
    data_dir: str | Path = "data/binance",
    days: int = 365,
    interval: str = "1h",
    notional: float = 1_000.0,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for strategy_name in strategy_names:
        strategy = STRATEGY_REGISTRY[strategy_name]
        bot = StrategyAgnosticBot(BotDeployment(name=f"{strategy_name} bot", strategy=strategy, interval=interval, notional=notional))
        for symbol in TOP_TRADING_SYMBOLS:
            path = Path(data_dir) / f"{symbol.replace('/', '')}_{interval}_{days}d_features.parquet"
            if not path.exists():
                continue
            metrics, _ = bot.replay(load_feature_file(path))
            rows.append({"strategy": strategy_name, **asdict(metrics)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def aggregate_strategy_matrix(matrix: pd.DataFrame) -> pd.DataFrame:
    if matrix.empty:
        return pd.DataFrame()
    grouped = matrix.groupby("strategy", as_index=False).agg(
        symbols=("symbol", "count"),
        trades=("trades", "sum"),
        wins=("wins", "sum"),
        losses=("losses", "sum"),
        total_pnl=("total_pnl", "sum"),
        avg_return_pct=("total_return_pct", "mean"),
        max_drawdown_pct=("max_drawdown_pct", "max"),
        avg_trade_return_pct=("avg_trade_return_pct", "mean"),
        sharpe_proxy=("sharpe_proxy", "mean"),
        confidence_score=("confidence_score", "mean"),
    )
    grouped["win_rate"] = grouped.apply(lambda row: 0.0 if row["trades"] <= 0 else row["wins"] / row["trades"] * 100, axis=1)
    grouped["status"] = grouped.apply(_status, axis=1)
    return grouped.sort_values(["status", "total_pnl"], ascending=[True, False])


def _status(row: pd.Series) -> str:
    if row["trades"] < 5:
        return "WATCH"
    if row["total_pnl"] > 0 and row["win_rate"] >= 45 and row["max_drawdown_pct"] < 12:
        return "DEPLOYABLE"
    if row["max_drawdown_pct"] >= 12:
        return "REJECTED"
    return "WATCH"

