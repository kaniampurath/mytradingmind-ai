from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_trader.storage.models import BotInstanceRow, JournalEventRow, RiskSettingsRow, ValidationRunRow

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
        "created_at",
        "updated_at",
    }
    payload = {key: value for key, value in _clean_dict(values).items() if key in allowed}
    for timestamp_field in ("deployed_at", "heartbeat_at", "created_at", "updated_at"):
        if isinstance(payload.get(timestamp_field), str):
            payload[timestamp_field] = _parse_datetime_string(payload[timestamp_field])
    payload["parameters_json"] = json.dumps(payload.pop("parameters", {}) or {})
    now = datetime.now(UTC)
    if payload.get("state") in {"DEPLOYED", "RUNNING"} and payload.get("deployed_at") is None:
        payload["deployed_at"] = now
    payload["heartbeat_at"] = payload.get("heartbeat_at") or now
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
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _clean_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _clean_value(value) for key, value in values.items()}


def _clean_value(value: Any) -> Any:
    if value is pd.NaT:
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
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
