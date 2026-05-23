from __future__ import annotations

from pathlib import Path


def test_bot_admin_navigation_and_command_bus_wiring_exist() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    assert '"BOT ADMIN"' in text
    assert "def bot_admin_screen" in text
    assert "RuntimeCommandBus" in text
    assert "python -m mytradingmind.runtime start-bot" in text
    assert 'st.session_state["screen"]' not in text
    assert "nav-screen-" in text
    assert "strategy_default_timeframe" in text
    assert "stream_timeframe_bar_for_bot" in text
    assert "START HEADLESS RUNTIME" in text
    assert "launch_headless_runtime_process" in text
    assert "python scripts/run_headless_runtime.py --mode headless" in text
    assert "framework_status" in text
    assert "protection_state" in text
    assert "last_framework_reason" in text
    assert "Bot Marketplace Readiness" in text
    assert "Default Stop" in text
    assert "Runtime Compatibility" in text


def test_bot_runtime_tiles_live_update_pnl() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert "def runtime_tile_live_mark_component" in text
    assert "def deploy_bot_from_tile" in text
    assert "def runtime_bot_rankings" in text
    assert "def runtime_marketplace_header" in text
    assert "def runtime_discovery_panel" in text
    assert "def runtime_bot_filter_bar" in text
    assert "def validate_bot_from_tile" in text
    assert "latest_scan_fallback" in text
    assert "Dormant strategy bots cannot be validated" in text
    assert "Active Strategies" in text
    assert "Daily Picks" in text
    assert "Hot Coin Leaderboard" in text
    assert "runtime_score" in text
    assert "socket_seen_at" in text
    assert "row.last_price = Number(data.p)" in text
    assert "def runtime_instance_profile" in text
    assert "Real-time P&L" in text
    assert "Unrealized / Realized" in text
    assert "Instance details: context, capital, stops, health" in text
    assert "PnL since start" in text
    assert "runtime-bot-boundary" in text
    assert "price unavailable" in text
    assert "Number.isFinite" in text
    assert 'return "$0.00"' in text
    assert "health_light" in text
    assert "operational_guidance" in text
    assert "tile-deploy-" in text
    assert "tile-stop-" in text
    assert "tile-remove-" in text
    assert "Runtime Controls" not in text
    assert "def runtime_live_metrics_component" not in text


def test_bot_admin_can_remove_stopped_bot_definitions() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert "def remove_bot_definition" in text
    assert "REMOVE BOT DEFINITION" in text
    assert "Type the bot name to confirm" in text
    assert "Remove definition" in text
    assert 'state in {"RUNNING", "DEPLOYED"}' in text
