from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from datetime import UTC, datetime

from aegis_trader.core.config import settings
from aegis_trader.bot.stability_framework import ProductionStabilityFramework
from aegis_trader.runtime.bot_registry import BotRegistry
from aegis_trader.runtime.runtime_state import BotRuntimeState, RuntimeAuditEvent, utc_now_iso


RUNTIME_STATE_PATH = Path("reports/runtime_state.json")
RUNTIME_AUDIT_PATH = Path("reports/runtime_audit.json")
RUNTIME_CONTROL_PATH = Path("reports/runtime_control.json")
RUNTIME_ALERTS_PATH = Path("reports/runtime_alerts.json")
RUNTIME_ORDER_AUDIT_PATH = Path("reports/runtime_order_audit.json")
RUNTIME_TRADE_EVENTS_PATH = Path("reports/runtime_trade_events.json")
RUNTIME_TRADE_PNL_SNAPSHOTS_PATH = Path("reports/runtime_trade_pnl_snapshots.json")


class RuntimeManager:
    """Headless-safe runtime state manager.

    This manager performs state transitions and audit logging. It does not place
    exchange orders and does not depend on Streamlit/session state.
    """

    def __init__(self, registry: BotRegistry | None = None, state_path: Path = RUNTIME_STATE_PATH) -> None:
        self.registry = registry or BotRegistry()
        self.state_path = state_path

    def list_bot_states(self) -> list[dict[str, Any]]:
        bots = self.registry.load()
        persisted = {row.get("bot_id", row.get("name", "")): row for row in self._read_state()}
        states: list[dict[str, Any]] = []
        for _, bot in bots.iterrows():
            bot_id = str(bot["bot_id"])
            framework = ProductionStabilityFramework()
            has_position = str(bot.get("state", "")) in {"RUNNING", "DEPLOYED"}
            framework.startup_check(has_position=has_position, protection_verified=True)
            if has_position:
                framework.evaluate_supervision(last_seen=self._parse_datetime(str(bot.get("heartbeat_at", "") or "")), has_position=True, protection_verified=True)
            framework_snapshot = framework.state.to_dict()
            persisted_state = persisted.get(bot_id, {})
            started_at = str(persisted_state.get("started_at", bot.get("deployed_at", "")) or "")
            entry_price = self._runtime_entry_price(bot, persisted_state)
            row = {
                **BotRuntimeState(
                    bot_id=bot_id,
                    name=str(bot["name"]),
                    description=str(bot.get("description", "")),
                    strategy=str(bot["strategy"]),
                    symbol=str(bot["symbol"]),
                    timeframe=str(bot.get("timeframe", "1h") or "1h"),
                    status=self._status_from_bot_state(str(bot["state"])),
                    mode=str(bot.get("mode", "PAPER")),
                    runtime_mode="HEADLESS",
                    started_at=started_at,
                    pnl_started_at=started_at,
                    runtime_entry_price=entry_price,
                    pnl_since_start=float(persisted_state.get("pnl_since_start", 0.0) or 0.0),
                    pnl_since_start_pct=float(persisted_state.get("pnl_since_start_pct", 0.0) or 0.0),
                    last_heartbeat=str(bot.get("heartbeat_at", "") or ""),
                    risk_state=str(framework_snapshot.get("risk_state", "OK")),
                    framework=str(framework_snapshot.get("framework_name", "PRODUCTION_STABILITY_V2")),
                    framework_status=str(framework_snapshot.get("status", "READY")),
                    protection_state=str(framework_snapshot.get("protection_state", "UNKNOWN")),
                    restart_required=bool(framework_snapshot.get("restart_required", False)),
                    last_framework_reason=str(framework_snapshot.get("last_reason", "")),
                    supervisor_action=str(framework_snapshot.get("supervisor_action", "NONE")),
                    alert_level=str(framework_snapshot.get("alert_level", "INFO")),
                    alert_code=str(framework_snapshot.get("alert_code", "")),
                    data_state=str(framework_snapshot.get("data_state", "UNKNOWN")),
                    reconciliation_state=str(framework_snapshot.get("reconciliation_state", "NOT_RUN")),
                    portfolio_state=str(framework_snapshot.get("portfolio_state", "UNKNOWN")),
                    validation_status="BACKTESTED" if str(bot["state"]) in {"BACKTESTED", "DEPLOYED", "RUNNING", "PAUSED"} else "PENDING",
                    llm_state=self.llm_state(),
                ).to_dict(),
                **persisted_state,
            }
            states.append(row)
        return states

    def runtime_status(self) -> dict[str, Any]:
        states = self.list_bot_states()
        control = self._read_runtime_control()
        return {
            "runtime": str(control.get("runtime", "STOPPED")),
            "runtime_mode": str(control.get("runtime_mode", "HEADLESS")),
            "running_bots": sum(1 for row in states if row.get("status") == "RUNNING"),
            "failed_bots": sum(1 for row in states if row.get("status") == "ERROR"),
            "alerts": sum(1 for row in states if str(row.get("alert_level", "INFO")) in {"WARNING", "CRITICAL"}),
            "critical_alerts": sum(1 for row in states if str(row.get("alert_level", "INFO")) == "CRITICAL"),
            "restart_required": sum(1 for row in states if bool(row.get("restart_required", False))),
            "data_faults": sum(1 for row in states if str(row.get("data_state", "OK")) not in {"OK", "UNKNOWN"}),
            "reconciliation_faults": sum(1 for row in states if str(row.get("reconciliation_state", "OK")) not in {"OK", "NOT_RUN"}),
            "llm_state": self.llm_state(),
            "runtime_heartbeat": str(control.get("heartbeat_at", "")),
            "updated_at": utc_now_iso(),
        }

    def start_runtime(self, mode: str = "HEADLESS") -> dict[str, Any]:
        now = utc_now_iso()
        state = {"runtime": "RUNNING", "runtime_mode": mode.upper(), "started_at": now, "heartbeat_at": now, "updated_at": now}
        self._write_runtime_control(state)
        return state

    def stop_runtime(self) -> dict[str, Any]:
        now = utc_now_iso()
        control = self._read_runtime_control()
        state = {**control, "runtime": "STOPPED", "heartbeat_at": now, "updated_at": now}
        self._write_runtime_control(state)
        return state

    def runtime_heartbeat(self, mode: str = "HEADLESS") -> dict[str, Any]:
        control = self._read_runtime_control()
        if str(control.get("runtime", "STOPPED")) != "RUNNING":
            control = self.start_runtime(mode)
        now = utc_now_iso()
        state = {**control, "runtime": "RUNNING", "runtime_mode": mode.upper(), "heartbeat_at": now, "updated_at": now}
        self._write_runtime_control(state)
        for bot_state in self.list_bot_states():
            if str(bot_state.get("status", "")) == "RUNNING":
                evaluated = self._framework_heartbeat(bot_state)
                if str(evaluated.get("supervisor_action", "")) in {"RESTART", "HALT_AND_RESTART"}:
                    self.restart_bot(str(bot_state["bot_id"]), source="SUPERVISOR")
        return state

    def start_bot(self, bot_id: str, source: str = "CLI") -> dict[str, Any]:
        return self._transition(bot_id, "RUNNING", "START_BOT", source)

    def stop_bot(self, bot_id: str, source: str = "CLI") -> dict[str, Any]:
        return self._transition(bot_id, "STOPPED", "STOP_BOT", source)

    def pause_bot(self, bot_id: str, source: str = "CLI") -> dict[str, Any]:
        return self._transition(bot_id, "PAUSED", "PAUSE_BOT", source)

    def resume_bot(self, bot_id: str, source: str = "CLI") -> dict[str, Any]:
        return self._transition(bot_id, "RUNNING", "RESUME_BOT", source)

    def restart_bot(self, bot_id: str, source: str = "CLI") -> dict[str, Any]:
        self._transition(bot_id, "STOPPED", "RESTART_BOT", source)
        return self._transition(bot_id, "RUNNING", "RESTART_BOT", source)

    @staticmethod
    def llm_state() -> str:
        llm_mode = str(getattr(settings, "llm_mode", "rules"))
        llm_enabled = bool(getattr(settings, "llm_enabled", False))
        if llm_mode.lower() == "rules" or not llm_enabled:
            return "RULE_BASED"
        return "OPENAI_READY" if bool(__import__("os").environ.get("OPENAI_API_KEY")) else "RULE_BASED"

    def _transition(self, bot_id: str, new_status: str, action: str, source: str) -> dict[str, Any]:
        states = {row["bot_id"]: row for row in self.list_bot_states()}
        current = states.get(bot_id)
        if current is None:
            current = next((row for row in states.values() if row.get("name") == bot_id), None)
        if current is None:
            raise KeyError(f"Unknown bot: {bot_id}")
        previous = str(current.get("status", "STOPPED"))
        if previous == new_status:
            result = "NOOP"
        else:
            result = "OK"
        now = utc_now_iso()
        current.update(
            {
                "status": new_status,
                "last_heartbeat": now,
                "updated_at": now,
                "runtime_mode": "HEADLESS",
                "llm_state": self.llm_state(),
            }
        )
        if new_status == "RUNNING":
            current["started_at"] = now
            current["pnl_started_at"] = now
            current["pnl_since_start"] = 0.0
            current["pnl_since_start_pct"] = 0.0
            current.update(self._framework_ready_state(now))
            self.record_order_lifecycle(
                bot_id=str(current["bot_id"]),
                client_order_id=f"{current['bot_id']}:{now}:START",
                symbol=str(current.get("symbol", "")),
                side="BOT",
                status="STARTED",
                quantity=0.0,
                price=float(current.get("runtime_entry_price", 0.0) or 0.0),
                reason=f"{action} via {source}",
            )
        self._upsert_state(current)
        bot_state = "RUNNING" if new_status == "RUNNING" else "PAUSED" if new_status == "PAUSED" else "STOPPED"
        self.registry.update_state(str(current["bot_id"]), bot_state, f"{action} via runtime_manager")
        self._audit(RuntimeAuditEvent(now, source, str(current["bot_id"]), action, previous, new_status, result))
        return current

    def reconcile_bot(
        self,
        bot_id: str,
        *,
        exchange_position_qty: float,
        local_position_qty: float,
        protection_verified: bool,
        open_orders_verified: bool,
        source: str = "RECONCILIATION",
    ) -> dict[str, Any]:
        states = {row["bot_id"]: row for row in self.list_bot_states()}
        current = states.get(bot_id)
        if current is None:
            raise KeyError(f"Unknown bot: {bot_id}")
        framework = ProductionStabilityFramework()
        plan = framework.restart_reconciliation_plan(
            exchange_position_qty=exchange_position_qty,
            local_position_qty=local_position_qty,
            protection_verified=protection_verified,
            open_orders_verified=open_orders_verified,
        )
        snapshot = framework.state.to_dict()
        current.update(
            {
                "reconciliation_state": str(snapshot.get("reconciliation_state", "NOT_RUN")),
                "framework_status": str(snapshot.get("status", current.get("framework_status", "READY"))),
                "restart_required": bool(snapshot.get("restart_required", False)),
                "supervisor_action": str(snapshot.get("supervisor_action", plan.action)),
                "alert_level": str(snapshot.get("alert_level", "INFO")),
                "alert_code": str(snapshot.get("alert_code", "")),
                "last_framework_reason": plan.reason,
                "updated_at": utc_now_iso(),
            }
        )
        self._upsert_state(current)
        self._audit(RuntimeAuditEvent(utc_now_iso(), source, bot_id, "RECONCILE_BOT", str(current.get("status", "")), str(current.get("status", "")), plan.action, plan.reason))
        return {**current, "reconciliation_plan": plan.to_dict()}

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
    ) -> dict[str, Any]:
        event = ProductionStabilityFramework().record_order_lifecycle(
            bot_id=bot_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            status=status,
            quantity=quantity,
            price=price,
            reason=reason,
        ).to_dict()
        self._append_json_row(RUNTIME_ORDER_AUDIT_PATH, event, limit=5_000)
        self.record_trade_event(
            bot_id=bot_id,
            trade_id=self._trade_id(bot_id, symbol, client_order_id),
            event_type=self._trade_event_type(status),
            symbol=symbol,
            order_state=status,
            position_state="OPEN" if float(quantity) > 0 and str(status).upper() not in {"CANCELLED", "CANCELED", "REJECTED", "FAILED", "CLOSED"} else "FLAT",
            lifecycle_state=self._trade_lifecycle_state(status),
            price=price,
            quantity=quantity,
            reason=reason,
            source_order_id=client_order_id,
        )
        return event

    def record_trade_event(
        self,
        *,
        bot_id: str,
        trade_id: str,
        event_type: str,
        symbol: str,
        order_state: str,
        position_state: str,
        lifecycle_state: str,
        price: float,
        quantity: float,
        reason: str = "",
        source_order_id: str = "",
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": f"{trade_id}:{event_type}:{utc_now_iso()}",
            "event_time": utc_now_iso(),
            "trade_id": trade_id,
            "bot_id": bot_id,
            "symbol": symbol,
            "event_type": event_type,
            "order_state": order_state,
            "position_state": position_state,
            "lifecycle_state": lifecycle_state,
            "price": float(price),
            "quantity": float(quantity),
            "reason": reason,
            "source_order_id": source_order_id,
            "metrics": metrics or {},
        }
        self._append_json_row(RUNTIME_TRADE_EVENTS_PATH, event, limit=10_000)
        return event

    def record_trade_pnl_snapshot(
        self,
        *,
        bot_id: str,
        trade_id: str,
        symbol: str,
        current_price: float,
        unrealized_pnl: float,
        realized_pnl: float,
        roi_pct: float,
        exposure: float,
        drawdown_pct: float,
        lifecycle_state: str,
    ) -> dict[str, Any]:
        snapshot = {
            "snapshot_id": f"{trade_id}:{utc_now_iso()}",
            "snapshot_time": utc_now_iso(),
            "trade_id": trade_id,
            "bot_id": bot_id,
            "symbol": symbol,
            "current_price": float(current_price),
            "unrealized_pnl": float(unrealized_pnl),
            "realized_pnl": float(realized_pnl),
            "roi_pct": float(roi_pct),
            "exposure": float(exposure),
            "drawdown_pct": float(drawdown_pct),
            "lifecycle_state": lifecycle_state,
        }
        self._append_json_row(RUNTIME_TRADE_PNL_SNAPSHOTS_PATH, snapshot, limit=10_000)
        return snapshot

    def _read_state(self) -> list[dict[str, Any]]:
        if not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _read_runtime_control(self) -> dict[str, Any]:
        if not RUNTIME_CONTROL_PATH.exists():
            return {}
        try:
            data = json.loads(RUNTIME_CONTROL_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _write_runtime_control(self, state: dict[str, Any]) -> None:
        RUNTIME_CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_CONTROL_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    def _upsert_state(self, state: dict[str, Any]) -> None:
        rows = self._read_state()
        rows = [row for row in rows if row.get("bot_id") != state.get("bot_id")]
        rows.append(state)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")

    def _audit(self, event: RuntimeAuditEvent) -> None:
        rows: list[dict[str, Any]] = []
        if RUNTIME_AUDIT_PATH.exists():
            try:
                data = json.loads(RUNTIME_AUDIT_PATH.read_text(encoding="utf-8"))
                rows = data if isinstance(data, list) else []
            except json.JSONDecodeError:
                rows = []
        rows.append(event.to_dict())
        RUNTIME_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_AUDIT_PATH.write_text(json.dumps(rows[-500:], indent=2), encoding="utf-8")

    @staticmethod
    def _status_from_bot_state(state: str) -> str:
        mapping = {"RUNNING": "RUNNING", "DEPLOYED": "RUNNING", "PAUSED": "PAUSED", "FAILED": "ERROR", "STOPPED": "STOPPED"}
        return mapping.get(state, "STOPPED")

    @staticmethod
    def _risk_locked() -> bool:
        return False

    @staticmethod
    def _trade_id(bot_id: str, symbol: str, client_order_id: str) -> str:
        normalized = symbol.replace("/", "").replace(":", "")
        return f"{bot_id}:{normalized}:{client_order_id}"

    @staticmethod
    def _trade_event_type(status: str) -> str:
        normalized = status.upper()
        if normalized in {"STARTED", "CREATED", "VALIDATED"}:
            return "TradeCreated"
        if normalized in {"ACKNOWLEDGED", "SUBMITTED", "FILLED"}:
            return "TradeEntered"
        if normalized in {"PARTIALLY_FILLED", "PARTIAL"}:
            return "TradeUpdated"
        if normalized in {"STOP_TRIGGERED", "STOPPED"}:
            return "StopTriggered"
        if normalized in {"RISK_TRIGGERED", "RISK_BLOCKED"}:
            return "RiskTriggered"
        if normalized in {"CLOSED", "EXITED", "CANCELLED", "CANCELED"}:
            return "TradeExited"
        if normalized in {"REJECTED", "FAILED", "ERROR"}:
            return "RuntimeAlertGenerated"
        return "TradeUpdated"

    @staticmethod
    def _trade_lifecycle_state(status: str) -> str:
        normalized = status.upper()
        if normalized in {"STARTED", "CREATED", "VALIDATED"}:
            return "Pending"
        if normalized in {"ACKNOWLEDGED", "SUBMITTED"}:
            return "Submitted"
        if normalized in {"PARTIALLY_FILLED", "PARTIAL"}:
            return "Partially Filled"
        if normalized == "FILLED":
            return "Filled"
        if normalized in {"PARTIALLY_EXITED", "PARTIAL_EXIT"}:
            return "Partially Exited"
        if normalized in {"CLOSED", "EXITED", "STOP_TRIGGERED", "STOPPED"}:
            return "Closed"
        if normalized in {"CANCELLED", "CANCELED"}:
            return "Cancelled"
        if normalized in {"REJECTED", "FAILED", "ERROR"}:
            return "Failed"
        return "Active"

    @staticmethod
    def _runtime_entry_price(bot: Any, persisted: dict[str, Any]) -> float:
        if persisted.get("runtime_entry_price"):
            return float(persisted.get("runtime_entry_price") or 0.0)
        parameters = bot.get("parameters", {}) if hasattr(bot, "get") else {}
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError:
                parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        return float(parameters.get("runtime_entry_price", 0.0) or 0.0)

    @staticmethod
    def _framework_ready_state(now: str) -> dict[str, Any]:
        framework = ProductionStabilityFramework()
        framework.startup_check(has_position=False, protection_verified=False)
        framework.heartbeat()
        state = framework.state.to_dict()
        return {
            "framework": str(state.get("framework_name", "PRODUCTION_STABILITY_V2")),
            "framework_status": str(state.get("status", "READY")),
            "protection_state": str(state.get("protection_state", "UNKNOWN")),
            "restart_required": bool(state.get("restart_required", False)),
            "last_framework_reason": str(state.get("last_reason", "")),
            "supervisor_action": str(state.get("supervisor_action", "NONE")),
            "alert_level": str(state.get("alert_level", "INFO")),
            "alert_code": str(state.get("alert_code", "")),
            "data_state": str(state.get("data_state", "UNKNOWN")),
            "reconciliation_state": str(state.get("reconciliation_state", "NOT_RUN")),
            "portfolio_state": str(state.get("portfolio_state", "UNKNOWN")),
            "risk_state": str(state.get("risk_state", "OK")),
            "last_heartbeat": now,
        }

    def _framework_heartbeat(self, state: dict[str, Any]) -> dict[str, Any]:
        framework = ProductionStabilityFramework()
        last_seen = self._parse_datetime(str(state.get("last_heartbeat", "") or ""))
        alert = framework.evaluate_supervision(last_seen=last_seen)
        snapshot = framework.heartbeat()
        state.update(
            {
                "framework": str(snapshot.get("framework_name", "PRODUCTION_STABILITY_V2")),
                "framework_status": str(snapshot.get("status", "READY")),
                "protection_state": str(snapshot.get("protection_state", state.get("protection_state", "UNKNOWN"))),
                "restart_required": bool(snapshot.get("restart_required", False)),
                "last_framework_reason": str(snapshot.get("last_reason", "framework ready")),
                "supervisor_action": str(snapshot.get("supervisor_action", "NONE")),
                "alert_level": str(snapshot.get("alert_level", "INFO")),
                "alert_code": str(snapshot.get("alert_code", "")),
                "data_state": str(snapshot.get("data_state", "UNKNOWN")),
                "reconciliation_state": str(snapshot.get("reconciliation_state", state.get("reconciliation_state", "NOT_RUN"))),
                "portfolio_state": str(snapshot.get("portfolio_state", "UNKNOWN")),
                "risk_state": str(snapshot.get("risk_state", "OK")),
                "last_heartbeat": str(snapshot.get("last_heartbeat", utc_now_iso())),
                "updated_at": utc_now_iso(),
            }
        )
        self._upsert_state(state)
        if alert.level in {"WARNING", "CRITICAL"}:
            self._alert(str(state.get("bot_id", "")), alert.to_dict())
        return state

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _alert(self, bot_id: str, alert: dict[str, Any]) -> None:
        self._append_json_row(RUNTIME_ALERTS_PATH, {"bot_id": bot_id, **alert}, limit=1_000)

    @staticmethod
    def _append_json_row(path: Path, row: dict[str, Any], *, limit: int) -> None:
        rows: list[dict[str, Any]] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data if isinstance(data, list) else []
            except json.JSONDecodeError:
                rows = []
        rows.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows[-limit:], indent=2, default=str), encoding="utf-8")
