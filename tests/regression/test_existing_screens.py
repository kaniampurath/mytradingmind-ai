from __future__ import annotations

from pathlib import Path


def test_existing_screen_names_remain_available() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    for screen in ["LIVE TRADING", "ORDERFLOW", "RISK", "BOT FRAMEWORK", "BOT RUNTIME", "SYSTEM HEALTH", "JOURNAL", "VALIDATION LAB"]:
        assert f'"{screen}"' in text
