from __future__ import annotations

from aegis_trader.consensus.devils_advocate import DevilsAdvocateAgent
from aegis_trader.consensus.engine import ConsensusEngine
from aegis_trader.core.enums import CertificationState, Regime, SessionPhase
from aegis_trader.core.events import Signal
from aegis_trader.orderflow.engine import OrderflowSnapshot, OrderflowVerifier
from aegis_trader.regime.engine import RegimeSnapshot
from aegis_trader.testing.certification import CertificationEngine, CertificationMetrics


async def test_consensus_vetoes_bad_orderflow() -> None:
    consensus = ConsensusEngine(OrderflowVerifier(), DevilsAdvocateAgent())
    signal = Signal(
        strategy="test",
        symbol="BTC/USDT",
        confidence=0.8,
        entry_price=100,
        stop_price=95,
        take_profit_price=110,
        notional=100,
        reason="test",
    )
    regime = RegimeSnapshot("BTC/USDT", Regime.TRENDING_UP, SessionPhase.OVERLAP_ACTIVE, 0.8)
    orderflow = OrderflowSnapshot("BTC/USDT", 0, -1, 0, 0, 0, False, 5, 1)

    decision = await consensus.approve(signal, regime, orderflow)

    assert decision.approved is False
    assert decision.reason == "hostile delta imbalance"


def test_certification_rejects_risk_violations() -> None:
    report = CertificationEngine().certify(
        CertificationMetrics(
            sharpe=2,
            profit_factor=2,
            max_drawdown_pct=5,
            replay_determinism=100,
            risk_violations=1,
        )
    )

    assert report.state == CertificationState.REJECTED
