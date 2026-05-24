from __future__ import annotations

import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_trader.storage.models import BotInstanceRow, BotRuntimeEventRow, JournalEventRow, RiskSettingsRow, ValidationRunRow

logger = logging.getLogger(__name__)


DEFAULT_RISK_SETTINGS: dict[str, Any] = {
    "profile": "default",
    "capital": 10_000.0,
    "max_cash_per_trade": 250.0,
    "max_risk_per_trade_pct": 0.01,
    "max_trades_per_window": 6,
    "trade_window_minutes": 240,
    "max_portfolio_exposure": 1_000.0,
    "kill_switch": False,
}


async def upsert_risk_settings(session: AsyncSession, values: dict[str, Any]) -> None:
    payload = {**DEFAULT_RISK_SETTINGS, **_clean_dict(values)}
    payload["kill_switch"] = int(bool(payload["kill_switch"]))
    stmt = mysql_insert(RiskSettingsRow).values(**payload)
    stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key] for key in payload if key != "profile"})
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "risk_settings_persisted profile=%s max_cash_per_trade=%s max_trades_per_window=%s exposure=%s kill_switch=%s",
        payload["profile"],
        payload["max_cash_per_trade"],
        payload["max_trades_per_window"],
        payload["max_portfolio_exposure"],
        bool(payload["kill_switch"]),
    )


async def read_risk_settings(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(RiskSettingsRow).where(RiskSettingsRow.profile == "default"))
    row = result.scalar_one_or_none()
    if row is None:
        logger.info("risk_settings_read_default profile=default")
        return DEFAULT_RISK_SETTINGS.copy()
    logger.info("risk_settings_read profile=%s kill_switch=%s", row.profile, bool(row.kill_switch))
    return {
        "profile": row.profile,
        "capital": row.capital,
        "max_cash_per_trade": row.max_cash_per_trade,
        "max_risk_per_trade_pct": row.max_risk_per_trade_pct,
        "max_trades_per_window": row.max_trades_per_window,
        "trade_window_minutes": row.trade_window_minutes,
        "max_portfolio_exposure": row.max_portfolio_exposure,
        "kill_switch": bool(row.kill_switch),
    }


async def upsert_bot_instance(session: AsyncSession, values: dict[str, Any]) -> None:
    allowed = {
        "bot_id",
        "name",
        "strategy",
        "symbol",
        "timeframe",
        "capital",
        "parameters",
        "state",
        "status_reason",
        "deployed_at",
        "heartbeat_at",
        "cumulative_started_at",
        "cumulative_realized_pnl",
        "cumulative_fees",
        "cumulative_slippage",
        "cumulative_trade_count",
        "last_entry_at",
        "last_exit_at",
        "runtime_position_state",
        "last_trade_event_type",
        "last_trade_event_at",
        "last_trade_event_reason",
        "created_at",
        "updated_at",
    }
    payload = {key: value for key, value in _clean_dict(values).items() if key in allowed}
    for timestamp_field in ("deployed_at", "heartbeat_at", "cumulative_started_at", "last_entry_at", "last_exit_at", "last_trade_event_at", "created_at", "updated_at"):
        if isinstance(payload.get(timestamp_field), str):
            payload[timestamp_field] = _parse_datetime_string(payload[timestamp_field])
    payload["bot_id"] = payload.get("bot_id") or _bot_id_from_name(str(payload.get("name", "")))
    payload["parameters_json"] = json.dumps(payload.pop("parameters", {}) or {})
    now = datetime.now(UTC)
    if payload.get("state") in {"DEPLOYED", "RUNNING"} and payload.get("deployed_at") is None:
        payload["deployed_at"] = now
    if payload.get("state") in {"DEPLOYED", "RUNNING"} and payload.get("cumulative_started_at") is None:
        payload["cumulative_started_at"] = payload.get("created_at") or payload.get("deployed_at") or now
    payload["heartbeat_at"] = payload.get("heartbeat_at") or now
    payload["created_at"] = payload.get("created_at") or now
    payload["updated_at"] = payload.get("updated_at") or now
    for numeric_field, default in {
        "cumulative_realized_pnl": 0.0,
        "cumulative_fees": 0.0,
        "cumulative_slippage": 0.0,
        "cumulative_trade_count": 0,
    }.items():
        payload[numeric_field] = payload.get(numeric_field) if payload.get(numeric_field) is not None else default
    stmt = mysql_insert(BotInstanceRow).values(**payload)
    stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key] for key in payload if key != "name"})
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "bot_instance_persisted name=%s strategy=%s symbol=%s state=%s capital=%s",
        payload.get("name"),
        payload.get("strategy"),
        payload.get("symbol"),
        payload.get("state"),
        payload.get("capital"),
    )


