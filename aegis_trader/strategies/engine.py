from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.events import Signal
from aegis_trader.features.engine import FeatureSnapshot
from aegis_trader.orderflow.engine import OrderflowSnapshot
from aegis_trader.regime.engine import RegimeSnapshot
from aegis_trader.strategies.base import StrategyPlugin
from aegis_trader.strategies.plugins import DEFAULT_STRATEGIES


@dataclass
class StrategyEngine:
    plugins: tuple[StrategyPlugin, ...] = DEFAULT_STRATEGIES

    def evaluate(
        self,
        features: FeatureSnapshot,
        regime: RegimeSnapshot,
        orderflow: OrderflowSnapshot,
    ) -> list[Signal]:
        return [
            signal
            for plugin in self.plugins
            if (signal := plugin.evaluate(features, regime, orderflow)) is not None
        ]
