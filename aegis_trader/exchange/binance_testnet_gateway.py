from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis_trader.core.events import OrderIntent
from aegis_trader.exchange.gateway import ExchangeGateway, ExchangeOrderResult


@dataclass
class BinanceTestnetCredentials:
    api_key: str
    api_secret: str
    testnet: bool = True


class BinanceSpotTestnetGateway(ExchangeGateway):
    """ccxt-backed Binance Spot gateway for sandbox execution tests.

    Historical replay data is intentionally not sourced from this gateway.
    Use public Binance market data for replay/back-calculation and this
    gateway for authenticated Spot Testnet order lifecycle checks.
    """

    def __init__(self, credentials: BinanceTestnetCredentials) -> None:
        self.credentials = credentials
        self.exchange: Any | None = None

    async def __aenter__(self) -> "BinanceSpotTestnetGateway":
        self.exchange = self._build_exchange()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.exchange is not None and hasattr(self.exchange, "close"):
            close_result = self.exchange.close()
            if hasattr(close_result, "__await__"):
                await close_result

    async def submit_oco(self, order: OrderIntent) -> ExchangeOrderResult:
        exchange = self._require_exchange()
        if order.side != "buy":
            return ExchangeOrderResult("", order.client_order_id, False, "REJECTED", "spot phase is long-only")
        if order.stop_price <= 0 or order.take_profit_price <= order.entry_price:
            return ExchangeOrderResult("", order.client_order_id, False, "REJECTED", "invalid OCO protection")

        params = {
            "symbol": order.symbol.replace("/", ""),
            "side": "BUY",
            "quantity": exchange.amount_to_precision(order.symbol, order.quantity),
            "price": exchange.price_to_precision(order.symbol, order.take_profit_price),
            "stopPrice": exchange.price_to_precision(order.symbol, order.stop_price),
            "stopLimitPrice": exchange.price_to_precision(order.symbol, order.stop_price * 0.999),
            "stopLimitTimeInForce": "GTC",
            "newClientOrderId": order.client_order_id,
        }
        try:
            result = await exchange.private_post_order_oco(params)
        except Exception as exc:  # ccxt preserves exchange error payloads in the exception.
            return ExchangeOrderResult("", order.client_order_id, False, "REJECTED", str(exc))
        return ExchangeOrderResult(
            exchange_order_id=str(result.get("orderListId", "")),
            client_order_id=order.client_order_id,
            accepted=True,
            status="ACKNOWLEDGED",
        )

    async def emergency_flatten(self, symbol: str, reason: str) -> None:
        exchange = self._require_exchange()
        balances = await exchange.fetch_balance()
        base = symbol.split("/")[0]
        free = float(balances.get(base, {}).get("free", 0.0) or 0.0)
        if free > 0:
            await exchange.create_market_sell_order(symbol, exchange.amount_to_precision(symbol, free), {"newClientOrderId": f"aegis-flatten-{reason[:16]}"})

    async def reconcile(self) -> dict[str, object]:
        exchange = self._require_exchange()
        balances = await exchange.fetch_balance()
        open_orders = await exchange.fetch_open_orders()
        return {"balances": balances, "open_orders": open_orders, "protected": True}

    def _build_exchange(self) -> Any:
        try:
            import ccxt.async_support as ccxt
        except ImportError as exc:
            raise RuntimeError("ccxt is required for Binance testnet execution. Run: pip install -e .") from exc

        exchange = ccxt.binance(
            {
                "apiKey": self.credentials.api_key,
                "secret": self.credentials.api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        exchange.set_sandbox_mode(self.credentials.testnet)
        return exchange

    def _require_exchange(self) -> Any:
        if self.exchange is None:
            self.exchange = self._build_exchange()
        return self.exchange
