from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any


FRAMEWORK_NAME = "PRODUCTION_STABILITY_V2"


@dataclass(frozen=True)
class ExecutionSymbolRules:
    """Exchange-aligned order sanitizer with no exchange side effects."""

    min_qty: float = 0.0
    step_size: float = 0.000001
    min_notional: float = 0.0
    tick_size: float = 0.000001
    max_capital_usd: float = 1_000.0

    def sanitize_qty(self, price: float, qty: float) -> tuple[float | None, str | None]:
        if price <= 0 or qty <= 0:
            return None, "INVALID_PRICE_OR_QTY"
        step = max(float(self.step_size), 0.00000001)
        reasons: list[str] = []
        if qty < self.min_qty:
            reasons.append("QTY_BELOW_MIN")
        if price * qty < self.min_notional:
            reasons.append("NOTIONAL_BELOW_MIN")
        adjusted = max(float(qty), float(self.min_qty), float(self.min_notional) / price if self.min_notional else 0.0)
        adjusted = math.floor(adjusted / step) * step
        precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        adjusted = float(f"{adjusted:.{precision}f}")
        if adjusted <= 0:
            return None, "SANITIZED_QTY_ZERO"
        if adjusted * price > self.max_capital_usd:
            return None, "CAPITAL_EXCEEDED"
        return adjusted, " / ".join(reasons) if reasons else None

    def sanitize_price(self, price: float) -> float:
        tick = max(float(self.tick_size), 0.00000001)
        return float(f"{math.floor(float(price) / tick) * tick:.12f}")


