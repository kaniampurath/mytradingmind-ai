from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.events import DAVerdict, Signal
from aegis_trader.orderflow.engine import OrderflowSnapshot
from aegis_trader.regime.engine import RegimeSnapshot


@dataclass
class DevilsAdvocateAgent:
    """Rules fallback for GPT-based DA review."""

    async def evaluate(self, signal: Signal, regime: RegimeSnapshot, orderflow: OrderflowSnapshot) -> DAVerdict:
        concerns: list[str] = []
        score = 0.0
        if orderflow.spread_bps > 12:
            score += 0.15
            concerns.append("elevated spread")
        if orderflow.liquidity_score < 0.5:
            score += 0.18
            concerns.append("thin liquidity")
        if regime.confidence < 0.6:
            score += 0.12
            concerns.append("weak regime confidence")
        if signal.confidence < 0.6:
            score += 0.15
            concerns.append("weak strategy confidence")

        veto = score > 0.35
        size_multiplier = 0.0 if veto else 0.5 if score > 0.20 else 1.0
        return DAVerdict(concern_score=score, veto=veto, size_multiplier=size_multiplier, reasons=concerns)
