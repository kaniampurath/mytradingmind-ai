from __future__ import annotations

from abc import ABC, abstractmethod

from aegis_trader.core.events import Signal
from aegis_trader.features.engine import FeatureSnapshot
from aegis_trader.orderflow.engine import OrderflowSnapshot
from aegis_trader.regime.engine import RegimeSnapshot


class StrategyPlugin(ABC):
    name: str

    @abstractmethod
    def evaluate(
        self,
        features: FeatureSnapshot,
        regime: RegimeSnapshot,
        orderflow: OrderflowSnapshot,
    ) -> Signal | None:
        """Return a signal or abstain. No execution authority lives here."""