@dataclass(frozen=True)
class BotStabilityConfig:
    framework_name: str = FRAMEWORK_NAME
    heartbeat_stale_seconds: int = 120
    heartbeat_halt_seconds: int = 300
    signal_queue_max: int = 2_000
    db_queue_max: int = 10_000
    queue_warning_pct: float = 0.80
    max_daily_loss_pct: float = 2.0
    max_daily_profit_usd: float = 3_000.0
    max_trades_day: int = 10
    max_consecutive_losses: int = 3
    max_drawdown_pct: float = 5.0
    max_portfolio_exposure_pct: float = 80.0
    max_single_symbol_exposure_pct: float = 25.0
    market_data_stale_seconds: int = 180
    exchange_clock_drift_seconds: int = 5
    require_protection: bool = True
    emergency_exit_bypasses_daily_pnl: bool = True
    execution_rules: ExecutionSymbolRules = field(default_factory=ExecutionSymbolRules)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BotStabilityState:
    framework_name: str = FRAMEWORK_NAME
    status: str = "READY"
    risk_state: str = "OK"
    protection_state: str = "UNKNOWN"
    day: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    daily_realized_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    equity_peak: float | None = None
    restart_required: bool = False
    shutdown_required: bool = False
    last_heartbeat: str = ""
    last_reason: str = ""
    supervisor_action: str = "NONE"
    alert_level: str = "INFO"
    alert_code: str = ""
    data_state: str = "UNKNOWN"
    reconciliation_state: str = "NOT_RUN"
    portfolio_state: str = "UNKNOWN"
    in_flight: dict[str, bool] = field(default_factory=lambda: {"entry": False, "partial": False, "exit": False, "stop": False, "emergency": False})
    processed_events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BotStabilityAlert:
    timestamp: str
    level: str
    code: str
    message: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReconciliationPlan:
    action: str
    reason: str
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderLifecycleEvent:
    timestamp: str
    bot_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    price: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProductionStabilityFramework:
    """Reusable bot runtime guardrail adapted from the long-running production bot.

    It deliberately does not talk to Binance, write to storage, or place orders.
    The runtime/execution layers consume its decisions and perform side effects.
    """

    def __init__(self, config: BotStabilityConfig | None = None, state: BotStabilityState | None = None) -> None:
        self.config = config or BotStabilityConfig()
        self.state = state or BotStabilityState(framework_name=self.config.framework_name)

    def startup_check(self, *, has_position: bool = False, protection_verified: bool = False) -> tuple[bool, str]:
        self.state.status = "READY"
        self.state.protection_state = "PROTECTED" if protection_verified else "UNPROTECTED" if has_position else "FLAT"
        if has_position and self.config.require_protection and not protection_verified:
            self.state.status = "HALTED"
            self.state.shutdown_required = True
            self.state.risk_state = "PROTECTION_REQUIRED"
            self.state.last_reason = "existing position is missing protective stop/OCO"
            return False, self.state.last_reason
        self.state.last_reason = "framework ready"
        return True, self.state.last_reason

    def heartbeat(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        self._reset_day_if_needed(current.date())
        self.state.last_heartbeat = current.isoformat()
        if self.state.status not in {"HALTED", "RISK_LOCKED"}:
            self.state.status = "READY"
        return self.state.to_dict()

    def detect_stale_heartbeat(self, last_seen: datetime | None, now: datetime | None = None) -> tuple[bool, str]:
        if last_seen is None:
            self.state.restart_required = True
            self._alert("CRITICAL", "HEARTBEAT_MISSING", "heartbeat missing", "RESTART")
            return True, self.state.last_reason
        current = now or datetime.now(UTC)
        age = max(0.0, (current - last_seen).total_seconds())
        if age > self.config.heartbeat_halt_seconds:
            self.state.restart_required = True
            self.state.shutdown_required = True
            self.state.status = "HALTED"
            self._alert("CRITICAL", "HEARTBEAT_HALTED", f"heartbeat stale > {self.config.heartbeat_halt_seconds}s", "HALT_AND_RESTART")
            return True, self.state.last_reason
        if age > self.config.heartbeat_stale_seconds:
            self.state.restart_required = True
            self._alert("WARNING", "HEARTBEAT_STALE", f"heartbeat stale > {self.config.heartbeat_stale_seconds}s", "RESTART")
            return True, self.state.last_reason
        self.state.restart_required = False
        self.state.supervisor_action = "NONE"
        return False, "heartbeat fresh"

    def evaluate_supervision(
        self,
        *,
        last_seen: datetime | None = None,
        last_market_data_seen: datetime | None = None,
        exchange_time: datetime | None = None,
        signal_queue_depth: int = 0,
        db_queue_depth: int = 0,
        has_position: bool = False,
        protection_verified: bool = True,
        now: datetime | None = None,
    ) -> BotStabilityAlert:
        self.detect_stale_heartbeat(last_seen, now)
        if self.state.alert_code in {"HEARTBEAT_STALE", "HEARTBEAT_HALTED", "HEARTBEAT_MISSING"}:
            return self.alert()
        if (last_market_data_seen is not None or exchange_time is not None) and self.data_feed_is_unhealthy(last_market_data_seen, exchange_time, now):
            return self.alert()
        if has_position and self.config.require_protection and not protection_verified:
            self.mark_protection(False, "position protection missing")
            return self._alert("CRITICAL", "PROTECTION_MISSING", "position protection missing", "HALT_AND_PROTECT")
        if self._queue_near_capacity(signal_queue_depth, self.config.signal_queue_max):
            return self._alert("WARNING", "SIGNAL_QUEUE_HIGH", "signal queue near capacity", "THROTTLE")
        if self._queue_near_capacity(db_queue_depth, self.config.db_queue_max):
            return self._alert("WARNING", "DB_QUEUE_HIGH", "db queue near capacity", "THROTTLE")
        if self.state.shutdown_required:
            return self._alert("CRITICAL", "SHUTDOWN_REQUIRED", self.state.last_reason or "shutdown required", "HALT")
        if self.state.restart_required:
            return self._alert(self.state.alert_level or "WARNING", self.state.alert_code or "RESTART_REQUIRED", self.state.last_reason, "RESTART")
        return self._alert("INFO", "FRAMEWORK_OK", "framework ready", "NONE")

    def restart_reconciliation_plan(
        self,
        *,
        exchange_position_qty: float,
        local_position_qty: float,
        protection_verified: bool,
        open_orders_verified: bool,
    ) -> ReconciliationPlan:
        checks = (
            "exchange_position",
            "local_position",
            "open_orders",
            "protective_stop_or_oco",
            "last_processed_event",
        )
        if abs(float(exchange_position_qty) - float(local_position_qty)) > 0.00000001:
            self.state.restart_required = True
            self.state.reconciliation_state = "MISMATCH"
            self._alert("CRITICAL", "POSITION_RECONCILIATION_MISMATCH", "exchange/local position mismatch", "HALT_AND_RECONCILE")
            return ReconciliationPlan("HALT_AND_RECONCILE", self.state.last_reason, checks)
        if exchange_position_qty > 0 and self.config.require_protection and not protection_verified:
            self.mark_protection(False, "restart reconciliation found missing protection")
            self.state.reconciliation_state = "UNPROTECTED"
            return ReconciliationPlan("HALT_AND_PROTECT", self.state.last_reason, checks)
        if exchange_position_qty > 0 and not open_orders_verified:
            self.state.reconciliation_state = "OPEN_ORDER_UNKNOWN"
            self._alert("CRITICAL", "ORDER_RECONCILIATION_REQUIRED", "open order state could not be verified", "HALT_AND_RECONCILE")
            return ReconciliationPlan("HALT_AND_RECONCILE", self.state.last_reason, checks)
        self.state.reconciliation_state = "OK"
        return ReconciliationPlan("RESUME", "restart reconciliation passed", checks)

    def data_feed_is_unhealthy(
        self,
        last_market_data_seen: datetime | None,
        exchange_time: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        if last_market_data_seen is None:
            self.state.data_state = "MISSING"
            self._alert("CRITICAL", "MARKET_DATA_MISSING", "market data heartbeat missing", "HALT")
            return True
        age = max(0.0, (current - last_market_data_seen).total_seconds())
        if age > self.config.market_data_stale_seconds:
            self.state.data_state = "STALE"
            self._alert("CRITICAL", "MARKET_DATA_STALE", f"market data stale > {self.config.market_data_stale_seconds}s", "HALT")
            return True
        if exchange_time is not None:
            drift = abs((current - exchange_time).total_seconds())
            if drift > self.config.exchange_clock_drift_seconds:
                self.state.data_state = "CLOCK_DRIFT"
                self._alert("WARNING", "EXCHANGE_CLOCK_DRIFT", f"exchange clock drift > {self.config.exchange_clock_drift_seconds}s", "THROTTLE")
                return True
        self.state.data_state = "OK"
        return False

    def check_portfolio_exposure(
        self,
        *,
        equity: float,
        total_exposure: float,
        symbol_exposure: float,
    ) -> tuple[bool, str]:
        if equity <= 0:
            self.state.portfolio_state = "INVALID_EQUITY"
            self._alert("CRITICAL", "INVALID_EQUITY", "portfolio equity is invalid", "HALT")
            return False, self.state.last_reason
        total_pct = (float(total_exposure) / float(equity)) * 100.0
        symbol_pct = (float(symbol_exposure) / float(equity)) * 100.0
        if total_pct > self.config.max_portfolio_exposure_pct:
            self.state.portfolio_state = "TOTAL_EXPOSURE_LIMIT"
            self._lock("PORTFOLIO_EXPOSURE_LOCK", "portfolio exposure limit reached")
            return False, self.state.last_reason
        if symbol_pct > self.config.max_single_symbol_exposure_pct:
            self.state.portfolio_state = "SYMBOL_EXPOSURE_LIMIT"
            self._lock("SYMBOL_EXPOSURE_LOCK", "single-symbol exposure limit reached")
            return False, self.state.last_reason
        self.state.portfolio_state = "OK"
        return True, "portfolio exposure approved"

    def record_order_lifecycle(
        self,
        *,
        bot_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        status: str,
        quantity: float,
        price: float,
        reason: str = "",
        now: datetime | None = None,
    ) -> OrderLifecycleEvent:
        event = OrderLifecycleEvent(
            timestamp=(now or datetime.now(UTC)).isoformat(),
            bot_id=bot_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            status=status,
            quantity=float(quantity),
            price=float(price),
            reason=reason,
        )
        self.mark_event(f"{bot_id}:{client_order_id}:{status}")
        return event

    def alert_payloads(self, channels: tuple[str, ...] = ("webhook", "email", "telegram")) -> list[dict[str, Any]]:
        alert = self.alert().to_dict()
        return [{"channel": channel, **alert} for channel in channels]

    def can_open_trade(self, equity: float | None = None) -> tuple[bool, str]:
        if self.state.shutdown_required:
            return False, self.state.last_reason or "framework halted"
        if any(self.state.in_flight.values()):
            return False, "execution operation already in flight"
        if self.state.risk_state != "OK":
            return False, self.state.last_reason or self.state.risk_state
        if self.state.trades_today >= self.config.max_trades_day:
            self._lock("MAX_TRADES_DAY", "max trades per day reached")
            return False, "max trades per day reached"
        if self.state.consecutive_losses >= self.config.max_consecutive_losses:
            self._lock("CONSECUTIVE_LOSS_LOCK", "consecutive loss lockout")
            return False, "consecutive loss lockout"
        if self.state.daily_realized_pnl <= -self._daily_loss_limit(equity):
            self._lock("DAILY_LOSS_LOCK", "daily loss limit reached")
            return False, "daily loss limit reached"
        if self.state.daily_realized_pnl >= self.config.max_daily_profit_usd:
            self._lock("DAILY_PROFIT_LOCK", "daily profit lock reached")
            return False, "daily profit lock reached"
        return True, "framework approved"

    def begin_operation(self, operation: str, event_key: str | None = None) -> tuple[bool, str]:
        if operation not in self.state.in_flight:
            return False, f"unknown operation: {operation}"
        if event_key and self.seen_event(event_key):
            return False, "duplicate event blocked"
        if any(self.state.in_flight.values()):
            return False, "operation already in flight"
        self.state.in_flight[operation] = True
        if event_key:
            self.mark_event(event_key)
        return True, "operation accepted"

    def end_operation(self, operation: str, *, success: bool = True, reason: str = "") -> None:
        if operation in self.state.in_flight:
            self.state.in_flight[operation] = False
        if not success:
            self.state.last_reason = reason or f"{operation} failed"

    def record_trade_result(self, realized_pnl: float, equity: float | None = None, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        self._reset_day_if_needed(current.date())
        self.state.trades_today += 1
        self.state.daily_realized_pnl += float(realized_pnl)
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if realized_pnl < 0 else 0
        if equity is not None:
            self.state.equity_peak = max(float(equity), float(self.state.equity_peak or equity))
            drawdown_pct = 0.0 if not self.state.equity_peak else ((self.state.equity_peak - float(equity)) / self.state.equity_peak) * 100
            if drawdown_pct >= self.config.max_drawdown_pct:
                self._lock("GLOBAL_DRAWDOWN_LOCK", "global drawdown lock reached")
        self.can_open_trade(equity)

    def mark_protection(self, verified: bool, reason: str = "") -> None:
        self.state.protection_state = "PROTECTED" if verified else "UNPROTECTED"
        if not verified and self.config.require_protection:
            self.state.shutdown_required = True
            self.state.status = "HALTED"
            self.state.risk_state = "PROTECTION_REQUIRED"
            self._alert("CRITICAL", "PROTECTION_REQUIRED", reason or "protective order missing", "HALT_AND_PROTECT")

    def seen_event(self, event_key: str) -> bool:
        return event_key in set(self.state.processed_events)

    def mark_event(self, event_key: str) -> None:
        if event_key in set(self.state.processed_events):
            return
        self.state.processed_events.append(event_key)
        self.state.processed_events = self.state.processed_events[-2_000:]

    def snapshot(self) -> dict[str, Any]:
        return {"config": self.config.to_dict(), "state": self.state.to_dict()}

    def alert(self) -> BotStabilityAlert:
        return BotStabilityAlert(
            timestamp=datetime.now(UTC).isoformat(),
            level=self.state.alert_level,
            code=self.state.alert_code,
            message=self.state.last_reason,
            action=self.state.supervisor_action,
        )

    def _lock(self, reason: str, message: str | None = None) -> None:
        self.state.risk_state = reason
        self.state.status = "RISK_LOCKED"
        self._alert("WARNING", reason, message or reason, "BLOCK_NEW_ENTRIES")

    def _reset_day_if_needed(self, current_day: date) -> None:
        if self.state.day != current_day.isoformat():
            self.state.day = current_day.isoformat()
            self.state.daily_realized_pnl = 0.0
            self.state.trades_today = 0
            self.state.consecutive_losses = 0
            if self.state.risk_state in {
                "MAX_TRADES_DAY",
                "CONSECUTIVE_LOSS_LOCK",
                "DAILY_LOSS_LOCK",
                "DAILY_PROFIT_LOCK",
                "PORTFOLIO_EXPOSURE_LOCK",
                "SYMBOL_EXPOSURE_LOCK",
            }:
                self.state.risk_state = "OK"
                self.state.status = "READY"
                self.state.last_reason = "daily lock reset"

    def _daily_loss_limit(self, equity: float | None) -> float:
        if equity is None or equity <= 0:
            return float("inf")
        return float(equity) * (self.config.max_daily_loss_pct / 100.0)

    def _queue_near_capacity(self, depth: int, max_depth: int) -> bool:
        if max_depth <= 0:
            return False
        return max(0, int(depth)) >= int(max_depth * self.config.queue_warning_pct)

    def _alert(self, level: str, code: str, message: str, action: str) -> BotStabilityAlert:
        self.state.alert_level = level
        self.state.alert_code = code
        self.state.last_reason = message
        self.state.supervisor_action = action
        return self.alert()