async def read_bot_instances(session: AsyncSession) -> pd.DataFrame:
    result = await session.execute(select(BotInstanceRow).order_by(BotInstanceRow.created_at.desc()))
    rows = result.scalars().all()
    logger.info("bot_instances_read count=%s", len(rows))
    return pd.DataFrame([_bot_row(row) for row in rows])


async def delete_bot_instance(session: AsyncSession, name: str) -> int:
    result = await session.execute(delete(BotInstanceRow).where(BotInstanceRow.name == name))
    await session.commit()
    deleted = int(result.rowcount or 0)
    logger.info("bot_instance_deleted name=%s rows=%s", name, deleted)
    return deleted


async def append_runtime_event(session: AsyncSession, event: dict[str, Any]) -> None:
    event = _clean_dict(event)
    payload = _runtime_event_payload(event)
    existing = await session.scalar(select(BotRuntimeEventRow.id).where(BotRuntimeEventRow.event_id == payload["event_id"]))
    if existing is not None:
        return
    stmt = mysql_insert(BotRuntimeEventRow).values(**payload)
    stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key] for key in payload if key != "event_id"})
    await session.execute(stmt)
    await _apply_runtime_event_to_bot(session, payload)
    await session.commit()
    logger.info(
        "runtime_event_persisted bot_id=%s event_type=%s symbol=%s realized_pnl=%s",
        payload["bot_id"],
        payload["event_type"],
        payload["symbol"],
        payload["realized_pnl"],
    )


async def read_runtime_events(session: AsyncSession, limit: int = 10_000) -> pd.DataFrame:
    result = await session.execute(select(BotRuntimeEventRow).order_by(BotRuntimeEventRow.event_time.desc()).limit(limit))
    rows = result.scalars().all()
    logger.info("runtime_events_read count=%s limit=%s", len(rows), limit)
    return pd.DataFrame([_runtime_event_row(row) for row in rows])


async def append_journal_event(session: AsyncSession, event: dict[str, Any]) -> None:
    event = _clean_dict(event)
    payload = {
        "event_time": event.get("event_time") or datetime.now(UTC),
        "bot_name": event.get("bot_name", "SYSTEM"),
        "symbol": event.get("symbol", ""),
        "event_type": event.get("event_type", "INFO"),
        "severity": event.get("severity", "INFO"),
        "decision": event.get("decision", ""),
        "reason": event.get("reason", ""),
        "metrics_json": json.dumps(event.get("metrics", {})),
    }
    session.add(JournalEventRow(**payload))
    await session.commit()
    logger.info(
        "journal_event_persisted bot=%s symbol=%s event_type=%s severity=%s decision=%s",
        payload["bot_name"],
        payload["symbol"],
        payload["event_type"],
        payload["severity"],
        payload["decision"],
    )


async def read_journal_events(session: AsyncSession, limit: int = 250) -> pd.DataFrame:
    result = await session.execute(select(JournalEventRow).order_by(JournalEventRow.event_time.desc()).limit(limit))
    rows = result.scalars().all()
    logger.info("journal_events_read count=%s limit=%s", len(rows), limit)
    return pd.DataFrame(
        [
            {
                "event_time": row.event_time,
                "bot_name": row.bot_name,
                "symbol": row.symbol,
                "event_type": row.event_type,
                "severity": row.severity,
                "decision": row.decision,
                "reason": row.reason,
                "metrics": json.loads(row.metrics_json or "{}"),
            }
            for row in rows
        ]
    )


