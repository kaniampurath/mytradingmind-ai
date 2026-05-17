from __future__ import annotations

import pytest

from aegis_trader.core.events import OrderIntent
from aegis_trader.exchange.gateway import PaperExchangeGateway
from aegis_trader.execution.engine import ExecutionEngine
from aegis_trader.oms.order_manager import ManagedOrder, OrderManager
from aegis_trader.risk.engine import PortfolioRiskEngine, RiskLimits


async def test_execution_fails_closed_when_protection_missing() -> None:
    risk = PortfolioRiskEngine(RiskLimits())
    gateway = PaperExchangeGateway()
    oms = OrderManager()
    execution = ExecutionEngine(gateway, oms, risk)
    order = ManagedOrder(
        intent=OrderIntent(
            symbol="BTC/USDT",
            side="buy",
            quantity=1,
            notional=100,
            entry_price=100,
            stop_price=90,
            take_profit_price=120,
            client_order_id="x",
        ),
        protection_verified=False,
    )

    with pytest.raises(RuntimeError, match="missing protection"):
        await execution.submit(order)

    assert risk.kill_switch_active is True
    assert gateway.flatten_reasons == ["BTC/USDT:missing protection"]


async def test_execution_kill_switches_on_slippage_breach() -> None:
    risk = PortfolioRiskEngine(RiskLimits())
    gateway = PaperExchangeGateway()
    oms = OrderManager()
    execution = ExecutionEngine(gateway, oms, risk, slippage_threshold_bps=5)
    order = ManagedOrder(
        intent=OrderIntent(
            symbol="BTC/USDT",
            side="buy",
            quantity=1,
            notional=100,
            entry_price=100,
            stop_price=90,
            take_profit_price=120,
            client_order_id="x",
        ),
        protection_verified=True,
    )

    with pytest.raises(RuntimeError, match="slippage breach"):
        await execution.submit(order, expected_slippage_bps=6)

    assert risk.kill_switch_active is True
