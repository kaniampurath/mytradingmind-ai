from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from aegis_trader.core.events import OrderIntent


@dataclass(frozen=True)
class ExchangeOrderResult:
    exchange_order_id: str
    client_order_id: str
    accepted: bool
    status: str
    reason: str = ""


class ExchangeGateway(ABC):
    @abstractmethod
    async def submit_oco(self, order: OrderIntent) -> ExchangeOrderResult:
        """Submit an entry plus protective OCO bracket."""

    @abstractmethod
    async def emergency_flatten(self, symbol: str, reason: str) -> None:
        """Flatten or protect exposure immediately."""

    @abstractmethod
    async def reconcile(self) -> dict[str, object]:
        """Return balances, orders, positions, and protection state."""


class PaperExchangeGateway(ExchangeGateway):
    def __init__(self) -> None:
        self.orders: dict[str, OrderIntent] = {}
        self.flatten_reasons: list[str] = []

    async def submit_oco(self, order: OrderIntent) -> ExchangeOrderResult:
        self.orders[order.client_order_id] = order
        return ExchangeOrderResult(
            exchange_order_id=f"paper-{order.client_order_id}",
            client_order_id=order.client_order_id,
            accepted=True,
            status="ACKNOWLEDGED",
        )

    async def emergency_flatten(self, symbol: str, reason: str) -> None:
        self.flatten_reasons.append(f"{symbol}:{reason}")

    async def reconcile(self) -> dict[str, object]:
        return {"orders": list(self.orders), "protected": True, "balances": {}}
