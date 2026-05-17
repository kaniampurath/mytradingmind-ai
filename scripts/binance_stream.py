from __future__ import annotations

import argparse
import asyncio

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS
from aegis_trader.market_data.binance_stream import stream_binance


def parse_symbols(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Stream Binance Spot market data into reports/live_stream.json.")
    parser.add_argument("--symbols", default=",".join(TOP_TRADING_SYMBOLS))
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--out", default="reports/live_stream.json")
    parser.add_argument("--base-url", default="wss://stream.testnet.binance.vision")
    parser.add_argument("--write-seconds", type=float, default=2.0)
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable websocket TLS verification for local dev machines with broken CA trust only.")
    args = parser.parse_args()
    await stream_binance(
        symbols=parse_symbols(args.symbols),
        output=args.out,
        interval=args.interval,
        base_url=args.base_url,
        write_seconds=args.write_seconds,
        insecure_ssl=args.insecure_ssl,
    )


if __name__ == "__main__":
    asyncio.run(main())
