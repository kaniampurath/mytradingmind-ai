from __future__ import annotations

from pathlib import Path


def test_existing_screen_names_remain_available() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    for screen in ["LIVE TRADING", "ORDERFLOW", "RISK", "BOT MANAGEMENT", "SYSTEM HEALTH", "JOURNAL"]:
        assert f'"{screen}"' in text
    for child in ["BOT FRAMEWORK", "BOT RUNTIME", "BOT ADMIN", "VALIDATION LAB"]:
        assert child in text


def test_live_trading_no_longer_renders_market_buckets_section() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    live_start = text.index("def live_trading")
    live_end = text.index("def bucket_board")
    live_body = text[live_start:live_end]

    assert "### Market Buckets" not in live_body
    assert "bucket_board(scan)" not in live_body
