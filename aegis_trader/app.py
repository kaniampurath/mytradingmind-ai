from __future__ import annotations

import asyncio

from aegis_trader.consensus.devils_advocate import DevilsAdvocateAgent
from aegis_trader.consensus.engine import ConsensusEngine
from aegis_trader.core.config import settings
from aegis_trader.core.logging import configure_logging
from aegis_trader.core.enums import Regime, SessionPhase
from aegis_trader.exchange.gateway import PaperExchangeGateway
from aegis_trader.execution.engine import ExecutionEngine
from aegis_trader.features.engine import FeatureSnapshot
from aegis_trader.orderflow.engine import OrderflowSnapshot, OrderflowVerifier
from aegis_trader.oms.order_manager import OrderManager
from aegis_trader.regime.engine import RegimeSnapshot
from aegis_trader.risk.engine import PortfolioRiskEngine, RiskLimits
from aegis_trader.strategies.engine import StrategyEngine


async def run_once() -> None:
    configure_logging()
    risk = PortfolioRiskEngine(
        RiskLimits(
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_position_notional=settings.max_position_notional,
            max_portfolio_exposure=settings.max_portfolio_exposure,
            max_trades_per_day=settings.max_trades_per_day,
            consecutive_loss_lock=settings.consecutive_loss_lock,
        )
    )
    oms = OrderManager()
    execution = ExecutionEngine(PaperExchangeGateway(), oms, risk, settings.slippage_threshold_bps)
    consensus = ConsensusEngine(OrderflowVerifier(), DevilsAdvocateAgent())
    strategies = StrategyEngine()

    symbol = settings.symbols[0] if settings.symbols else "ETH/USDT"
    features = FeatureSnapshot(symbol, 10, 105, 102, 100, 106, 1.7, 0.01, 5, 1, 0.7)
    regime = RegimeSnapshot(symbol, regime=Regime.TRENDING_UP, session_phase=SessionPhase.OVERLAP_ACTIVE, confidence=0.8)
    orderflow = OrderflowSnapshot(symbol, 2, 1, 0, 0, 0.5, False, 5, 0.95)
    for signal in strategies.evaluate(features, regime, orderflow):
        decision = await consensus.approve(signal, regime, orderflow)
        if decision.approved:
            risk_decision = risk.approve(decision.signal)
            if risk_decision.approved:
                await execution.submit(oms.create_order(decision.signal, risk_decision.adjusted_notional))


def main() -> None:
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
