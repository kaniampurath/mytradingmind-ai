from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from aegis_trader.market_data.binance_history import calculate_features, write_csv, write_parquet_if_available


def main() -> None:
    parser = argparse.ArgumentParser(description="Resample local Binance raw candle CSV files and calculate replay features.")
    parser.add_argument("--source-interval", default="5m")
    parser.add_argument("--target-interval", default="10m")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols such as BTC/USDT,ETH/USDT.")
    parser.add_argument("--data-dir", default="data/binance")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        safe = symbol.replace("/", "")
        raw_path = data_dir / f"{safe}_{args.source_interval}_{args.days}d_raw.csv"
        if not raw_path.exists():
            print(f"skip symbol={symbol} missing={raw_path}")
            continue
        raw = pd.read_csv(raw_path)
        if raw.empty:
            print(f"skip symbol={symbol} empty={raw_path}")
            continue
        rows = resample_raw(raw, symbol=symbol, target_interval=args.target_interval)
        features = calculate_features(rows)
        raw_out = data_dir / f"{safe}_{args.target_interval}_{args.days}d_raw.csv"
        features_csv = data_dir / f"{safe}_{args.target_interval}_{args.days}d_features.csv"
        features_parquet = data_dir / f"{safe}_{args.target_interval}_{args.days}d_features.parquet"
        write_csv(raw_out, rows)
        write_csv(features_csv, features)
        parquet_written = write_parquet_if_available(features_parquet, features)
        print(
            f"saved symbol={symbol} candles={len(rows)} raw={raw_out} features={features_csv}"
            + (f" parquet={features_parquet}" if parquet_written else " parquet=skipped")
        )


def resample_raw(raw: pd.DataFrame, *, symbol: str, target_interval: str) -> list[dict[str, object]]:
    rows = raw.copy()
    rows["open_time"] = pd.to_datetime(rows["open_time"], utc=True, errors="coerce")
    rows["close_time"] = pd.to_datetime(rows["close_time"], utc=True, errors="coerce")
    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ]
    for column in numeric_columns:
        rows[column] = pd.to_numeric(rows[column], errors="coerce")
    rows = rows.dropna(subset=["open_time", "open", "high", "low", "close"]).sort_values("open_time")
    grouped = rows.resample(target_interval, on="open_time", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "close_time": "max",
            "quote_asset_volume": "sum",
            "trade_count": "sum",
            "taker_buy_base_volume": "sum",
            "taker_buy_quote_volume": "sum",
        }
    )
    grouped = grouped.dropna(subset=["open", "high", "low", "close"]).reset_index()
    grouped["symbol"] = symbol
    grouped["interval"] = target_interval
    output = grouped[
        [
            "symbol",
            "interval",
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "trade_count",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ]
    ].copy()
    output["open_time"] = output["open_time"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\+0000)$", "+00:00", regex=True)
    output["close_time"] = output["close_time"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\+0000)$", "+00:00", regex=True)
    return output.to_dict(orient="records")


if __name__ == "__main__":
    main()
