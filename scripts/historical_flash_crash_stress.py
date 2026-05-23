from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Iterable

import pandas as pd

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS, Trade, load_feature_file
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY, BacktestStrategy, active_strategy_names


@dataclass(frozen=True)
class CrashEvent:
    name: str
    start: str
    end: str
    source: str
    note: str


CRASH_EVENTS: tuple[CrashEvent, ...] = (
    CrashEvent(
        name="2024-08-05 global risk selloff",
        start="2024-08-05T00:00:00+00:00",
        end="2024-08-06T00:00:00+00:00",
        source="CNBC / Cointelegraph reported broad crypto selloff and liquidations around Aug 5, 2024.",
        note="Requires older local backfill than the current 365-day files.",
    ),
    CrashEvent(
        name="2025-04-07 tariff/liquidation shock",
        start="2025-04-07T00:00:00+00:00",
        end="2025-04-08T00:00:00+00:00",
        source="Crypto Times reported about $1.38B crypto liquidations on Apr 7, 2025.",
        note="Requires older local backfill than the current 365-day files.",
    ),
    CrashEvent(
        name="2025-05-30 options-expiry liquidation",
        start="2025-05-30T00:00:00+00:00",
        end="2025-05-31T00:00:00+00:00",
        source="Crypto Times reported more than $750M liquidations on May 30, 2025.",
        note="Covered by current local one-hour Binance feature files.",
    ),
    CrashEvent(
        name="2025-10-10 liquidation cascade",
        start="2025-10-10T12:00:00+00:00",
        end="2025-10-12T00:00:00+00:00",
        source="CoinGecko/Axios reported the Oct 10-11, 2025 crypto liquidation cascade.",
        note="Covered by current local one-hour Binance feature files.",
    ),
    CrashEvent(
        name="2026-02-05 bitcoin under 64k",
        start="2026-02-05T00:00:00+00:00",
        end="2026-02-06T00:00:00+00:00",
        source="Axios reported Bitcoin fell under $64K on Feb 5, 2026 with forced liquidations amplifying swings.",
        note="Covered by current local one-hour Binance feature files.",
    ),
)


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


def event_window(frame: pd.DataFrame, event: CrashEvent, warmup_bars: int, cooldown_bars: int) -> pd.DataFrame:
    start = parse_dt(event.start) - timedelta(hours=warmup_bars)
    end = parse_dt(event.end) + timedelta(hours=cooldown_bars)
    times = pd.to_datetime(frame["open_time"], utc=True)
    return frame[(times >= start) & (times <= end)].copy()


def data_coverage(frame: pd.DataFrame, event: CrashEvent) -> str:
    if frame.empty:
        return "missing"
    times = pd.to_datetime(frame["open_time"], utc=True)
    start = parse_dt(event.start)
    end = parse_dt(event.end)
    if times.min() <= start and times.max() >= end:
        return "covered"
    if times.max() < start or times.min() > end:
        return "unavailable"
    return "partial"


def market_drop_pct(frame: pd.DataFrame, event: CrashEvent) -> float:
    times = pd.to_datetime(frame["open_time"], utc=True)
    event_rows = frame[(times >= parse_dt(event.start)) & (times <= parse_dt(event.end))]
    if event_rows.empty:
        return 0.0
    before = frame[times < parse_dt(event.start)]
    reference = float(before["close"].iloc[-1]) if not before.empty else float(event_rows["open"].iloc[0])
    if reference <= 0:
        return 0.0
    low = float(event_rows["low"].min())
    return round((low - reference) / reference * 100, 2)


def trade_overlaps_event(trade: Trade, event: CrashEvent) -> bool:
    entry = parse_dt(trade.entry_time)
    exit_time = parse_dt(trade.exit_time)
    return entry <= parse_dt(event.end) and exit_time >= parse_dt(event.start)


