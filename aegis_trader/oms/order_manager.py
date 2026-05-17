from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from aegis_trader.core.enums import OrderState
from aegis_trader.core.events import OrderIntent, Signal


@dataclass
class ManagedOrder:
    intent: OrderIntent
    state: OrderState = OrderState.CREATED
    exchange_order_id: str | None = None
    protection_verified: bool = False


@dataclass
class OrderManager:
    orders: dict[str, ManagedOrder] = field(default_factory=dict)

    def create_order(self, signal: Signal, adjusted_notional: float) -> ManagedOrder:
        if signal.stop_price <= 0 or signal.take_profit_price <= signal.entry_price:
            raise ValueError("invalid OCO protection")
        quantity = adjusted_notional / signal.entry_price
        intent = OrderIntent(
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            notional=adjusted_notional,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            client_order_id=f"aegis-{uuid4()}",
        )
        order = ManagedOrder(intent=intent, state=OrderState.VALIDATED, protection_verified=True)
        self.orders[intent.client_order_id] = order
        return order

    def transition(self, client_order_id: str, state: OrderState, exchange_order_id: str | None = None) -> ManagedOrder:
        order = self.orders[client_order_id]
        order.state = state
        if exchange_order_id:
            order.exchange_order_id = exchange_order_id
        return order

    def has_unprotected_position(self) -> bool:
        return any(not order.protection_verified for order in self.orders.values())
