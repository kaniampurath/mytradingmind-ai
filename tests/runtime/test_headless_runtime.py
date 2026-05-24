from __future__ import annotations

import subprocess
import sys

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
    assert status["runtime"] == "STOPPED"
    assert status["runtime_mode"] == "HEADLESS"


def test_headless_runtime_imports_without_dashboard_or_streamlit() -> None:
    code = (
        "import sys;"
        "import aegis_trader.runtime.headless_service;"
        "blocked=[name for name in sys.modules if name == 'streamlit' or name.startswith('aegis_trader.dashboards')];"
        "assert not blocked, blocked;"
        "print('headless_import_clean')"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr or result.stdout
    assert "headless_import_clean" in result.stdout


def test_mytradingmind_runtime_cli_alias_is_headless_safe() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mytradingmind.runtime", "status"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert '"action": "STATUS"' in result.stdout
    assert '"runtime_mode": "HEADLESS"' in result.stdout


def test_stopped_runtime_does_not_report_active_running_bots() -> None:
    tmp_path = workspace_tmp()
    registry = BotRegistry(tmp_path / "bots.json")
    registry.save(pd.DataFrame([{"name": "Stopped Runtime Bot", "strategy": "ATR Trend Burst", "symbol": "BTC/USDT", "state": "RUNNING"}]))
    manager = RuntimeManager(registry=registry, state_path=tmp_path / "runtime_state.json")

    manager.stop_runtime()
    status = manager.runtime_status()

    assert status["runtime"] == "STOPPED"
    assert status["running_bots"] == 0
    assert status["configured_running_bots"] == 1
    assert status["runtime_state_consistency"] == "STOPPED_WITH_RUNNING_BOT_DEFINITIONS"
