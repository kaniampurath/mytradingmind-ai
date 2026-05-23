from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Callable, Iterable, Protocol

import pandas as pd

from aegis_trader.analytics.replay_metrics import SymbolMetrics


class ReplayableStrategy(Protocol):
    name: str

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Any]]:
        ...


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class OptimizationObjective:
    return_weight: float = 1.0
    sharpe_weight: float = 25.0
    profit_factor_weight: float = 10.0
    drawdown_penalty: float = 2.0
    min_trades: int = 5
    low_trade_penalty: float = 25.0

    def score(self, metrics: SymbolMetrics) -> float:
        profit_factor = min(float(metrics.profit_factor), 10.0)
        score = (
            (float(metrics.total_return_pct) * self.return_weight)
            + (float(metrics.sharpe_proxy) * self.sharpe_weight)
            + (profit_factor * self.profit_factor_weight)
            - (float(metrics.max_drawdown_pct) * self.drawdown_penalty)
        )
        if int(metrics.trades) < self.min_trades:
            score -= self.low_trade_penalty * (self.min_trades - int(metrics.trades))
        return round(score, 6)


@dataclass(frozen=True)
class OptimizationResult:
    rank: int
    parameters: dict[str, Any]
    score: float
    train_score: float
    validation_score: float | None
    metrics: SymbolMetrics


def parameter_grid(specs: Iterable[ParameterSpec]) -> list[dict[str, Any]]:
    items = list(specs)
    if not items:
        return [{}]
    names = [item.name for item in items]
    value_sets = [item.values for item in items]
    return [dict(zip(names, values, strict=True)) for values in product(*value_sets)]


def split_by_time(
    df: pd.DataFrame,
    *,
    validation_fraction: float = 0.25,
    time_column: str = "open_time",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        raise ValueError("cannot split empty feature set")
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    rows = df.copy()
    rows[time_column] = pd.to_datetime(rows[time_column], utc=True, errors="coerce")
    rows = rows.dropna(subset=[time_column]).sort_values(time_column).reset_index(drop=True)
    split_index = max(1, min(len(rows) - 1, int(len(rows) * (1 - validation_fraction))))
    return rows.iloc[:split_index].reset_index(drop=True), rows.iloc[split_index:].reset_index(drop=True)


def optimize_parameters(
    strategy_factory: Callable[[dict[str, Any]], ReplayableStrategy],
    feature_sets: Iterable[pd.DataFrame],
    specs: Iterable[ParameterSpec],
    *,
    objective: OptimizationObjective | None = None,
    notional: float = 1_000.0,
    validation_fraction: float | None = 0.25,
) -> list[OptimizationResult]:
    objective = objective or OptimizationObjective()
    datasets = [df for df in feature_sets if not df.empty]
    if not datasets:
        raise ValueError("at least one non-empty feature set is required")

    results: list[OptimizationResult] = []
    for params in parameter_grid(specs):
        strategy = strategy_factory(params)
        train_metrics: list[SymbolMetrics] = []
        validation_metrics: list[SymbolMetrics] = []
        for df in datasets:
            if validation_fraction is None:
                train_df = df
                validation_df = None
            else:
                train_df, validation_df = split_by_time(df, validation_fraction=validation_fraction)
            metrics, _ = strategy.replay(train_df, notional=notional)
            train_metrics.append(metrics)
            if validation_df is not None and len(validation_df) >= 210:
                validation_result, _ = strategy.replay(validation_df, notional=notional)
                validation_metrics.append(validation_result)

        train_summary = summarize_metrics(train_metrics, symbol=f"{strategy.name} train")
        validation_summary = summarize_metrics(validation_metrics, symbol=f"{strategy.name} validation") if validation_metrics else None
        train_score = objective.score(train_summary)
        validation_score = objective.score(validation_summary) if validation_summary is not None else None
        blended = train_score if validation_score is None else round((train_score * 0.4) + (validation_score * 0.6), 6)
        results.append(
            OptimizationResult(
                rank=0,
                parameters=params,
                score=blended,
                train_score=train_score,
                validation_score=validation_score,
                metrics=validation_summary or train_summary,
            )
        )

    ranked = sorted(results, key=lambda item: item.score, reverse=True)
    return [
        OptimizationResult(
            rank=index + 1,
            parameters=item.parameters,
            score=item.score,
            train_score=item.train_score,
            validation_score=item.validation_score,
            metrics=item.metrics,
        )
        for index, item in enumerate(ranked)
    ]


def summarize_metrics(metrics: Iterable[SymbolMetrics], *, symbol: str = "portfolio") -> SymbolMetrics:
    items = list(metrics)
    if not items:
        raise ValueError("cannot summarize empty metrics")
    trades = sum(item.trades for item in items)
    wins = sum(item.wins for item in items)
    losses = sum(item.losses for item in items)
    total_pnl = sum(item.total_pnl for item in items)
    total_return = sum(item.total_return_pct for item in items)
    gains = sum(max(0.0, item.total_pnl) for item in items)
    gross_losses = abs(sum(min(0.0, item.total_pnl) for item in items))
    return SymbolMetrics(
        symbol=symbol,
        candles=sum(item.candles for item in items),
        trades=trades,
        wins=wins,
        losses=losses,
        win_rate=0.0 if trades == 0 else wins / trades * 100,
        total_pnl=total_pnl,
        total_return_pct=total_return,
        profit_factor=float("inf") if gross_losses == 0 and gains > 0 else 0.0 if gross_losses == 0 else gains / gross_losses,
        max_drawdown_pct=max(item.max_drawdown_pct for item in items),
        avg_trade_return_pct=sum(item.avg_trade_return_pct for item in items) / len(items),
        sharpe_proxy=sum(item.sharpe_proxy for item in items) / len(items),
        last_close=items[-1].last_close,
        scan_bucket="OPTIMIZED",
        scan_reason="parameter optimization aggregate",
        active_entry=None,
        active_pnl=None,
        active_pnl_pct=None,
        watch_score=sum(item.watch_score for item in items) / len(items),
        buy_score=sum(item.buy_score for item in items) / len(items),
        sell_score=sum(item.sell_score for item in items) / len(items),
        orderflow_score=sum(item.orderflow_score for item in items) / len(items),
        confidence_score=sum(item.confidence_score for item in items) / len(items),
        watch_missing="aggregate",
        buy_missing="aggregate",
        sell_missing="aggregate",
        orderflow_reason="aggregate",
        confidence_reason="aggregate",
    )


def prepare_cross_sectional_features(
    df: pd.DataFrame,
    *,
    momentum_window: int = 63,
    top_n: int = 10,
    time_column: str = "open_time",
) -> pd.DataFrame:
    required = {"symbol", time_column, "close", "atr14"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")

    rows = df.copy()
    rows[time_column] = pd.to_datetime(rows[time_column], utc=True, errors="coerce")
    rows = rows.dropna(subset=[time_column]).sort_values(["symbol", time_column]).reset_index(drop=True)
    close = pd.to_numeric(rows["close"], errors="coerce")
    atr = pd.to_numeric(rows["atr14"], errors="coerce")
    rows["momentum63"] = rows.groupby("symbol")["close"].pct_change(momentum_window)
    rows["atr_pct"] = atr / close
    rows["momentum_volatility_ratio"] = rows["momentum63"] / rows["atr_pct"].replace(0, pd.NA)
    rows["momentum_rank"] = rows.groupby(time_column)["momentum63"].rank(method="first", ascending=False)
    rows["top_momentum_eligible"] = rows["momentum_rank"] <= top_n
    return rows

