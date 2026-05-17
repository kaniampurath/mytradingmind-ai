from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aegis_trader.core.config import settings
from aegis_trader.market_data.binance_history import (
    BinanceHistoricalClient,
    calculate_features,
    default_history_window,
    write_csv,
    write_parquet_if_available,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Binance Spot public candles and calculate replay features.")
    parser.add_argument("--symbols", default=",".join(settings.symbols), help="Comma-separated Binance spot symbols.")
    parser.add_argument("--interval", default=settings.binance_history_interval)
    parser.add_argument("--days", type=int, default=settings.binance_history_days)
    parser.add_argument("--out", default=settings.market_data_dir)
    parser.add_argument("--base-url", default=settings.binance_spot_base_url)
    parser.add_argument("--transport", choices=["python", "powershell"], default="python")
    args = parser.parse_args()

    start, end = default_history_window(args.days)
    client = BinanceHistoricalClient(args.base_url, transport=args.transport)
    output_dir = Path(args.out)

    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        print(f"backfill symbol={symbol} interval={args.interval} start={start.isoformat()} end={end.isoformat()}")
        klines = await client.fetch_klines(symbol=symbol, interval=args.interval, start=start, end=end)
        raw_rows = [kline.as_dict(symbol, args.interval) for kline in klines]
        feature_rows = calculate_features(raw_rows)

        safe_symbol = symbol.replace("/", "")
        raw_csv = output_dir / f"{safe_symbol}_{args.interval}_{args.days}d_raw.csv"
        feature_csv = output_dir / f"{safe_symbol}_{args.interval}_{args.days}d_features.csv"
        feature_parquet = output_dir / f"{safe_symbol}_{args.interval}_{args.days}d_features.parquet"

        write_csv(raw_csv, raw_rows)
        write_csv(feature_csv, feature_rows)
        parquet_written = write_parquet_if_available(feature_parquet, feature_rows)
        print(
            f"saved symbol={symbol} candles={len(raw_rows)} raw={raw_csv} features={feature_csv}"
            + (f" parquet={feature_parquet}" if parquet_written else " parquet=skipped")
        )


if __name__ == "__main__":
    asyncio.run(main())
