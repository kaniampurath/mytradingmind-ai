from __future__ import annotations

from aegis_trader.core.enums import Regime
from aegis_trader.core.events import Signal
from aegis_trader.features.engine import FeatureSnapshot
from aegis_trader.orderflow.engine import OrderflowSnapshot
from aegis_trader.regime.engine import RegimeSnapshot
from aegis_trader.strategies.base import StrategyPlugin


class ATRBurstStrategy(StrategyPlugin):
    name = "ATR Burst"

    def evaluate(self, features: FeatureSnapshot, regime: RegimeSnapshot, orderflow: OrderflowSnapshot) -> Signal | None:
        if regime.regime != Regime.TRENDING_UP or features.rvol < 1.4 or orderflow.delta_imbalance <= 0:
            return None
        entry = features.vwap + features.atr
        return Signal(
            strategy=self.name,
            symbol=features.symbol,
            confidence=0.68,
            entry_price=entry,
            stop_price=entry - (1.2 * features.atr),
            take_profit_price=entry + (2.0 * features.atr),
            notional=100.0,
            reason="trend aligned ATR expansion with positive delta",
        )


class VWAPReclaimStrategy(StrategyPlugin):
    name = "VWAP Reclaim"

    def evaluate(self, features: FeatureSnapshot, regime: RegimeSnapshot, orderflow: OrderflowSnapshot) -> Signal | None:
        if regime.regime not in {Regime.TRENDING_UP, Regime.MEAN_REVERT} or features.microstructure_score < 0.4:
            return None
        if orderflow.delta_imbalance <= 0 or features.rvol < 1.0:
            return None
        return Signal(
            strategy=self.name,
            symbol=features.symbol,
            confidence=0.62,
            entry_price=features.vwap,
            stop_price=features.vwap - features.atr,
            take_profit_price=features.vwap + (1.6 * features.atr),
            notional=75.0,
            reason="VWAP reclaim with acceptable microstructure",
        )


class BreakoutContinuationStrategy(ATRBurstStrategy):
    name = "Breakout Continuation"


class ReversalFadeStrategy(VWAPReclaimStrategy):
    name = "Reversal Fade"


class MeanReversionStrategy(VWAPReclaimStrategy):
    name = "Mean Reversion"


DEFAULT_STRATEGIES: tuple[StrategyPlugin, ...] = (
    ATRBurstStrategy(),
    VWAPReclaimStrategy(),
    BreakoutContinuationStrategy(),
    ReversalFadeStrategy(),
    MeanReversionStrategy(),
)
