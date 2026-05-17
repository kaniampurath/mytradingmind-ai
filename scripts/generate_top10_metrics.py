from __future__ import annotations

import argparse
from pathlib import Path

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS, load_feature_file, run_symbol_replay, write_reports
from aegis_trader.core.config import settings
from aegis_trader.storage.db import build_engine, build_session_factory, create_schema
from aegis_trader.storage.scan_repository import write_scan_state


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Generate one-year replay metrics and live-scan buckets for top crypto symbols.")
    parser.add_argument("--data-dir", default="data/binance")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--out", default="reports")
    parser.add_argument("--database", action="store_true", default=settings.database_enabled)
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args()

    metrics = []
    trades = []
    for symbol in TOP_TRADING_SYMBOLS:
        safe = symbol.replace("/", "")
        path = Path(args.data_dir) / f"{safe}_{args.interval}_{args.days}d_features.parquet"
        if not path.exists():
            print(f"missing {symbol}: {path}")
            continue
        symbol_metrics, symbol_trades = run_symbol_replay(load_feature_file(path))
        metrics.append(symbol_metrics)
        trades.extend(symbol_trades)
        print(
            f"{symbol_metrics.symbol:9} bucket={symbol_metrics.scan_bucket:9} "
            f"trades={symbol_metrics.trades:4} pnl={symbol_metrics.total_pnl:9.2f} "
            f"win={symbol_metrics.win_rate:5.1f}% pf={symbol_metrics.profit_factor:.2f}"
        )
    write_reports(metrics, trades, Path(args.out))
    if args.database:
        from datetime import UTC, datetime

        await create_schema(args.database_url)
        heartbeat = {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": "one_year_replay",
            "base_url": "local_parquet",
            "symbols_ok": len(metrics),
            "symbols_error": len(TOP_TRADING_SYMBOLS) - len(metrics),
            "errors": {},
            "refresh_seconds": 0,
        }
        engine = build_engine(args.database_url)
        factory = build_session_factory(engine)
        async with factory() as session:
            await write_scan_state(session, metrics, trades, heartbeat, run_id=f"replay-{args.interval}-{args.days}d")
        await engine.dispose()
    print(f"wrote reports to {Path(args.out).resolve()}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(async_main())
