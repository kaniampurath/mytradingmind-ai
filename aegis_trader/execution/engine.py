from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aegis_trader.core.enums import OrderState
from aegis_trader.exchange.gateway import ExchangeGateway
from aegis_trader.oms.order_manager import ManagedOrder, OrderManager
from aegis_trader.risk.engine import PortfolioRiskEngine


@dataclass
class ExecutionEngine:
    gateway: ExchangeGateway
    oms: OrderManager
    risk: PortfolioRiskEngine
    slippage_threshold_bps: float = 15.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def submit(self, order: ManagedOrder, expected_slippage_bps: float = 0.0) -> ManagedOrder:
        async with self._lock:
            if not order.protection_verified:
                self.risk.trigger_kill_switch("missing protection")
                await self.gateway.emergency_flatten(order.intent.symbol, "missing protection")
                raise RuntimeError("missing protection")
            if expected_slippage_bps > self.slippage_threshold_bps:
                self.risk.trigger_kill_switch("slippage breach")
                await self.gateway.emergency_flatten(order.intent.symbol, "slippage breach")
                raise RuntimeError("slippage breach")
            result = await self.gateway.submit_oco(order.intent)
            state = OrderState.ACKNOWLEDGED if result.accepted else OrderState.REJECTED
            if result.accepted:
                self.risk.record_submitted_trade(order.intent.notional)
            return self.oms.transition(order.intent.client_order_id, state, result.exchange_order_id)
