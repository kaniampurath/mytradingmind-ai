from __future__ import annotations

from pathlib import Path


def test_bot_admin_navigation_and_command_bus_wiring_exist() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    assert '"BOT ADMIN"' in text
    assert "def bot_admin_screen" in text
    assert "RuntimeCommandBus" in text
    assert "python -m mytradingmind.runtime start-bot" in text
    assert 'st.session_state["screen"]' not in text
    assert "screen_selector" in text
    assert "strategy_default_timeframe" in text
    assert "stream_timeframe_bar_for_bot" in text
    assert "START HEADLESS RUNTIME" in text
    assert "launch_headless_runtime_process" in text
    assert "python scripts/run_headless_runtime.py --mode headless" in text
