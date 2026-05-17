from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from aegis_trader.storage.db import Base


class ScannerHeartbeatRow(Base):
    __tablename__ = "myts_bot_table_scanner_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(255))
    symbols_ok: Mapped[int] = mapped_column(Integer)
    symbols_error: Mapped[int] = mapped_column(Integer)
    refresh_seconds: Mapped[int] = mapped_column(Integer)
    errors_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiveScanRow(Base):
    __tablename__ = "myts_bot_table_live_scan"
    __table_args__ = (UniqueConstraint("symbol", name="uq_myts_bot_live_scan_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    scan_bucket: Mapped[str] = mapped_column(String(32), index=True)
    scan_reason: Mapped[str] = mapped_column(Text)
    last_close: Mapped[float] = mapped_column(Float)
    active_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    watch_score: Mapped[float] = mapped_column(Float, default=0.0)
    buy_score: Mapped[float] = mapped_column(Float, default=0.0)
    sell_score: Mapped[float] = mapped_column(Float, default=0.0)
    orderflow_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    watch_missing: Mapped[str] = mapped_column(Text, default="")
    buy_missing: Mapped[str] = mapped_column(Text, default="")
    sell_missing: Mapped[str] = mapped_column(Text, default="")
    orderflow_reason: Mapped[str] = mapped_column(Text, default="")
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    profit_factor: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ReplayMetricRow(Base):
    __tablename__ = "myts_bot_table_replay_metrics"
    __table_args__ = (UniqueConstraint("run_id", "symbol", name="uq_myts_bot_replay_run_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    candles: Mapped[int] = mapped_column(Integer)
    trades: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    losses: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    total_return_pct: Mapped[float] = mapped_column(Float)
    profit_factor: Mapped[float] = mapped_column(Float)
    max_drawdown_pct: Mapped[float] = mapped_column(Float)
    avg_trade_return_pct: Mapped[float] = mapped_column(Float)
    sharpe_proxy: Mapped[float] = mapped_column(Float)
    last_close: Mapped[float] = mapped_column(Float)
    scan_bucket: Mapped[str] = mapped_column(String(32))
    scan_reason: Mapped[str] = mapped_column(Text)
    watch_score: Mapped[float] = mapped_column(Float, default=0.0)
    buy_score: Mapped[float] = mapped_column(Float, default=0.0)
    sell_score: Mapped[float] = mapped_column(Float, default=0.0)
    orderflow_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReplayTradeRow(Base):
    __tablename__ = "myts_bot_table_replay_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    take_profit_price: Mapped[float] = mapped_column(Float)
    bars_held: Mapped[int] = mapped_column(Integer)
    return_pct: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(64))


class RiskSettingsRow(Base):
    __tablename__ = "myts_bot_table_risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile: Mapped[str] = mapped_column(String(64), unique=True, default="default")
    capital: Mapped[float] = mapped_column(Float, default=10_000.0)
    max_cash_per_trade: Mapped[float] = mapped_column(Float, default=250.0)
    max_risk_per_trade_pct: Mapped[float] = mapped_column(Float, default=0.01)
    max_trades_per_window: Mapped[int] = mapped_column(Integer, default=6)
    trade_window_minutes: Mapped[int] = mapped_column(Integer, default=240)
    max_portfolio_exposure: Mapped[float] = mapped_column(Float, default=1_000.0)
    kill_switch: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BotInstanceRow(Base):
    __tablename__ = "myts_bot_table_bot_instances"
    __table_args__ = (UniqueConstraint("name", name="uq_myts_bot_instance_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    strategy: Mapped[str] = mapped_column(String(100), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), default="1h")
    capital: Mapped[float] = mapped_column(Float, default=1_000.0)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(32), index=True, default="DRAFT")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JournalEventRow(Base):
    __tablename__ = "myts_bot_table_journal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bot_name: Mapped[str] = mapped_column(String(100), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    decision: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")


class ValidationRunRow(Base):
    __tablename__ = "myts_bot_table_validation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    bot_name: Mapped[str] = mapped_column(String(100), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), default="1h")
    start_date: Mapped[str] = mapped_column(String(32), default="")
    end_date: Mapped[str] = mapped_column(String(32), default="")
    capital: Mapped[float] = mapped_column(Float, default=1_000.0)
    fees_bps: Mapped[float] = mapped_column(Float, default=10.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=5.0)
    state: Mapped[str] = mapped_column(String(32), default="COMPLETED")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
