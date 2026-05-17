from __future__ import annotations

import pandas as pd
from pathlib import Path
from uuid import uuid4

from aegis_trader.runtime.bot_registry import BotRegistry
from aegis_trader.runtime.command_bus import RuntimeCommand, RuntimeCommandBus
from aegis_trader.runtime.runtime_manager import RuntimeManager


def workspace_tmp() -> Path:
    path = Path("tmp") / "tests" / str(uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_command_bus_starts_and_pauses_bot() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Alpha Bot", "strategy": "Existing Momentum", "symbol": "ADA/USDT", "state": "BACKTESTED"}]))
    manager = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json")
    bus = RuntimeCommandBus(manager)

    started = bus.dispatch(RuntimeCommand("START_BOT", bot_id="Alpha_Bot", source="TEST"))
    assert started.ok
    assert started.state["status"] == "RUNNING"

    paused = bus.dispatch(RuntimeCommand("PAUSE_BOT", bot_id="Alpha_Bot", source="TEST"))
    assert paused.ok
    assert paused.state["status"] == "PAUSED"


def test_command_bus_rejects_unknown_action() -> None:
    result = RuntimeCommandBus().dispatch(RuntimeCommand("MAKE_ORDER"))
    assert not result.ok
    assert "unsupported" in result.message


def test_command_bus_fails_closed_for_unknown_flatten_bot() -> None:
    result = RuntimeCommandBus().dispatch(RuntimeCommand("FLATTEN_POSITION", bot_id="missing-bot"))
    assert not result.ok
    assert "Unknown bot" in result.message
