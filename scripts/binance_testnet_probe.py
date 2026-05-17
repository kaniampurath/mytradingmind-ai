from __future__ import annotations

import argparse
import asyncio

from aegis_trader.core.config import settings
from aegis_trader.exchange.binance_testnet_gateway import BinanceSpotTestnetGateway, BinanceTestnetCredentials


async def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Binance Spot Testnet account connectivity without placing orders.")
    parser.add_argument("--api-key", default=settings.binance_api_key)
    parser.add_argument("--api-secret", default=settings.binance_api_secret)
    args = parser.parse_args()
    if not args.api_key or not args.api_secret:
        raise SystemExit("Missing AEGIS_BINANCE_API_KEY / AEGIS_BINANCE_API_SECRET for Spot Testnet.")

    async with BinanceSpotTestnetGateway(BinanceTestnetCredentials(args.api_key, args.api_secret, testnet=True)) as gateway:
        state = await gateway.reconcile()
    print({"open_orders": len(state.get("open_orders", [])), "protected": state.get("protected")})


if __name__ == "__main__":
    asyncio.run(main())
