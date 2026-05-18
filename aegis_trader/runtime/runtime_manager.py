from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis_trader.core.config import settings
from aegis_trader.runtime.bot_registry import BotRegistry
from aegis_trader.runtime.runtime_state import BotRuntimeState, RuntimeAuditEvent, utc_now_iso


RUNTIME_STATE_PATH = Path("reports/runtime_state.json")
RUNTIME_AUDIT_PATH = Path("reports/runtime_audit.json")
RUNTIME_CONTROL_PATH = Path("reports/runtime_control.json")


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
                    last_heartbeat=str(bot.get("heartbeat_at", "") or ""),
                    risk_state="RISK_LOCKED" if self._risk_locked() else "OK",
                    validation_status="BACKTESTED" if str(bot["state"]) in {"BACKTESTED", "DEPLOYED", "RUNNING", "PAUSED"} else "PENDING",
                    llm_state=self.llm_state(),
                ).to_dict(),
                **persisted.get(bot_id, {}),
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
        self._upsert_state(current)
        bot_state = "RUNNING" if new_status == "RUNNING" else "PAUSED" if new_status == "PAUSED" else "STOPPED"
        self.registry.update_state(str(current["bot_id"]), bot_state, f"{action} via runtime_manager")
        self._audit(RuntimeAuditEvent(now, source, str(current["bot_id"]), action, previous, new_status, result))
        return current

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
