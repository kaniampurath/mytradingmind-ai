from __future__ import annotations

from pathlib import Path


def test_bot_admin_navigation_and_command_bus_wiring_exist() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    assert '"BOT ADMIN"' in text
    assert "def bot_admin_screen" in text
    assert "RuntimeCommandBus" in text
    assert "python -m mytradingmind.runtime start-bot" in text