def summarize_event(rows: list[dict[str, object]], event: CrashEvent) -> dict[str, object]:
    covered = [row for row in rows if row["coverage"] == "covered"]
    tested = [row for row in covered if row["strategy_status"] == "tested"]
    overlapping_trades = int(sum(int(row["crash_trades"]) for row in tested))
    crash_pnl = round(float(sum(float(row["crash_pnl"]) for row in tested)), 2)
    worst_trade = min((float(row["worst_crash_trade_pct"]) for row in tested), default=0.0)
    worst_market_drop = min((float(row["market_drop_pct"]) for row in covered), default=0.0)
    worst_drawdown = max((float(row["max_drawdown_pct"]) for row in tested), default=0.0)
    finite_profit_factors = [float(row["profit_factor"]) for row in tested if math.isfinite(float(row["profit_factor"]))]
    profit_factor = mean(finite_profit_factors) if finite_profit_factors else 0.0
    skip_count = len([row for row in rows if row["coverage"] != "covered"])
    hard_fail = crash_pnl < -150.0 or worst_trade <= -8.0 or worst_drawdown >= 18.0
    status = "SKIP" if not tested else "FAIL" if hard_fail else "PASS"
    return {
        "event": event.name,
        "status": status,
        "coverage": "covered" if tested else "unavailable",
        "tested_rows": len(tested),
        "skipped_rows": skip_count,
        "overlapping_crash_trades": overlapping_trades,
        "crash_pnl": crash_pnl,
        "avg_profit_factor": round(float(profit_factor), 2),
        "worst_crash_trade_pct": round(worst_trade, 2),
        "worst_market_drop_pct": round(worst_market_drop, 2),
        "worst_strategy_drawdown_pct": round(worst_drawdown, 2),
        "source": event.source,
        "note": event.note,
    }


