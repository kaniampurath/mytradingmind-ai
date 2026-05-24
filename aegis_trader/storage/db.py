from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from aegis_trader.core.config import settings
from aegis_trader.core.logging import log_diagnostic, redact_url

SCHEMA_TOKEN = "app_schema"
logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA_TOKEN)

    pass


def build_engine(database_url: str | None = None) -> AsyncEngine:
    resolved_url = normalize_async_database_url(database_url or settings.database_url)
    log_diagnostic(logger, "database_engine_build", url=redact_url(resolved_url), schema=settings.database_schema)
    return create_async_engine(
        resolved_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        poolclass=NullPool,
        execution_options={"schema_translate_map": _schema_translate_map(resolved_url)},
    )


def build_session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine or build_engine(), expire_on_commit=False)


async def session_scope(database_url: str | None = None) -> AsyncIterator[AsyncSession]:
    engine = build_engine(database_url)
    factory = build_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def create_schema(database_url: str | None = None) -> None:
    from aegis_trader.storage import models  # noqa: F401

    raw_url = database_url or settings.database_url
    driver_url = _normalize_async_driver_url(raw_url)
    log_diagnostic(logger, "database_schema_create_start", url=redact_url(driver_url), schema=settings.database_schema)
    if _is_mysql_url(driver_url) and settings.database_schema:
        server_url = str(make_url(driver_url).set(database="").render_as_string(hide_password=False))
        server_engine = create_async_engine(server_url, pool_pre_ping=True, pool_recycle=1800)
        try:
            async with server_engine.begin() as conn:
                await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{_mysql_identifier(settings.database_schema)}`"))
            log_diagnostic(logger, "database_schema_ensure_ok", schema=settings.database_schema)
        except SQLAlchemyError:
            logger.exception("database_schema_ensure_failed schema=%s", settings.database_schema)
            # Some deployments pre-create schemas and grant only table-level access.
            # In that case create_all below is the authority on whether access is valid.
            pass
        finally:
            await server_engine.dispose()

    engine = build_engine(raw_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_bot_instance_runtime_columns(conn)
    factory = build_session_factory(engine)
    async with factory() as session:
        from aegis_trader.security.auth import bootstrap_security_defaults

        await bootstrap_security_defaults(session)
    await engine.dispose()
    log_diagnostic(logger, "database_schema_create_complete", schema=settings.database_schema, tables=len(Base.metadata.tables))


async def _ensure_bot_instance_runtime_columns(conn) -> None:
    """Additive compatibility shim for v1.2 runtime CAGR fields.

    SQLAlchemy create_all creates missing tables but does not mutate existing
    tables. These columns are intentionally additive and nullable/defaulted so
    older deployments can restart without manual SQL.
    """

    dialect = conn.dialect.name
    table_name = "myts_bot_table_bot_instances"
    schema = settings.database_schema if dialect.startswith("mysql") and settings.database_schema else None
    existing = await conn.run_sync(lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns(table_name, schema=schema)})
    column_defs = {
        "bot_id": "VARCHAR(120) NULL",
        "cumulative_started_at": "DATETIME NULL",
        "cumulative_realized_pnl": "FLOAT DEFAULT 0",
        "cumulative_fees": "FLOAT DEFAULT 0",
        "cumulative_slippage": "FLOAT DEFAULT 0",
        "cumulative_trade_count": "INTEGER DEFAULT 0",
        "last_entry_at": "DATETIME NULL",
        "last_exit_at": "DATETIME NULL",
        "runtime_position_state": "VARCHAR(32) DEFAULT 'OUT_OF_TRADE'",
        "last_trade_event_type": "VARCHAR(64) DEFAULT ''",
        "last_trade_event_at": "DATETIME NULL",
        "last_trade_event_reason": "TEXT",
    }
    qualified = f"`{_mysql_identifier(schema)}`.`{table_name}`" if schema else table_name
    for column, definition in column_defs.items():
        if column in existing:
            continue
        if dialect.startswith("mysql"):
            await conn.execute(text(f"ALTER TABLE {qualified} ADD COLUMN `{column}` {definition}"))
        else:
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"))


def normalize_async_database_url(database_url: str) -> str:
    """Accept common local MySQL URLs while preserving async SQLAlchemy execution."""
    url = make_url(_normalize_async_driver_url(database_url))
    if _is_mysql_url(url) and settings.database_schema:
        return url.set(database=settings.database_schema).render_as_string(hide_password=False)
    return url.render_as_string(hide_password=False)


def _normalize_async_driver_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername == "mysql+pymysql":
        return url.set(drivername="mysql+aiomysql").render_as_string(hide_password=False)
    return url.render_as_string(hide_password=False)


def _schema_translate_map(database_url: str) -> dict[str, str | None]:
    if _is_mysql_url(database_url) and settings.database_schema:
        return {SCHEMA_TOKEN: settings.database_schema}
    return {SCHEMA_TOKEN: None}


def _is_mysql_url(database_url: str | URL) -> bool:
    drivername = make_url(str(database_url)).drivername
    return drivername.startswith("mysql")


def _mysql_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe MySQL identifier: {value!r}")
    return value
