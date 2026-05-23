from __future__ import annotations

from pathlib import Path


ROOT = Path("scripts")


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_runtime_shell_scripts_exist_for_headless_operations() -> None:
    for name in ["runtime_start.sh", "runtime_stop.sh", "runtime_monitor.sh"]:
        text = read_script(name)
        assert text.startswith("#!/usr/bin/env sh")
        assert "streamlit" not in text.lower()
        assert "dashboard" not in text.lower()


def test_runtime_start_script_runs_headless_cli_in_background() -> None:
    text = read_script("runtime_start.sh")

    assert "nohup" in text
    assert "-m mytradingmind.runtime start" in text
    assert "--mode \"$MODE\"" in text
    assert "reports/headless_runtime.pid" in text
    assert "logs/headless_runtime.out" in text
    assert "-m mytradingmind.runtime status" in text


def test_runtime_stop_script_uses_graceful_runtime_control() -> None:
    text = read_script("runtime_stop.sh")

    assert "-m mytradingmind.runtime stop" in text
    assert "-m mytradingmind.runtime status" in text
    assert "rm -f \"$PID_FILE\"" in text


def test_runtime_monitor_script_supports_one_shot_and_loop_modes() -> None:
    text = read_script("runtime_monitor.sh")

    assert "MONITOR_INTERVAL_SECONDS" in text
    assert "--once" in text
    assert "-m mytradingmind.runtime status" in text
    assert "while true" in text
