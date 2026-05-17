from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.consensus.devils_advocate import DevilsAdvocateAgent
from aegis_trader.core.events import DAVerdict, Signal
from aegis_trader.orderflow.engine import OrderflowSnapshot, OrderflowVerifier
from aegis_trader.regime.engine import RegimeSnapshot


@dataclass(frozen=True)
class ConsensusDecision:
    approved: bool
    reason: str
    signal: Signal
    da_verdict: DAVerdict | None = None


@dataclass
class ConsensusEngine:
    orderflow_verifier: OrderflowVerifier
    da_agent: DevilsAdvocateAgent

    async def approve(
        self,
        signal: Signal,
        regime: RegimeSnapshot,
        orderflow: OrderflowSnapshot,
        news_clear: bool = True,
    ) -> ConsensusDecision:
        if signal.side != "buy":
            return ConsensusDecision(False, "spot phase is long-only", signal)
        if signal.stop_price >= signal.entry_price:
            return ConsensusDecision(False, "invalid protective stop", signal)
        verified, reason = self.orderflow_verifier.verify(signal, orderflow)
        if not verified:
            return ConsensusDecision(False, reason, signal)
        if not news_clear:
            return ConsensusDecision(False, "news filter blocked trade", signal)
        da_verdict = await self.da_agent.evaluate(signal, regime, orderflow)
        if da_verdict.veto:
            return ConsensusDecision(False, "devil's advocate veto", signal, da_verdict)
        adjusted = signal.model_copy(update={"notional": signal.notional * da_verdict.size_multiplier})
        return ConsensusDecision(True, "approved", adjusted, da_verdict)
