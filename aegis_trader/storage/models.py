from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    bot_id: Mapped[str | None] = mapped_column(String(120), unique=True, index=True, nullable=True)
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
    cumulative_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cumulative_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_fees: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_slippage: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_trade_count: Mapped[int] = mapped_column(Integer, default=0)
    last_entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_position_state: Mapped[str] = mapped_column(String(32), default="OUT_OF_TRADE")
    last_trade_event_type: Mapped[str] = mapped_column(String(64), default="")
    last_trade_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_trade_event_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BotRuntimeEventRow(Base):
    __tablename__ = "myts_bot_table_runtime_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_myts_bot_runtime_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), index=True)
    bot_id: Mapped[str] = mapped_column(String(120), index=True)
    bot_name: Mapped[str] = mapped_column(String(100), index=True, default="")
    runtime_instance_id: Mapped[str] = mapped_column(String(160), index=True, default="")
    trade_id: Mapped[str] = mapped_column(String(255), index=True, default="")
    strategy: Mapped[str] = mapped_column(String(100), index=True, default="")
    symbol: Mapped[str] = mapped_column(String(32), index=True, default="")
    timeframe: Mapped[str] = mapped_column(String(16), default="")
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    position_state: Mapped[str] = mapped_column(String(64), default="")
    order_state: Mapped[str] = mapped_column(String(64), default="")
    lifecycle_state: Mapped[str] = mapped_column(String(64), default="")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)
    exposure: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING_ACTIVATION")
    subscription_tier: Mapped[str] = mapped_column(String(32), index=True, default="BASIC_USER")
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    force_password_change: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RoleRow(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    system_role: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PermissionRow(Base):
    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("name", name="uq_permissions_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScreenRow(Base):
    __tablename__ = "screens"
    __table_args__ = (UniqueConstraint("name", name="uq_screens_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    route: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ActionRow(Base):
    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("name", name="uq_actions_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserRoleRow(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.roles.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RolePermissionRow(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.roles.id"), index=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.permissions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoleScreenRow(Base):
    __tablename__ = "role_screens"
    __table_args__ = (UniqueConstraint("role_id", "screen_id", name="uq_role_screens_role_screen"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.roles.id"), index=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.screens.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionRow(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    tier: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="ACTIVE")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserBotSubscriptionRow(Base):
    __tablename__ = "user_bot_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "bot_name", name="uq_user_bot_subscription"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    bot_name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    included_with_tier: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BillingHistoryRow(Base):
    __tablename__ = "billing_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="PLACEHOLDER")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("session_token_hash", name="uq_sessions_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_token_hash: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="ACTIVE")
    roles_json: Mapped[str] = mapped_column(Text, default="[]")
    permissions_json: Mapped[str] = mapped_column(Text, default="[]")
    screens_json: Mapped[str] = mapped_column(Text, default="[]")
    subscription_tier: Mapped[str] = mapped_column(String(32), default="BASIC_USER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ActivationTokenRow(Base):
    __tablename__ = "activation_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_activation_tokens_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{Base.metadata.schema}.users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True, default="ACTIVATION")
    used_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditTrailRow(Base):
    __tablename__ = "audit_trail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_ip: Mapped[str] = mapped_column(String(80), default="")
    session_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    correlation_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminBootstrapCredentialRow(Base):
    __tablename__ = "admin_bootstrap_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_email: Mapped[str] = mapped_column(String(255), index=True)
    temporary_password_hash: Mapped[str] = mapped_column(Text)
    attempts_remaining: Mapped[int] = mapped_column(Integer, default=3)
    used_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    expired_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
