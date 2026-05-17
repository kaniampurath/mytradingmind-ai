from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aegis_trader.runtime.runtime_manager import RuntimeManager


@dataclass(frozen=True)
class RuntimeCommand:
    action: str
    bot_id: str = ""
    source: str = "CLI"
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class RuntimeCommandResult:
    command_id: str
    action: str
    ok: bool
    message: str
    state: dict[str, Any] = field(default_factory=dict)


class RuntimeCommandBus:
    """Shared command bus for Bot Admin and CLI runtime actions."""

    VALID_ACTIONS = {
        "START_RUNTIME",
        "STOP_RUNTIME",
        "START_BOT",
        "STOP_BOT",
        "RESTART_BOT",
        "PAUSE_BOT",
        "RESUME_BOT",
        "RUN_VALIDATION",
        "SWITCH_MODE",
        "FLATTEN_POSITION",
        "STATUS",
    }

    def __init__(self, manager: RuntimeManager | None = None) -> None:
        self.manager = manager or RuntimeManager()
        self._seen: set[str] = set()

    def dispatch(self, command: RuntimeCommand) -> RuntimeCommandResult:
        if command.command_id in self._seen:
            return RuntimeCommandResult(command.command_id, command.action, True, "duplicate command ignored")
        self._seen.add(command.command_id)
        action = command.action.upper()
        if action not in self.VALID_ACTIONS:
            return RuntimeCommandResult(command.command_id, action, False, f"unsupported action: {action}")
        if action.endswith("_BOT") and not command.bot_id:
            return RuntimeCommandResult(command.command_id, action, False, "bot_id is required")
        try:
            state = self._execute(action, command)
            return RuntimeCommandResult(command.command_id, action, True, "ok", state)
        except Exception as exc:
            return RuntimeCommandResult(command.command_id, action, False, str(exc))

    def _execute(self, action: str, command: RuntimeCommand) -> dict[str, Any]:
        if action == "STATUS":
            return self.manager.runtime_status()
        if action == "START_RUNTIME":
            return self.manager.start_runtime(str(command.payload.get("mode", "HEADLESS")))
        if action == "STOP_RUNTIME":
            return self.manager.stop_runtime()
        if action == "START_BOT":
            return self.manager.start_bot(command.bot_id, command.source)
        if action == "STOP_BOT":
            return self.manager.stop_bot(command.bot_id, command.source)
        if action == "RESTART_BOT":
            return self.manager.restart_bot(command.bot_id, command.source)
        if action == "PAUSE_BOT":
            return self.manager.pause_bot(command.bot_id, command.source)
        if action == "RESUME_BOT":
            return self.manager.resume_bot(command.bot_id, command.source)
        if action in {"RUN_VALIDATION", "SWITCH_MODE", "FLATTEN_POSITION"}:
            return {"accepted": True, "action": action, "bot_id": command.bot_id}
        raise ValueError(f"unsupported action: {action}")