def run_flash_crash_stress(
    data_dir: Path,
    interval: str,
    days: int,
    symbols: Iterable[str],
    strategies: dict[str, BacktestStrategy],
    warmup_bars: int,
    cooldown_bars: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    detail_rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    feature_cache: dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        path = data_dir / f"{safe_symbol(symbol)}_{interval}_{days}d_features.parquet"
        if path.exists():
            feature_cache[symbol] = load_feature_file(path)

    for event in CRASH_EVENTS:
        event_rows: list[dict[str, object]] = []
        for symbol in symbols:
            frame = feature_cache.get(symbol, pd.DataFrame())
            coverage = data_coverage(frame, event)
            drop = market_drop_pct(frame, event) if coverage in {"covered", "partial"} else 0.0
            if coverage != "covered":
                row = {
                    "event": event.name,
                    "symbol": symbol,
                    "strategy": "",
                    "coverage": coverage,
                    "strategy_status": "skipped",
                    "market_drop_pct": drop,
                    "trades": 0,
                    "crash_trades": 0,
                    "crash_pnl": 0.0,
                    "worst_crash_trade_pct": 0.0,
                    "profit_factor": 0.0,
                    "max_drawdown_pct": 0.0,
                    "reason": event.note,
                }
                event_rows.append(row)
                detail_rows.append(row)
                continue

            replay_frame = event_window(frame, event, warmup_bars, cooldown_bars)
            for strategy_name, strategy in strategies.items():
                try:
                    metrics, trades = strategy.replay(replay_frame)
                    crash_trades = [trade for trade in trades if trade_overlaps_event(trade, event)]
                    crash_pnl = sum(trade.pnl for trade in crash_trades)
                    worst_trade = min((trade.return_pct for trade in crash_trades), default=0.0)
                    row = {
                        "event": event.name,
                        "symbol": symbol,
                        "strategy": strategy_name,
                        "coverage": coverage,
                        "strategy_status": "tested",
                        "market_drop_pct": drop,
                        "trades": metrics.trades,
                        "crash_trades": len(crash_trades),
                        "crash_pnl": round(float(crash_pnl), 2),
                        "worst_crash_trade_pct": round(float(worst_trade), 2),
                        "profit_factor": round(float(metrics.profit_factor), 2) if math.isfinite(float(metrics.profit_factor)) else 99.0,
                        "max_drawdown_pct": round(float(metrics.max_drawdown_pct), 2),
                        "reason": "historical replay window tested",
                    }
                except (KeyError, ValueError, TypeError) as exc:
                    row = {
                        "event": event.name,
                        "symbol": symbol,
                        "strategy": strategy_name,
                        "coverage": coverage,
                        "strategy_status": "error",
                        "market_drop_pct": drop,
                        "trades": 0,
                        "crash_trades": 0,
                        "crash_pnl": 0.0,
                        "worst_crash_trade_pct": 0.0,
                        "profit_factor": 0.0,
                        "max_drawdown_pct": 0.0,
                        "reason": str(exc),
                    }
                event_rows.append(row)
                detail_rows.append(row)
        summaries.append(summarize_event(event_rows, event))
    return summaries, detail_rows


def overall_status(summaries: list[dict[str, object]]) -> str:
    tested = [row for row in summaries if row["status"] != "SKIP"]
    if not tested:
        return "FAIL"
    if any(row["status"] == "FAIL" for row in tested):
        return "FAIL"
    if any(row["status"] == "SKIP" for row in summaries):
        return "WARN"
    return "PASS"


def select_strategies(strategy_set: str, strategy_names: str = "") -> dict[str, BacktestStrategy]:
    if strategy_names.strip():
        requested = [item.strip() for item in strategy_names.split(",") if item.strip()]
    elif strategy_set == "active":
        requested = active_strategy_names()
    elif strategy_set == "all":
        requested = list(STRATEGY_REGISTRY)
    else:
        raise ValueError(f"unsupported strategy set: {strategy_set}")

    missing = [name for name in requested if name not in STRATEGY_REGISTRY]
    if missing:
        raise ValueError("unknown strategy names: " + ", ".join(missing))
    selected = {name: STRATEGY_REGISTRY[name] for name in requested}
    if not selected:
        raise ValueError("no strategies selected for flash-crash stress")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay local strategies across known crypto flash-crash windows.")
    parser.add_argument("--data-dir", default="data/binance")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--symbols", default=",".join(TOP_TRADING_SYMBOLS))
    parser.add_argument("--strategy-set", choices=["active", "all"], default="active")
    parser.add_argument("--strategies", default="", help="Comma-separated strategy names. Overrides --strategy-set.")
    parser.add_argument("--warmup-bars", type=int, default=336, help="Warmup bars before each crash window.")
    parser.add_argument("--cooldown-bars", type=int, default=72, help="Cooldown bars after each crash window.")
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    effective_strategy_set = "custom" if args.strategies.strip() else args.strategy_set
    strategies = select_strategies(args.strategy_set, args.strategies)
    summaries, details = run_flash_crash_stress(
        data_dir=Path(args.data_dir),
        interval=args.interval,
        days=args.days,
        symbols=symbols,
        strategies=strategies,
        warmup_bars=args.warmup_bars,
        cooldown_bars=args.cooldown_bars,
    )
    result = {
        "status": overall_status(summaries),
        "generated_at": datetime.now(UTC).isoformat(),
        "interval": args.interval,
        "days": args.days,
        "symbols": symbols,
        "strategy_set": effective_strategy_set,
        "strategies": list(strategies),
        "events": [asdict(event) for event in CRASH_EVENTS],
        "summaries": summaries,
        "recommendations": recommendations(summaries),
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_stem = "historical_flash_crash_stress" if effective_strategy_set == "active" else "historical_flash_crash_stress_custom"
    (out_dir / f"{report_stem}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    pd.DataFrame(details).to_csv(out_dir / f"{report_stem}_details.csv", index=False)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] in {"PASS", "WARN"} else 1)


def recommendations(summaries: list[dict[str, object]]) -> list[str]:
    guidance: list[str] = []
    skipped = [row["event"] for row in summaries if row["status"] == "SKIP"]
    failed = [row for row in summaries if row["status"] == "FAIL"]
    if skipped:
        guidance.append("Backfill older 1h and 5m Binance candles to directly replay skipped windows: " + ", ".join(str(item) for item in skipped) + ".")
    for row in failed:
        if float(row["worst_strategy_drawdown_pct"]) >= 18.0:
            guidance.append(f"{row['event']}: add crash-mode drawdown throttle before allowing new entries.")
        if float(row["crash_pnl"]) < -150.0:
            guidance.append(f"{row['event']}: reduce max cash per bot and require orderflow confirmation during liquidation cascades.")
        if float(row["worst_crash_trade_pct"]) <= -8.0:
            guidance.append(f"{row['event']}: tighten ATR stop or add gap-risk kill switch.")
    if not guidance:
        guidance.append("Keep historical flash-crash replay in the production readiness gate and rerun after each new strategy is registered.")
    return guidance


if __name__ == "__main__":
    main()
