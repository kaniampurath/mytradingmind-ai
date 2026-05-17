from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_trader.analytics.replay_metrics import SymbolMetrics, Trade
from aegis_trader.storage.models import LiveScanRow, ReplayMetricRow, ReplayTradeRow, ScannerHeartbeatRow


async def write_scan_state(
    session: AsyncSession,
    metrics: list[SymbolMetrics],
    trades: list[Trade],
    heartbeat: dict[str, object],
    run_id: str,
) -> None:
    generated_at = _parse_datetime(str(heartbeat["generated_at"]))
    session.add(
        ScannerHeartbeatRow(
            generated_at=generated_at,
            source=str(heartbeat["source"]),
            base_url=str(heartbeat["base_url"]),
            symbols_ok=int(heartbeat["symbols_ok"]),
            symbols_error=int(heartbeat["symbols_error"]),
            refresh_seconds=int(heartbeat["refresh_seconds"]),
            errors_json=json.dumps(heartbeat.get("errors", {})),
        )
    )
    for item in metrics:
        values = {
            "symbol": item.symbol,
            "scan_bucket": item.scan_bucket,
            "scan_reason": item.scan_reason,
            "last_close": item.last_close,
            "active_entry": item.active_entry,
            "active_pnl": item.active_pnl,
            "active_pnl_pct": item.active_pnl_pct,
            "watch_score": item.watch_score,
            "buy_score": item.buy_score,
            "sell_score": item.sell_score,
            "orderflow_score": item.orderflow_score,
            "confidence_score": item.confidence_score,
            "watch_missing": item.watch_missing,
            "buy_missing": item.buy_missing,
            "sell_missing": item.sell_missing,
            "orderflow_reason": item.orderflow_reason,
            "confidence_reason": item.confidence_reason,
            "trades": item.trades,
            "win_rate": item.win_rate,
            "total_pnl": item.total_pnl,
            "profit_factor": _finite(item.profit_factor),
            "updated_at": generated_at,
        }
        stmt = mysql_insert(LiveScanRow).values(**values)
        stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key] for key in values if key != "symbol"})
        await session.execute(stmt)

        metric_values = {
            "run_id": run_id,
            "symbol": item.symbol,
            "candles": item.candles,
            "trades": item.trades,
            "wins": item.wins,
            "losses": item.losses,
            "win_rate": item.win_rate,
            "total_pnl": item.total_pnl,
            "total_return_pct": item.total_return_pct,
            "profit_factor": _finite(item.profit_factor),
            "max_drawdown_pct": item.max_drawdown_pct,
            "avg_trade_return_pct": item.avg_trade_return_pct,
            "sharpe_proxy": item.sharpe_proxy,
            "last_close": item.last_close,
            "scan_bucket": item.scan_bucket,
            "scan_reason": item.scan_reason,
            "watch_score": item.watch_score,
            "buy_score": item.buy_score,
            "sell_score": item.sell_score,
            "orderflow_score": item.orderflow_score,
            "confidence_score": item.confidence_score,
            "confidence_reason": item.confidence_reason,
        }
        metric_stmt = mysql_insert(ReplayMetricRow).values(**metric_values)
        metric_stmt = metric_stmt.on_duplicate_key_update(**{key: metric_stmt.inserted[key] for key in metric_values if key not in {"run_id", "symbol"}})
        await session.execute(metric_stmt)

    await session.execute(delete(ReplayTradeRow).where(ReplayTradeRow.run_id == run_id))
    session.add_all(
        ReplayTradeRow(
            run_id=run_id,
            symbol=trade.symbol,
            entry_time=_parse_datetime(trade.entry_time),
            exit_time=_parse_datetime(trade.exit_time),
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            stop_price=trade.stop_price,
            take_profit_price=trade.take_profit_price,
            bars_held=trade.bars_held,
            return_pct=trade.return_pct,
            pnl=trade.pnl,
            exit_reason=trade.exit_reason,
        )
        for trade in trades
    )
    await session.commit()


async def read_live_scan(session: AsyncSession) -> pd.DataFrame:
    result = await session.execute(select(LiveScanRow).order_by(LiveScanRow.symbol))
    rows = result.scalars().all()
    return pd.DataFrame(
        [
            {
                "symbol": row.symbol,
                "scan_bucket": row.scan_bucket,
                "scan_reason": row.scan_reason,
                "last_close": row.last_close,
                "active_entry": row.active_entry,
                "active_pnl": row.active_pnl,
                "active_pnl_pct": row.active_pnl_pct,
                "watch_score": row.watch_score,
                "buy_score": row.buy_score,
                "sell_score": row.sell_score,
                "orderflow_score": row.orderflow_score,
                "confidence_score": row.confidence_score,
                "watch_missing": row.watch_missing,
                "buy_missing": row.buy_missing,
                "sell_missing": row.sell_missing,
                "orderflow_reason": row.orderflow_reason,
                "confidence_reason": row.confidence_reason,
                "trades": row.trades,
                "win_rate": row.win_rate,
                "total_pnl": row.total_pnl,
                "profit_factor": row.profit_factor,
            }
            for row in rows
        ]
    )


async def read_latest_heartbeat(session: AsyncSession) -> dict[str, object]:
    result = await session.execute(select(ScannerHeartbeatRow).order_by(ScannerHeartbeatRow.generated_at.desc()).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        return {"generated_at": None, "source": "database_empty", "symbols_ok": 0, "symbols_error": 0}
    return {
        "generated_at": row.generated_at.isoformat(),
        "source": row.source,
        "base_url": row.base_url,
        "symbols_ok": row.symbols_ok,
        "symbols_error": row.symbols_error,
        "refresh_seconds": row.refresh_seconds,
    }


def metrics_to_dataframe(metrics: list[SymbolMetrics]) -> pd.DataFrame:
    return pd.DataFrame([{**asdict(item), "profit_factor": _finite(item.profit_factor)} for item in metrics])


def _finite(value: float) -> float:
    if math.isinf(value):
        return 999_999.0
    if math.isnan(value):
        return 0.0
    return value


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