async def upsert_validation_run(session: AsyncSession, values: dict[str, Any]) -> None:
    allowed = {
        "run_id",
        "bot_name",
        "symbol",
        "timeframe",
        "start_date",
        "end_date",
        "capital",
        "fees_bps",
        "slippage_bps",
        "state",
        "metrics",
    }
    payload = {key: value for key, value in _clean_dict(values).items() if key in allowed}
    payload["metrics_json"] = json.dumps(payload.pop("metrics", {}) or {})
    stmt = mysql_insert(ValidationRunRow).values(**payload)
    stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key] for key in payload if key != "run_id"})
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "validation_run_persisted run_id=%s bot=%s symbol=%s state=%s",
        payload.get("run_id"),
        payload.get("bot_name"),
        payload.get("symbol"),
        payload.get("state"),
    )


async def read_validation_runs(session: AsyncSession, limit: int = 100) -> pd.DataFrame:
    result = await session.execute(select(ValidationRunRow).order_by(ValidationRunRow.created_at.desc()).limit(limit))
    rows = result.scalars().all()
    logger.info("validation_runs_read count=%s limit=%s", len(rows), limit)
    return pd.DataFrame(
        [
            {
                "run_id": row.run_id,
                "bot_name": row.bot_name,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "capital": row.capital,
                "fees_bps": row.fees_bps,
                "slippage_bps": row.slippage_bps,
                "state": row.state,
                **json.loads(row.metrics_json or "{}"),
                "created_at": row.created_at,
            }
            for row in rows
        ]
    )


