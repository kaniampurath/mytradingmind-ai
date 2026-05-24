from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import json

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS, load_feature_file, run_symbol_replay, write_reports
from aegis_trader.core.config import settings
from aegis_trader.market_data.binance_history import BinanceHistoricalClient, calculate_features, default_history_window, write_csv, write_parquet_if_available
from aegis_trader.storage.db import build_engine, build_session_factory, create_schema
from aegis_trader.storage.scan_repository import write_scan_state


async def scan_once(args: argparse.Namespace) -> None:
    start, end = default_history_window(args.lookback_days)
    base_url = settings.binance_spot_testnet_base_url if args.testnet else args.base_url
    client = BinanceHistoricalClient(base_url, transport=args.transport)
    data_dir = Path(args.data_dir)
    metrics = []
    trades = []
    errors: dict[str, str] = {}
    symbols = [item.strip() for item in (args.symbols or "").split(",") if item.strip()] or list(TOP_TRADING_SYMBOLS)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        try:
            klines = await client.fetch_klines(symbol=symbol, interval=args.interval, start=start, end=end)
            raw_rows = [kline.as_dict(symbol, args.interval) for kline in klines]
            feature_rows = calculate_features(raw_rows)
            if len(feature_rows) < 220:
                errors[symbol] = f"insufficient candles: {len(feature_rows)}"
                continue
            snapshot = await client.fetch_market_snapshot(symbol)
            feature_rows[-1]["orderflow_score"] = snapshot.orderflow_score
            feature_rows[-1]["spread_bps_live"] = snapshot.spread_bps
            feature_rows[-1]["depth_imbalance"] = snapshot.depth_imbalance
            feature_rows[-1]["taker_buy_ratio"] = snapshot.taker_buy_ratio
            safe = symbol.replace("/", "")
            path = data_dir / f"{safe}_{args.interval}_{args.lookback_days}d_features.parquet"
            write_csv(data_dir / f"{safe}_{args.interval}_{args.lookback_days}d_features.csv", feature_rows)
            write_parquet_if_available(path, feature_rows)
            write_csv(data_dir / f"{safe}_{args.interval}_live_features.csv", feature_rows)
            write_parquet_if_available(data_dir / f"{safe}_{args.interval}_live_features.parquet", feature_rows)
            symbol_metrics, symbol_trades = run_symbol_replay(load_feature_file(path))
            metrics.append(symbol_metrics)
            trades.extend(symbol_trades)
        except Exception as exc:
            errors[symbol] = str(exc)
            log_line = {"generated_at": datetime.now(UTC).isoformat(), "symbol": symbol, "error": str(exc)}
            with (out_dir / "scanner_errors.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_line) + "\n")
    write_reports(metrics, trades, out_dir)
    heartbeat = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "binance_spot_testnet" if args.testnet else "binance_public_market_data",
        "base_url": base_url,
        "symbols_ok": len(metrics),
        "symbols_error": len(errors),
        "errors": errors,
        "refresh_seconds": args.sleep,
    }
    (out_dir / "live_scan_heartbeat.json").write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")
    status = "OK" if metrics else "DEGRADED"
    (out_dir / "scanner_status.json").write_text(json.dumps({"status": status, **heartbeat}, indent=2), encoding="utf-8")
    buy_symbols = [item.symbol for item in metrics if item.scan_bucket == "BUY"]
    in_trade_symbols = [item.symbol for item in metrics if item.scan_bucket == "IN TRADE"]
    if args.database:
        await create_schema(args.database_url)
        engine = build_engine(args.database_url)
        factory = build_session_factory(engine)
        async with factory() as session:
            if metrics:
                await write_scan_state(session, metrics, trades, heartbeat, run_id=f"live-{args.interval}-{args.lookback_days}d")
        await engine.dispose()
    print({**heartbeat, "buy": buy_symbols, "in_trade": in_trade_symbols})


async def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Binance top-10 live-scan buckets from recent public candles.")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--symbols", default=",".join(settings.symbols))
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--data-dir", default="data/binance_live")
    parser.add_argument("--out", default="reports")
    parser.add_argument("--base-url", default="https://data-api.binance.vision")
    parser.add_argument("--testnet", action="store_true", help="Use Binance Spot Testnet public market-data endpoint.")
    parser.add_argument("--transport", choices=["python", "powershell"], default="powershell")
    parser.add_argument("--database", action="store_true", default=settings.database_enabled)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--sleep", type=int, default=settings.live_scan_refresh_seconds)
    args = parser.parse_args()

    while True:
        await scan_once(args)
        if not args.loop:
            break
        await asyncio.sleep(args.sleep)


if __name__ == "__main__":
    asyncio.run(main())
