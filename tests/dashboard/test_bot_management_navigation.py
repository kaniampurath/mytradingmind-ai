from __future__ import annotations

from pathlib import Path

from aegis_trader.dashboards.app import (
    BOT_MANAGEMENT_ROUTES,
    bot_management_path_from_url,
    resolve_screen_request,
)


def test_bot_management_is_root_navigation_parent() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    screen_options = text[text.index("SCREEN_OPTIONS = [") : text.index("def resolve_screen_request")]

    assert '"BOT MANAGEMENT"' in screen_options
    assert '"BOT FRAMEWORK"' not in screen_options
    assert '"BOT RUNTIME"' not in screen_options
    assert '"BOT ADMIN"' not in screen_options
    assert '"VALIDATION LAB"' not in screen_options


def test_legacy_bot_screen_requests_resolve_to_bot_management_children() -> None:
    assert resolve_screen_request("BOT FRAMEWORK") == ("BOT MANAGEMENT", "Framework")
    assert resolve_screen_request("BOT RUNTIME") == ("BOT MANAGEMENT", "Runtime")
    assert resolve_screen_request("BOT ADMIN") == ("BOT MANAGEMENT", "Admin")
    assert resolve_screen_request("VALIDATION LAB") == ("BOT MANAGEMENT", "Validation Lab")


def test_bot_management_route_mapping_supports_deep_links() -> None:
    assert BOT_MANAGEMENT_ROUTES["Framework"] == "/bot-management/framework"
    assert resolve_screen_request("", "/bot-management/framework") == ("BOT MANAGEMENT", "Framework")
    assert resolve_screen_request("", "/bot-management/runtime") == ("BOT MANAGEMENT", "Runtime")
    assert resolve_screen_request("", "/bot-management/admin") == ("BOT MANAGEMENT", "Admin")
    assert resolve_screen_request("", "/bot-management/validation-lab") == ("BOT MANAGEMENT", "Validation Lab")


def test_bot_management_child_and_path_requests_are_resolved() -> None:
    assert resolve_screen_request("BOT MANAGEMENT") == ("BOT MANAGEMENT", "")
    assert resolve_screen_request("BOT MANAGEMENT", requested_bot_child="Admin") == ("BOT MANAGEMENT", "Admin")
    assert bot_management_path_from_url("http://localhost:8501/bot-management/runtime") == "/bot-management/runtime"
    assert resolve_screen_request("", current_url="http://localhost:8501/bot-management/runtime") == (
        "BOT MANAGEMENT",
        "Runtime",
    )


def test_bot_management_uses_drill_down_controls_not_radio_selector() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    assert "def bot_management_landing" in text
    assert "nav-screen-" in text
    assert "nav-bot-management-child-" in text
    assert "open_bot_management_child(child)" in text
    assert "bot_management_nav_expanded" in text
    assert "toggle_bot_management_nav" in text
    assert 'st.radio(\n        "Screen"' not in text
    assert "st.segmented_control" not in text[text.index("def bot_management_screen") : text.index("with st.sidebar:")]
    assert "Bot Management child screen" not in text[text.index("def bot_management_screen") : text.index("with st.sidebar:")]


def test_dashboard_is_global_visibility_center_and_sidebar_has_no_navigation_caption() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    dashboard_body = text[text.index("def dashboard_screen") : text.index("def strategy_backtest_ranking")]
    banner_body = text[text.index("def app_banner") : text.index("def public_login_landing")]
    sidebar_body = text[text.index("with st.sidebar:") : text.index("app_banner()", text.index("with st.sidebar:"))]

    assert "mytradingmind.ai" in banner_body
    assert "Global trading operations dashboard" in banner_body
    assert "app-emblem" in banner_body
    assert "Live Trading" in dashboard_body
    assert "Signal Flow" in dashboard_body
    assert "Risk Exposure Summary" in dashboard_body
    assert 'st.caption("Navigation")' not in sidebar_body


def test_sidebar_is_forced_visible_for_authenticated_navigation() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    css_body = text[text.index("CSS = ") : text.index("st.markdown(CSS")]
    sidebar_body = text[text.index("with st.sidebar:") : text.index("app_banner()", text.index("with st.sidebar:"))]

    assert 'section[data-testid="stSidebar"][aria-expanded="false"]' in css_body
    assert "transform: translateX(0) !important" in css_body
    assert "visibility: visible !important" in css_body
    assert "z-index: 999980" in css_body
    assert "sidebar-panel-title\">Operations" in sidebar_body
    assert "Signed in:" in sidebar_body
    assert "allowed_screen_options_for_context(context)" in sidebar_body