def _bot_row(row: BotInstanceRow) -> dict[str, Any]:
    return {
        "bot_id": row.bot_id or _bot_id_from_name(row.name),
        "name": row.name,
        "strategy": row.strategy,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "capital": row.capital,
        "parameters": json.loads(row.parameters_json or "{}"),
        "state": row.state,
        "status_reason": row.status_reason,
        "deployed_at": row.deployed_at,
        "heartbeat_at": row.heartbeat_at,
        "cumulative_started_at": row.cumulative_started_at,
        "cumulative_realized_pnl": row.cumulative_realized_pnl,
        "cumulative_fees": row.cumulative_fees,
        "cumulative_slippage": row.cumulative_slippage,
        "cumulative_trade_count": row.cumulative_trade_count,
        "last_entry_at": row.last_entry_at,
        "last_exit_at": row.last_exit_at,
        "runtime_position_state": row.runtime_position_state,
        "last_trade_event_type": row.last_trade_event_type,
        "last_trade_event_at": row.last_trade_event_at,
        "last_trade_event_reason": row.last_trade_event_reason,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _runtime_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    metrics = event.get("metrics", {}) if isinstance(event.get("metrics"), dict) else {}
    event_time = event.get("event_time") or event.get("snapshot_time") or datetime.now(UTC)
    if isinstance(event_time, str):
        event_time = _parse_datetime_string(event_time) or datetime.now(UTC)
    bot_id = str(event.get("bot_id") or event.get("bot_name") or "unknown")
    realized_pnl = event.get("realized_pnl", metrics.get("realized_pnl", 0.0))
    unrealized_pnl = event.get("unrealized_pnl", metrics.get("unrealized_pnl", 0.0))
    return {
        "event_id": str(event.get("event_id") or event.get("snapshot_id") or f"{bot_id}:{event.get('event_type', 'RuntimeEvent')}:{datetime.now(UTC).isoformat()}"),
        "bot_id": bot_id,
        "bot_name": str(event.get("bot_name") or event.get("name") or ""),
        "runtime_instance_id": str(event.get("runtime_instance_id") or bot_id),
        "trade_id": str(event.get("trade_id") or ""),
        "strategy": str(event.get("strategy") or metrics.get("strategy", "")),
        "symbol": str(event.get("symbol") or ""),
        "timeframe": str(event.get("timeframe") or metrics.get("timeframe", "")),
        "event_type": str(event.get("event_type") or "RuntimeEvent"),
        "event_time": event_time,
        "position_state": str(event.get("position_state") or ""),
        "order_state": str(event.get("order_state") or ""),
        "lifecycle_state": str(event.get("lifecycle_state") or ""),
        "quantity": float(event.get("quantity") or metrics.get("quantity", 0.0) or 0.0),
        "price": float(event.get("price") or event.get("current_price") or metrics.get("price", 0.0) or 0.0),
        "realized_pnl": float(realized_pnl or 0.0),
        "unrealized_pnl": float(unrealized_pnl or 0.0),
        "roi_pct": float(event.get("roi_pct") or metrics.get("roi_pct", 0.0) or 0.0),
        "exposure": float(event.get("exposure") or metrics.get("exposure", 0.0) or 0.0),
        "drawdown_pct": float(event.get("drawdown_pct") or metrics.get("drawdown_pct", 0.0) or 0.0),
        "fees": float(event.get("fees") or metrics.get("fees", 0.0) or 0.0),
        "slippage": float(event.get("slippage") or metrics.get("slippage", 0.0) or 0.0),
        "capital": float(event.get("capital") or metrics.get("capital", 0.0) or 0.0),
        "reason": str(event.get("reason") or ""),
        "details_json": json.dumps({key: value for key, value in event.items() if key not in {"metrics"}} | {"metrics": metrics}, default=str),
    }


async def _apply_runtime_event_to_bot(session: AsyncSession, payload: dict[str, Any]) -> None:
    result = await session.execute(select(BotInstanceRow).where(BotInstanceRow.bot_id == payload["bot_id"]))
    row = result.scalar_one_or_none()
    if row is None and payload["bot_name"]:
        result = await session.execute(select(BotInstanceRow).where(BotInstanceRow.name == payload["bot_name"]))
        row = result.scalar_one_or_none()
    if row is None:
        return
    if row.bot_id is None:
        row.bot_id = payload["bot_id"]
    if row.cumulative_started_at is None:
        row.cumulative_started_at = row.created_at or payload["event_time"]
    row.heartbeat_at = payload["event_time"]
    event_type = str(payload["event_type"])
    if event_type in {"TradeEntered", "TradeExited", "StopTriggered", "RiskTriggered", "BOT_STOPPED"}:
        row.last_trade_event_type = event_type
        row.last_trade_event_at = payload["event_time"]
        row.last_trade_event_reason = str(payload.get("reason") or "")
    if event_type == "TradeEntered":
        row.runtime_position_state = "IN_TRADE"
        row.last_entry_at = payload["event_time"]
    if event_type in {"TradeExited", "StopTriggered", "RiskTriggered", "BOT_STOPPED"}:
        row.runtime_position_state = "OUT_OF_TRADE"
        row.last_exit_at = payload["event_time"]
    if event_type == "PNL_SNAPSHOT":
        details = {}
        try:
            details = json.loads(payload.get("details_json") or "{}")
        except json.JSONDecodeError:
            details = {}
        metrics = details.get("metrics", {}) if isinstance(details, dict) else {}
        row.cumulative_realized_pnl = float(payload["realized_pnl"] or 0.0)
        row.cumulative_fees = float(payload["fees"] or 0.0)
        row.cumulative_slippage = float(payload["slippage"] or 0.0)
        trade_count = metrics.get("trades", metrics.get("total_trades")) if isinstance(metrics, dict) else None
        if trade_count is not None:
            row.cumulative_trade_count = int(float(trade_count or 0))


def _runtime_event_row(row: BotRuntimeEventRow) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "event_time": row.event_time,
        "trade_id": row.trade_id,
        "bot_id": row.bot_id,
        "bot_name": row.bot_name,
        "runtime_instance_id": row.runtime_instance_id,
        "strategy": row.strategy,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "event_type": row.event_type,
        "order_state": row.order_state,
        "position_state": row.position_state,
        "lifecycle_state": row.lifecycle_state,
        "price": row.price,
        "current_price": row.price,
        "quantity": row.quantity,
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "roi_pct": row.roi_pct,
        "exposure": row.exposure,
        "drawdown_pct": row.drawdown_pct,
        "fees": row.fees,
        "slippage": row.slippage,
        "capital": row.capital,
        "reason": row.reason,
        "metrics": json.loads(row.details_json or "{}"),
    }


def _bot_id_from_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "bot"


def _clean_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _clean_value(value) for key, value in values.items()}


def _clean_value(value: Any) -> Any:
    if value is pd.NaT:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "nat", "nan", "none", "null"}:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _parse_datetime_string(value: str) -> datetime | str:
    if value.strip().lower() in {"", "nat", "nan", "none", "null"}:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
