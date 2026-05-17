from __future__ import annotations

from pathlib import Path


def test_institutional_check_script_exists() -> None:
    path = Path("scripts/institutional_check.py")

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "docs/UBUNTU_DROPLET_DEPLOYMENT.md" in text
    assert "myts_bot_table_" in text
