from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from aegis_trader.analytics.replay_metrics import SymbolMetrics, Trade
from aegis_trader.strategies.backtest_plugins import BacktestStrategy


@dataclass(frozen=True)
class BotDeployment:
    name: str
    strategy: BacktestStrategy
    mode: str = "PAPER_MODE"
    interval: str = "1h"
    notional: float = 1_000.0


class StrategyAgnosticBot:
    """Bot host owns lifecycle and reporting; attached strategy owns signal rules only."""

    def __init__(self, deployment: BotDeployment) -> None:
        self.deployment = deployment

    @property
    def strategy_name(self) -> str:
        return self.deployment.strategy.name

    def replay(self, features: pd.DataFrame) -> tuple[SymbolMetrics, list[Trade]]:
        return self.deployment.strategy.replay(features, notional=self.deployment.notional)

