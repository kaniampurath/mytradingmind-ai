from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.enums import Regime, SessionPhase
from aegis_trader.features.engine import FeatureSnapshot


@dataclass(frozen=True)
class RegimeSnapshot:
    symbol: str
    regime: Regime
    session_phase: SessionPhase
    confidence: float


class RegimeEngine:
    def classify(self, features: FeatureSnapshot, session_phase: SessionPhase = SessionPhase.DEAD_ZONE) -> RegimeSnapshot:
        if features.spread_bps > 25 or features.volatility > 0.05:
            regime = Regime.PANIC
            confidence = 0.8
        elif features.ema20 > features.ema50 > features.ema200:
            regime = Regime.TRENDING_UP
            confidence = 0.75
        elif features.ema20 < features.ema50 < features.ema200:
            regime = Regime.TRENDING_DOWN
            confidence = 0.75
        elif features.volatility < 0.004:
            regime = Regime.COMPRESSION
            confidence = 0.65
        else:
            regime = Regime.CHOPPY
            confidence = 0.55
        return RegimeSnapshot(features.symbol, regime, session_phase, confidence)
