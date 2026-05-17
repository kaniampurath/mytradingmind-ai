from __future__ import annotations

from pathlib import Path


def test_dashboard_launcher_exists() -> None:
    launcher = Path("scripts/start_dashboard.py")

    assert launcher.exists()
    text = launcher.read_text(encoding="utf-8")
    assert "streamlit_stdout.log" in text
    assert "streamlit_stderr.log" in text
    assert "aegis_trader/dashboards/app.py" in text
