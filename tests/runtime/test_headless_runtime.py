from __future__ import annotations

import pandas as pd
from pathlib import Path
from uuid import uuid4

from aegis_trader.runtime.bot_registry import BotRegistry
from aegis_trader.runtime.headless_service import run_runtime
from aegis_trader.runtime.runtime_manager import RuntimeManager


def workspace_tmp() -> Path:
    path = Path("tmp") / "tests" / str(uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_runtime_state_is_file_persistent() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Runtime Bot", "strategy": "ATR Trend Burst", "symbol": "ETH/USDT", "state": "BACKTESTED"}]))
    state_path = tmp_path / "runtime_state.json"

    manager = RuntimeManager(registry=registry, state_path=state_path)
    manager.start_bot("Runtime_Bot", source="TEST")

    recovered = RuntimeManager(registry=registry, state_path=state_path).list_bot_states()
    assert recovered[0]["bot_id"] == "Runtime_Bot"
    assert recovered[0]["status"] == "RUNNING"


def test_bot_registry_merge_prefers_fresh_persistent_state() -> None:
    older = pd.DataFrame(
        [
            {
                "name": "Runtime Bot",
                "bot_id": "Runtime_Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "ETH/USDT",
                "state": "BACKTESTED",
                "updated_at": "2026-05-22T00:00:00+00:00",
            }
        ]
    )
    fresher = pd.DataFrame(
        [
            {
                "name": "Runtime Bot",
                "bot_id": "Runtime_Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "ETH/USDT",
                "state": "RUNNING",
                "updated_at": "2026-05-23T00:00:00+00:00",
            }
        ]
    )

    merged = BotRegistry.merge(older, fresher)

    assert merged.iloc[0]["state"] == "RUNNING"


def test_headless_runtime_single_cycle_starts_independently() -> None:
    import asyncio

    asyncio.run(run_runtime("HEADLESS", 0.01, once=True))
    status = RuntimeManager().runtime_status()
    assert status["runtime"] == "RUNNING"
    RuntimeManager().stop_runtime()
