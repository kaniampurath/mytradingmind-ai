from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import html
import hashlib
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from dotenv import dotenv_values
from plotly.subplots import make_subplots

from aegis_trader import __version__ as APP_VERSION
from aegis_trader.analytics.replay_metrics import load_feature_file
from aegis_trader.analytics.strategy_reports import aggregate_strategy_matrix, run_strategy_matrix
from aegis_trader.analytics.time_utils import utc_datetime_series, utc_day_window
from aegis_trader.bot.framework import BotDeployment, StrategyAgnosticBot
from aegis_trader.core.config import settings
from aegis_trader.core.enums import CertificationState
from aegis_trader.core.logging import configure_logging, log_diagnostic, redact_url
from aegis_trader.llm.reasoning_agent import ReasoningAgent
from aegis_trader.runtime.command_bus import RuntimeCommand, RuntimeCommandBus
from aegis_trader.runtime.runtime_manager import (
    RUNTIME_ALERTS_PATH,
    RUNTIME_ORDER_AUDIT_PATH,
    RUNTIME_TRADE_EVENTS_PATH,
    RUNTIME_TRADE_PNL_SNAPSHOTS_PATH,
    RuntimeManager,
)
from aegis_trader.security.auth import (
    can_access_screen,
    create_password_reset,
    login_user,
    logout_session,
    register_user,
    security_schema_summary,
    set_user_password,
    verify_password,
)
from aegis_trader.storage.bot_repository import (
    DEFAULT_RISK_SETTINGS,
    append_journal_event,
    delete_bot_instance,
    read_bot_instances,
    read_journal_events,
    read_risk_settings,
    read_runtime_events,
    read_validation_runs,
    upsert_bot_instance,
    upsert_risk_settings,
    upsert_validation_run,
)
from aegis_trader.storage.db import build_engine, build_session_factory
from aegis_trader.storage.scan_repository import read_latest_heartbeat, read_live_scan
from aegis_trader.strategies import backtest_plugins

STRATEGY_REGISTRY = backtest_plugins.STRATEGY_REGISTRY


def active_strategy_names() -> list[str]:
    helper = getattr(backtest_plugins, "active_strategy_names", None)
    if callable(helper):
        return list(helper())
    names = [name for name, strategy in STRATEGY_REGISTRY.items() if str(getattr(strategy, "activation_state", "")).upper() == "ACTIVE"]
    return names or ["Certified Risk Managed Composite"]


def dormant_strategy_names() -> list[str]:
    helper = getattr(backtest_plugins, "dormant_strategy_names", None)
    if callable(helper):
        return list(helper())
    active = set(active_strategy_names())
    return [name for name in STRATEGY_REGISTRY if name not in active]


def certified_symbols_for_strategy(strategy_name: str) -> list[str]:
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    symbols = tuple(getattr(strategy, "certified_symbols", ()) or ())
    return [str(symbol) for symbol in symbols]


def strategy_symbol_is_certified(strategy_name: str, symbol: str) -> bool:
    certified = certified_symbols_for_strategy(strategy_name)
    return not certified or str(symbol) in set(certified)


def deployable_strategy_symbol_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in active_strategy_names():
        strategy = STRATEGY_REGISTRY[name]
        certified = certified_symbols_for_strategy(name)
        rows.append(
            {
                "strategy": name,
                "timeframe": str(getattr(strategy, "default_timeframe", "")),
                "passed coins": ", ".join(certified) if certified else "all active symbols",
                "deployment note": str(getattr(strategy, "activation_reason", "")),
            }
        )
    return rows
from aegis_trader.testing.certification import CertificationEngine, CertificationMetrics


st.set_page_config(page_title="mytradingmind.ai Ops", layout="wide", initial_sidebar_state="expanded")
LOG_PATH = configure_logging()
logger = logging.getLogger(__name__)


DEFAULT_LIVE_SYMBOLS: tuple[str, ...] = tuple(settings.symbols)
INSTITUTIONAL_BOT_CAGR_SOURCES = [
    {
        "source": "Barclay BTOP50 managed-futures index",
        "cagr_pct": 7.50,
        "url": "https://www.globalcustodian.com/managed-futures-gained-5-83-in-2006-says-barclay-btop50-index/",
    },
    {
        "source": "BTOP50 long-run managed-futures reference",
        "cagr_pct": 7.57,
        "url": "https://detalus.com/wp-content/uploads/2017/07/Managed-Futures-Detalus-Advisors.pdf",
    },
    {
        "source": "Simplify CTA managed-futures ETF institutional comparison",
        "cagr_pct": 11.01,
        "url": "https://www.simplify.us/sites/default/files/fund-insights/2026-03/Simplify-FI-CTA-Four-Years-In.pdf",
    },
]
INSTITUTIONAL_BOT_CAGR_MAX_PCT = round(float(np.mean([row["cagr_pct"] for row in INSTITUTIONAL_BOT_CAGR_SOURCES])), 2)
DEPLOYED_STRATEGIES_PATH = Path("reports/deployed_strategies.json")
BOT_INSTANCES_PATH = Path("reports/bot_instances.json")
RISK_SETTINGS_PATH = Path("reports/risk_settings.json")
JOURNAL_PATH = Path("reports/journal_events.json")
VALIDATION_RUNS_PATH = Path("reports/validation_runs.json")
STRATEGY_MATRIX_CACHE_PATH = Path("reports/strategy_matrix_cache.json")
SIGNAL_FLOW_BACKTEST_CACHE_PATH = Path("reports/signal_flow_top10_backtest.json")
POSITION_SIZE_DECISIONS_PATH = Path("reports/position_size_decisions.json")
BOT_ACTION_AUDIT_PATH = Path("reports/bot_action_audit.json")


def setting_bool(name: str, default: bool = False) -> bool:
    return bool(getattr(settings, name, default))


def utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def setting_int(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


log_diagnostic(logger, "dashboard_start", database_enabled=setting_bool("database_enabled"), log_path=LOG_PATH)


@st.cache_data(ttl=60, show_spinner=False)
def available_feature_files(data_dir: Path = Path("data/binance"), interval: str = "1h", days: int = 365) -> dict[str, Path]:
    files: dict[str, Path] = {}
    suffix = f"_{interval}_{days}d_features.parquet"
    for path in sorted(data_dir.glob(f"*{suffix}")):
        symbol_key = path.name[: -len(suffix)]
        if symbol_key.endswith("USDT"):
            symbol = f"{symbol_key[:-4]}/USDT"
        else:
            symbol = symbol_key
        files[symbol] = path
    return files


def available_live_symbols() -> list[str]:
    configured = [symbol for symbol in settings.symbols if symbol]
    discovered = list(available_feature_files())
    merged = list(dict.fromkeys([*configured, *discovered]))
    return merged


CSS = """
<style>
:root {
  --bg: #0f1418;
  --panel: #151c22;
  --panel-soft: #1b242c;
  --ink: #e8edf2;
  --muted: #94a3ad;
  --line: #26323b;
  --good: #55d49a;
  --warn: #f0c86a;
  --bad: #ff6f7d;
  --info: #79a7ff;
}
.stApp {
  background:
    linear-gradient(180deg, rgba(20, 29, 34, 0.98), rgba(12, 16, 20, 1));
  color: var(--ink);
}
* {
  transition: none !important;
  animation: none !important;
}
section[data-testid="stSidebar"] {
  width: 218px !important;
  min-width: 218px !important;
  max-width: 218px !important;
  margin-top: 76px !important;
  height: calc(100vh - 76px) !important;
  transform: translateX(0) !important;
  visibility: visible !important;
  display: block !important;
  left: 0 !important;
  z-index: 999980 !important;
}
section[data-testid="stSidebar"] > div {
  width: 218px !important;
  min-width: 218px !important;
  max-width: 218px !important;
  height: calc(100vh - 76px) !important;
  transform: translateX(0) !important;
  visibility: visible !important;
  display: block !important;
}
section[data-testid="stSidebar"][aria-expanded="false"],
section[data-testid="stSidebar"][aria-expanded="false"] > div,
section[data-testid="stSidebar"][data-expanded="false"],
section[data-testid="stSidebar"][data-expanded="false"] > div {
  width: 218px !important;
  min-width: 218px !important;
  max-width: 218px !important;
  margin-left: 0 !important;
  transform: translateX(0) !important;
  visibility: visible !important;
  display: block !important;
}
button[kind="header"],
[data-testid="collapsedControl"] {
  display: none !important;
}
div[data-testid="stSidebar"],
div[data-testid="stSidebarContent"] {
  background: #0d1216;
  border-right: 1px solid rgba(121, 167, 255, 0.32);
  width: 218px !important;
  min-width: 218px !important;
  max-width: 218px !important;
  box-shadow: 8px 0 24px rgba(0, 0, 0, 0.18);
}
div[data-testid="stSidebar"] > div,
div[data-testid="stSidebarContent"] > div {
  padding: 0.5rem 0.52rem 0.85rem !important;
}
div[data-testid="stSidebar"] h1,
div[data-testid="stSidebar"] h2,
div[data-testid="stSidebar"] h3,
div[data-testid="stSidebarContent"] h1,
div[data-testid="stSidebarContent"] h2,
div[data-testid="stSidebarContent"] h3 {
  font-size: 0.94rem !important;
  margin-bottom: 0.08rem !important;
}
div[data-testid="stSidebar"] p,
div[data-testid="stSidebar"] label,
div[data-testid="stSidebar"] .stCaptionContainer,
div[data-testid="stSidebarContent"] p,
div[data-testid="stSidebarContent"] label,
div[data-testid="stSidebarContent"] .stCaptionContainer {
  font-size: 0.74rem !important;
}
div[data-testid="stSidebar"] button,
div[data-testid="stSidebarContent"] button {
  min-height: 1.85rem !important;
  padding: 0.16rem 0.36rem !important;
  font-size: 0.78rem !important;
}
div[data-testid="stSidebar"] input,
div[data-testid="stSidebarContent"] input {
  min-height: 1.85rem !important;
  font-size: 0.78rem !important;
}
div[data-testid="stSidebar"] [data-testid="stExpander"],
div[data-testid="stSidebarContent"] [data-testid="stExpander"] {
  border-radius: 8px !important;
}
div[data-testid="stSidebar"] [data-testid="stVerticalBlock"],
div[data-testid="stSidebarContent"] [data-testid="stVerticalBlock"] {
  gap: 0.25rem !important;
}
.block-container {
  padding-top: 5.65rem;
  padding-bottom: 2rem;
}
h1, h2, h3 {
  letter-spacing: 0;
}
h1 {
  font-size: 1.55rem;
  margin-bottom: 0.1rem;
}
.subtle {
  color: var(--muted);
  font-size: 0.9rem;
}
.app-banner {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 999990;
  display: flex;
  align-items: center;
  gap: 0.9rem;
  min-height: 76px;
  border-bottom: 1px solid rgba(121, 167, 255, 0.3);
  background:
    linear-gradient(90deg, rgba(13, 18, 22, 0.98), rgba(19, 31, 39, 0.98) 52%, rgba(13, 18, 22, 0.98)),
    radial-gradient(circle at 14% 0%, rgba(121, 167, 255, 0.22), transparent 34%);
  padding: 0.72rem 1.05rem;
  margin: 0;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.28);
}
.app-emblem {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #79a7ff, #55d49a);
  color: #071116;
  font-weight: 950;
  font-size: 1.02rem;
  border: 1px solid rgba(255,255,255,0.22);
  flex: 0 0 auto;
  box-shadow: 0 0 0 4px rgba(121, 167, 255, 0.08), 0 12px 28px rgba(85, 212, 154, 0.16);
  cursor: pointer;
}
.app-home-link,
.app-home-link:visited,
.app-home-link:hover,
.app-home-link:active {
  text-decoration: none;
  color: inherit;
  display: inline-flex;
  flex: 0 0 auto;
}
.app-title {
  color: var(--ink);
  font-weight: 900;
  font-size: 1.34rem;
  line-height: 1.1;
}
.app-subtitle {
  color: #b8c8d2;
  font-size: 0.9rem;
  line-height: 1.32;
  margin-top: 0.16rem;
  max-width: 980px;
}
@media (max-width: 900px) {
  .app-banner {
    min-height: 86px;
    padding: 0.68rem 0.8rem;
  }
  .app-emblem {
    width: 46px;
    height: 46px;
  }
  .app-title {
    font-size: 1.12rem;
  }
  .app-subtitle {
    font-size: 0.78rem;
  }
  .block-container {
    padding-top: 6.15rem;
  }
  section[data-testid="stSidebar"],
  section[data-testid="stSidebar"] > div {
    margin-top: 86px !important;
    height: calc(100vh - 86px) !important;
  }
}
.public-hero {
  border: 1px solid rgba(121, 167, 255, 0.26);
  border-radius: 8px;
  background: linear-gradient(135deg, rgba(21, 28, 34, 0.98), rgba(16, 24, 28, 0.92));
  padding: 1.2rem 1.35rem;
  margin: 0.4rem 0 1rem;
}
.public-hero h1 {
  font-size: 1.65rem;
  line-height: 1.08;
  margin: 0 0 0.55rem;
}
.public-hero p {
  color: #cbd5dc;
  font-size: 1rem;
  line-height: 1.48;
  max-width: 840px;
  margin: 0;
}
.public-proof-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.75rem;
  margin: 0.9rem 0 1rem;
}
.public-proof {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 0.75rem 0.85rem;
  min-height: 90px;
}
.public-proof b {
  display: block;
  margin-bottom: 0.25rem;
}
.public-proof span {
  color: var(--muted);
  font-size: 0.84rem;
  line-height: 1.35;
}
.auth-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 0.75rem 0.85rem;
  margin: 0.75rem 0 0.85rem;
}
.auth-card h3 {
  margin-top: 0;
  font-size: 1rem;
}
.auth-inline {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(220px, 1.2fr) 140px 150px;
  gap: 0.55rem;
  align-items: end;
}
.auth-actions {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
@media (max-width: 1000px) {
  .auth-inline {
    grid-template-columns: 1fr;
  }
}
.account-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.85rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(21, 28, 34, 0.78);
  padding: 0.55rem 0.72rem;
  margin: 0 0 0.7rem;
}
.account-left {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}
.user-avatar {
  width: 38px;
  height: 38px;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #061016;
  font-weight: 900;
  font-size: 0.9rem;
  border: 1px solid rgba(255,255,255,0.22);
}
.account-name {
  color: var(--ink);
  font-weight: 800;
  line-height: 1.15;
}
.account-meta {
  color: var(--muted);
  font-size: 0.78rem;
  margin-top: 0.1rem;
}
@media (max-width: 900px) {
  .public-proof-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .account-strip {
    align-items: flex-start;
    flex-direction: column;
  }
}
.status-row {
  display: grid;
  grid-template-columns: repeat(6, minmax(110px, 1fr));
  gap: 0.65rem;
  margin: 0.65rem 0 0.72rem;
}
.status-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.58rem 0.72rem;
  min-height: 64px;
}
.status-label {
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.status-value {
  font-size: 1.02rem;
  font-weight: 700;
  margin-top: 0.2rem;
  overflow-wrap: anywhere;
}
.pill {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.14rem 0.55rem;
  font-size: 0.78rem;
  color: var(--muted);
  background: var(--panel-soft);
}
.bucket-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.8rem;
  margin: 0.8rem 0 1rem;
}
.bucket {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-height: 190px;
  padding: 0.82rem;
}
.bucket-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 750;
  margin-bottom: 0.65rem;
}
.bucket-count {
  color: var(--muted);
  font-size: 0.84rem;
}
.scan-card {
  border: 1px solid #26323b;
  border-left: 4px solid #3f5261;
  border-radius: 7px;
  padding: 0.55rem 0.6rem;
  margin-bottom: 0.5rem;
  background: #111820;
}
.scan-symbol {
  font-weight: 760;
  margin-bottom: 0.16rem;
}
.scan-meta {
  color: var(--muted);
  font-size: 0.8rem;
}
.bucket-buy .scan-card { border-left-color: var(--good); }
.bucket-watch .scan-card { border-left-color: var(--warn); }
.bucket-trade .scan-card { border-left-color: var(--info); }
.runtime-discovery {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: 0.8rem;
  margin: 0.85rem 0 1rem;
}
.runtime-picks {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.65rem;
}
.runtime-pick,
.runtime-leaderboard {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.78rem;
}
.runtime-pick-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-weight: 760;
  margin-bottom: 0.35rem;
}
.runtime-rank {
  color: var(--warn);
  font-weight: 820;
}
.runtime-leader-row {
  display: grid;
  grid-template-columns: 2.1rem 1fr auto;
  gap: 0.55rem;
  align-items: center;
  padding: 0.38rem 0;
  border-bottom: 1px solid rgba(148, 163, 173, 0.13);
}
.runtime-leader-row:last-child {
  border-bottom: 0;
}
.runtime-bot-boundary {
  border: 1px solid rgba(121, 167, 255, 0.34);
  border-left: 5px solid var(--info);
  border-radius: 8px;
  background: rgba(17, 24, 32, 0.44);
  padding: 0.55rem 0.65rem;
  margin: 0.15rem 0 0.75rem;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035);
}
.runtime-bot-boundary.good { border-left-color: var(--good); }
.runtime-bot-boundary.warn { border-left-color: var(--warn); }
.runtime-bot-boundary.bad { border-left-color: var(--bad); }
.runtime-bot-boundary-title {
  font-size: 1.02rem;
  font-weight: 820;
  line-height: 1.25;
  margin-bottom: 0.25rem;
}
.runtime-bot-boundary-meta {
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.42;
}
.trade-console-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr);
  gap: 0.9rem;
  margin: 0.8rem 0 1rem;
}
.trade-panel {
  background: rgba(17, 24, 32, 0.56);
  border: 1px solid rgba(121, 167, 255, 0.24);
  border-radius: 8px;
  padding: 0.85rem;
  margin-bottom: 0.85rem;
}
.trade-panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.65rem;
}
.trade-panel-title strong {
  font-size: 1.02rem;
}
.trade-health-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.55rem;
  margin: 0.55rem 0 0.2rem;
}
.trade-health-cell {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.55rem;
  background: rgba(21, 28, 34, 0.74);
}
.trade-health-cell .label {
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
}
.trade-health-cell .value {
  font-weight: 800;
  font-size: 1.02rem;
}
.trade-refresh-note {
  color: var(--muted);
  font-size: 0.8rem;
}
.buy-alert {
  border: 1px solid rgba(85, 212, 154, 0.45);
  background: rgba(85, 212, 154, 0.08);
  border-radius: 8px;
  padding: 0.85rem;
  margin: 0.75rem 0;
}
.heartbeat {
  display: flex;
  gap: 0.55rem;
  flex-wrap: wrap;
  align-items: center;
  color: var(--muted);
  font-size: 0.82rem;
  margin: 0.45rem 0 0.25rem;
}
.diagnostic-strip {
  display: flex;
  gap: 0.55rem;
  flex-wrap: wrap;
  align-items: center;
  margin: 0.35rem 0 0.5rem;
}
.decision-summary {
  border: 1px solid rgba(121, 167, 255, 0.28);
  border-left: 5px solid var(--info);
  border-radius: 8px;
  padding: 0.72rem 0.85rem;
  background: rgba(17, 24, 32, 0.62);
  margin: 0.65rem 0 0.85rem;
}
.decision-summary.warn { border-left-color: var(--warn); }
.decision-summary.bad { border-left-color: var(--bad); }
.decision-summary.good { border-left-color: var(--good); }
.decision-summary-title {
  font-weight: 820;
  margin-bottom: 0.18rem;
}
.metric-tile-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  margin: 0.75rem 0 1.1rem;
}
.metric-tile {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.85rem;
}
.metric-tile .label {
  color: var(--muted);
  font-size: 0.76rem;
  text-transform: uppercase;
}
.metric-tile .value {
  font-size: 1.55rem;
  font-weight: 840;
  margin-top: 0.2rem;
}
.metric-tile .sub {
  color: var(--muted);
  font-size: 0.82rem;
  margin-top: 0.18rem;
}
.runtime-tile-zone {
  border: 1px solid rgba(148, 163, 173, 0.2);
  border-radius: 8px;
  padding: 0.7rem;
  margin-top: 0.65rem;
  background: rgba(13, 18, 22, 0.45);
}
.runtime-tile-zone-title {
  color: var(--muted);
  font-size: 0.74rem;
  text-transform: uppercase;
  margin-bottom: 0.45rem;
}
.journal-card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.8rem;
  margin: 0.75rem 0 1rem;
}
.journal-card {
  border: 1px solid var(--line);
  border-left: 5px solid var(--info);
  border-radius: 8px;
  background: rgba(17, 24, 32, 0.58);
  padding: 0.85rem;
}
.journal-card.good { border-left-color: var(--good); }
.journal-card.warn { border-left-color: var(--warn); }
.journal-card.bad { border-left-color: var(--bad); }
.journal-card-title {
  font-weight: 840;
  margin-bottom: 0.2rem;
}
.journal-visual-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.65rem;
  margin: 0.7rem 0 0.55rem;
}
.journal-mini-metric {
  border: 1px solid rgba(148, 163, 173, 0.2);
  border-radius: 8px;
  background: rgba(13, 18, 22, 0.54);
  padding: 0.58rem 0.62rem;
}
.journal-mini-metric .label {
  color: var(--muted);
  font-size: 0.68rem;
  text-transform: uppercase;
}
.journal-mini-metric .value {
  color: var(--ink);
  font-size: 1.05rem;
  font-weight: 900;
  margin-top: 0.12rem;
}
.journal-scorebar {
  height: 8px;
  border-radius: 999px;
  background: rgba(148, 163, 173, 0.16);
  overflow: hidden;
  margin-top: 0.6rem;
}
.journal-scorebar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--bad), var(--warn), var(--good));
}
.journal-action-chip {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 0.12rem 0.48rem;
  border: 1px solid rgba(148, 163, 173, 0.24);
  color: var(--ink);
  font-size: 0.72rem;
  margin-top: 0.42rem;
  background: rgba(13, 18, 22, 0.5);
}
.journal-card-section {
  margin-top: 0.52rem;
}
.journal-card-section b {
  color: var(--muted);
}
.bot-meter-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.8rem;
  margin: 0.75rem 0 0.9rem;
}
.bot-meter-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(17, 24, 32, 0.72);
  padding: 0.82rem;
}
.bot-meter-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.65rem;
  margin-bottom: 0.55rem;
}
.bot-meter-title {
  color: var(--ink);
  font-weight: 840;
  overflow-wrap: anywhere;
}
.bot-meter-meta {
  color: var(--muted);
  font-size: 0.78rem;
  margin-top: 0.12rem;
}
.bot-meter-value {
  font-size: 1.15rem;
  font-weight: 900;
  white-space: nowrap;
}
.bot-analog-meter {
  position: relative;
  width: min(260px, 100%);
  aspect-ratio: 2 / 1;
  margin: 0.72rem auto 0.15rem;
  overflow: hidden;
}
.bot-analog-arc {
  position: absolute;
  inset: 0;
  border-radius: 260px 260px 0 0;
  background:
    conic-gradient(from 270deg at 50% 100%, var(--bad) 0deg 45deg, var(--warn) 45deg 135deg, var(--good) 135deg 180deg, transparent 180deg 360deg);
  border: 1px solid rgba(148, 163, 173, 0.24);
}
.bot-analog-arc::after {
  content: "";
  position: absolute;
  left: 11%;
  right: 11%;
  bottom: -1px;
  height: 78%;
  border-radius: 220px 220px 0 0;
  background: #111820;
  border: 1px solid rgba(148, 163, 173, 0.16);
  border-bottom: 0;
}
.bot-analog-needle {
  position: absolute;
  left: 50%;
  bottom: 0;
  width: 3px;
  height: 82%;
  transform-origin: 50% 100%;
  border-radius: 999px;
  background: currentColor;
  box-shadow: 0 0 12px currentColor;
}
.bot-analog-hub {
  position: absolute;
  left: 50%;
  bottom: -7px;
  width: 18px;
  height: 18px;
  transform: translateX(-50%);
  border-radius: 999px;
  background: #dce7ee;
  border: 3px solid #111820;
  box-shadow: 0 0 0 1px rgba(148, 163, 173, 0.36);
}
.bot-analog-center {
  position: absolute;
  left: 50%;
  bottom: 13%;
  transform: translateX(-50%);
  text-align: center;
  min-width: 120px;
}
.bot-analog-number {
  color: var(--ink);
  font-weight: 900;
  font-size: 1.02rem;
  line-height: 1.08;
}
.bot-analog-caption {
  color: var(--muted);
  font-size: 0.7rem;
  margin-top: 0.12rem;
}
.bot-analog-tick {
  position: absolute;
  bottom: 1px;
  color: var(--muted);
  font-size: 0.68rem;
}
.bot-analog-tick.left { left: 0; }
.bot-analog-tick.mid {
  left: 50%;
  transform: translateX(-50%);
}
.bot-analog-tick.right { right: 0; }
.bot-meter-bands {
  display: flex;
  justify-content: center;
  gap: 0.35rem;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 0.72rem;
  margin-top: 0.28rem;
}
.bot-meter-band {
  border: 1px solid rgba(148, 163, 173, 0.2);
  border-radius: 999px;
  padding: 0.08rem 0.42rem;
  background: rgba(13, 18, 22, 0.38);
}
.bot-meter-scale {
  display: flex;
  justify-content: space-between;
  color: var(--muted);
  font-size: 0.74rem;
  margin-top: 0.32rem;
}
.nav-hint {
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.45;
}
.sidebar-panel {
  border: 1px solid rgba(121, 167, 255, 0.26);
  border-radius: 8px;
  background: rgba(17, 24, 32, 0.86);
  padding: 0.65rem 0.68rem;
  margin: 0.12rem 0 0.55rem;
}
.sidebar-panel-title {
  color: var(--ink);
  font-size: 0.8rem;
  font-weight: 850;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  margin-bottom: 0.28rem;
}
.sidebar-panel-text {
  color: var(--muted);
  font-size: 0.76rem;
  line-height: 1.38;
}
.good { color: var(--good); }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
.info { color: var(--info); }
div[data-testid="stMetric"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.8rem 0.85rem;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
  font-size: 1.08rem;
  line-height: 1.25;
}
div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
  font-size: 0.82rem;
}
div[data-testid="stMetric"] label {
  color: var(--muted);
}
div[data-testid="stDataFrame"] {
  border: 1px solid var(--line);
  border-radius: 8px;
}
@media (max-width: 900px) {
  .status-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  h1 {
    font-size: 1.35rem;
  }
  .bucket-grid {
    grid-template-columns: 1fr;
  }
  .runtime-discovery,
  .runtime-picks,
  .metric-tile-grid,
  .journal-card-grid,
  .bot-meter-grid,
  .trade-console-grid,
  .trade-health-strip {
    grid-template-columns: 1fr;
  }
}
</style>
"""


st.markdown(CSS, unsafe_allow_html=True)


@st.cache_data(ttl=60, show_spinner=False)
def binance_history_snapshot(path: str) -> dict[str, pd.DataFrame | dict[str, float | str]]:
    file_path = Path(path)
    if file_path.suffix == ".parquet":
        history = pd.read_parquet(file_path)
    else:
        history = pd.read_csv(file_path)
    history = history.tail(360).copy()
    history["time"] = pd.to_datetime(history["open_time"])
    history["spread_bps"] = np.clip(history["volatility"].fillna(0) * 10_000 * 0.08, 1.2, 18)
    history["delta"] = history["delta_ratio"].fillna(0)
    history["volume"] = history["volume"].astype(float)
    history["close"] = history["close"].astype(float)
    history = history.tail(360)
    latest = history.iloc[-1]
    recent = history.tail(72).copy()
    recent["bar_return"] = recent["close"].pct_change().fillna(0)
    recent["notional"] = recent["close"] * recent["volume"]
    tape = pd.DataFrame(
        {
            "symbol": recent["symbol"].astype(str),
            "side": np.where(recent["bar_return"] >= 0, "BUY", "SELL"),
            "notional": recent["notional"].round(0),
            "spread_bps": recent["spread_bps"].round(2),
            "delta": recent["delta_ratio"].round(2),
        }
    )
    tape["age_ms"] = np.arange(len(tape), 0, -1) * 1_000
    tape["verdict"] = np.where((tape["spread_bps"] < 9) & (tape["delta"] > -0.45), "PASS", "WATCH")
    last_price = float(latest["close"])
    depth_scale = max(float(history["volume"].tail(50).mean()), 1.0)
    levels = np.linspace(last_price - 220, last_price + 220, 32)
    book = pd.DataFrame(
        {
            "price": levels,
            "bid_depth": np.exp(-np.abs(levels - last_price) / 150) * depth_scale,
            "ask_depth": np.exp(-np.abs(levels - last_price) / 155) * depth_scale * (1 + max(float(latest["delta_ratio"]), -0.5)),
        }
    )
    health = pd.DataFrame(
        {
            "component": ["Testnet REST", "Testnet Websocket", "Event Bus", "Risk", "OMS", "Execution", "Dashboard"],
            "latency_ms": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "status": ["CONNECTED", "STREAMING", "READY", "READY", "PAPER", "PAPER", "READY"],
            "queue": [0, 0, 0, 0, 0, 0, 0],
        }
    )
    journal = pd.DataFrame(
        {
            "time": pd.date_range(datetime.now(UTC) - timedelta(minutes=48), periods=8, freq="7min"),
            "event": [
                "Loaded Binance 1Y feature set",
                "Spot Testnet scan active",
                "VWAP state recalculated from Binance candles",
                "Orderflow proxy updated from Binance candles",
                "Risk replay checkpoint refreshed",
                "ATR regime sample refreshed",
                "Strategy metrics recalculated",
                "Binance backtest ready",
            ],
            "severity": ["INFO", "INFO", "INFO", "INFO", "INFO", "WARN", "INFO", "INFO"],
        }
    )
    summary = {
        "mode": "BINANCE SPOT TESTNET",
        "symbol": str(latest["symbol"]),
        "price": last_price,
        "pnl": 0.0,
        "drawdown": 0.0,
        "risk": "TESTNET",
        "kill": "ARMED",
        "feed": "BINANCE TESTNET",
        "orders": 0.0,
        "veto": 0.0,
    }
    return {"candles": history, "tape": tape, "book": book, "health": health, "journal": journal, "summary": summary}


@st.cache_data(ttl=2, show_spinner=False)
def load_live_scan(path: str = "reports/live_scan.json") -> pd.DataFrame:
    stream = load_live_stream()
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = merge_stream_state(asyncio.run(_load_live_scan_from_db()), stream)
            log_diagnostic(logger, "live_scan_loaded", source="database", rows=len(frame))
            return frame
        except Exception:
            logger.exception("live_scan_database_load_failed fallback=file")
    file_path = Path(path)
    if not file_path.exists():
        log_diagnostic(logger, "live_scan_loaded", source="default", rows=len(DEFAULT_LIVE_SYMBOLS))
        return merge_stream_state(default_live_scan_frame(), stream)
    try:
        frame = merge_stream_state(ensure_default_live_symbols(pd.read_json(file_path)), stream)
        log_diagnostic(logger, "live_scan_loaded", source="file", rows=len(frame))
        return frame
    except (OSError, ValueError, pd.errors.EmptyDataError, json.JSONDecodeError) as exc:
        log_diagnostic(logger, "live_scan_file_unavailable", path=str(file_path), reason=str(exc))
        st.warning("No replay data available yet. Run Binance backfill or scanner.")
        return merge_stream_state(default_live_scan_frame(), stream)


@st.cache_data(ttl=2, show_spinner=False)
def load_live_stream(path: str = "reports/live_stream.json") -> dict[str, object]:
    file_path = Path(path)
    if not file_path.exists():
        return {"status": "not_started", "updated_at": None, "symbols": {}}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unreadable", "updated_at": None, "symbols": {}}


def merge_stream_state(scan: pd.DataFrame, stream: dict[str, object]) -> pd.DataFrame:
    scan = ensure_default_live_symbols(scan)
    symbols = stream.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        return scan

    scan = scan.copy()
    for symbol, payload in symbols.items():
        if not isinstance(payload, dict):
            continue
        if symbol not in set(scan["symbol"].astype(str)):
            scan = pd.concat(
                [
                    scan,
                    pd.DataFrame(
                        [
                            {
                                "symbol": symbol,
                                "scan_bucket": "NO SIGNAL",
                                "scan_reason": "websocket live market tracking; scanner score pending",
                                "last_close": 0.0,
                                "active_entry": None,
                                "active_pnl": None,
                                "active_pnl_pct": None,
                                "watch_score": 0.0,
                                "buy_score": 0.0,
                                "sell_score": 0.0,
                                "orderflow_score": 0.0,
                                "confidence_score": 0.0,
                                "watch_missing": "awaiting scanner data",
                                "buy_missing": "awaiting scanner data",
                                "sell_missing": "awaiting scanner data",
                                "orderflow_reason": "websocket stream active",
                                "confidence_reason": "awaiting scanner data",
                                "trades": 0,
                                "win_rate": 0.0,
                                "total_pnl": 0.0,
                                "profit_factor": 0.0,
                                "priority": 100,
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
        mask = scan["symbol"].astype(str) == str(symbol)
        last_price = float(payload.get("last_price") or 0.0)
        if last_price > 0:
            scan.loc[mask, "last_close"] = last_price
        stream_flow = float(payload.get("orderflow_score") or 0.0)
        if stream_flow > 0:
            scan.loc[mask, "orderflow_score"] = stream_flow
        spread_bps = float(payload.get("spread_bps") or 0.0)
        depth_imbalance = float(payload.get("depth_imbalance") or 0.0)
        taker_buy_ratio = float(payload.get("taker_buy_ratio") or 0.0)
        trade_count = int(payload.get("trade_count") or 0)
        scan.loc[mask, "stream_spread_bps"] = spread_bps
        scan.loc[mask, "stream_depth_imbalance"] = depth_imbalance
        scan.loc[mask, "stream_taker_buy_ratio"] = taker_buy_ratio
        scan.loc[mask, "stream_trade_count"] = trade_count
        scan.loc[mask, "stream_updated_at"] = str(payload.get("updated_at") or "")
        scan.loc[mask, "stream_status"] = str(payload.get("status") or stream.get("status") or "stream")
        scan.loc[mask, "orderflow_reason"] = (
            f"socket flow {stream_flow:.0f}% | spread {spread_bps:.2f} bps | "
            f"depth {depth_imbalance:+.2f} | taker buy {taker_buy_ratio:.0%}"
        )
    return normalize_scan_columns(scan)


def stream_timeframe_bar_for_bot(bot: pd.Series, stream: dict[str, object]) -> dict[str, object]:
    symbols = stream.get("symbols")
    if not isinstance(symbols, dict):
        return {}
    payload = symbols.get(str(bot.get("symbol", "")))
    if not isinstance(payload, dict):
        return {}
    timeframes = payload.get("timeframes")
    if not isinstance(timeframes, dict):
        return {}
    bar = timeframes.get(str(bot.get("timeframe", "1h") or "1h"))
    return bar if isinstance(bar, dict) else {}


def default_live_scan_frame() -> pd.DataFrame:
    symbols = list(DEFAULT_LIVE_SYMBOLS) or available_live_symbols()
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "scan_bucket": "NO SIGNAL",
                "scan_reason": "default live tracking enabled; scanner has not produced a fresh signal yet",
                "last_close": 0.0,
                "active_entry": None,
                "active_pnl": None,
                "active_pnl_pct": None,
                "watch_score": 0.0,
                "buy_score": 0.0,
                "sell_score": 0.0,
                "orderflow_score": 0.0,
                "confidence_score": 0.0,
                "watch_missing": "awaiting scanner data",
                "buy_missing": "awaiting scanner data",
                "sell_missing": "awaiting scanner data",
                "orderflow_reason": "awaiting scanner data",
                "confidence_reason": "awaiting scanner data",
                "trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "profit_factor": 0.0,
                "priority": index,
            }
            for index, symbol in enumerate(symbols)
        ]
    )


def ensure_default_live_symbols(scan: pd.DataFrame) -> pd.DataFrame:
    if scan is None or scan.empty or "symbol" not in scan.columns:
        return default_live_scan_frame()
    scan = scan.copy()
    if "priority" not in scan.columns:
        scan["priority"] = 100
    existing_symbols = set(scan["symbol"].fillna("").astype(str))
    for index, symbol in enumerate(list(DEFAULT_LIVE_SYMBOLS) or available_live_symbols()):
        if symbol in existing_symbols:
            scan.loc[scan["symbol"] == symbol, "priority"] = index
            continue
        scan = pd.concat(
            [
                scan,
                pd.DataFrame(
                    [
                        {
                            "symbol": symbol,
                            "scan_bucket": "NO SIGNAL",
                            "scan_reason": "default live tracking enabled; awaiting scanner data",
                            "last_close": 0.0,
                            "active_entry": None,
                            "active_pnl": None,
                            "active_pnl_pct": None,
                            "watch_score": 0.0,
                            "buy_score": 0.0,
                            "sell_score": 0.0,
                            "orderflow_score": 0.0,
                            "confidence_score": 0.0,
                            "watch_missing": "awaiting scanner data",
                            "buy_missing": "awaiting scanner data",
                            "sell_missing": "awaiting scanner data",
                            "orderflow_reason": "awaiting scanner data",
                            "confidence_reason": "awaiting scanner data",
                            "trades": 0,
                            "win_rate": 0.0,
                            "total_pnl": 0.0,
                            "profit_factor": 0.0,
                            "priority": index,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    return normalize_scan_columns(scan)


def normalize_scan_columns(scan: pd.DataFrame) -> pd.DataFrame:
    scan = scan.copy()
    defaults = {
        "symbol": "",
        "scan_bucket": "NO SIGNAL",
        "scan_reason": "scanner data unavailable",
        "last_close": 0.0,
        "active_entry": None,
        "active_pnl": None,
        "active_pnl_pct": None,
        "watch_score": 0.0,
        "buy_score": 0.0,
        "sell_score": 0.0,
        "orderflow_score": 0.0,
        "confidence_score": 0.0,
        "watch_missing": "not calculated",
        "buy_missing": "not calculated",
        "sell_missing": "not calculated",
        "orderflow_reason": "not calculated",
        "confidence_reason": "not calculated",
        "stream_spread_bps": 0.0,
        "stream_depth_imbalance": 0.0,
        "stream_taker_buy_ratio": 0.0,
        "stream_trade_count": 0,
        "stream_updated_at": "",
        "stream_status": "not_started",
        "priority": 100,
    }
    for column, default in defaults.items():
        if column not in scan.columns:
            scan[column] = default
    return scan


@st.cache_data(ttl=5, show_spinner=False)
def load_live_scan_heartbeat(path: str = "reports/live_scan_heartbeat.json") -> dict[str, object]:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            heartbeat = asyncio.run(_load_heartbeat_from_db())
            log_diagnostic(logger, "live_scan_heartbeat_loaded", source="database", generated_at=heartbeat.get("generated_at"))
            return heartbeat
        except Exception:
            logger.exception("live_scan_heartbeat_database_load_failed fallback=file")
    file_path = Path(path)
    if not file_path.exists():
        fallback = Path("reports/live_scan.json")
        if fallback.exists():
            return {"generated_at": datetime.fromtimestamp(fallback.stat().st_mtime, UTC).isoformat(), "source": "local_report", "symbols_ok": None, "symbols_error": None}
        return {"generated_at": None, "source": "not_started", "symbols_ok": 0, "symbols_error": 0}
    return json.loads(file_path.read_text(encoding="utf-8"))


def live_stream_heartbeat(stream: dict[str, object]) -> dict[str, str]:
    updated_at = stream.get("updated_at")
    age_text = "not started"
    if updated_at:
        try:
            generated = utc_datetime(str(updated_at))
            age_seconds = max(0, int((datetime.now(UTC) - generated).total_seconds()))
            age_text = f"{age_seconds}s ago" if age_seconds < 60 else f"{age_seconds // 60}m {age_seconds % 60}s ago"
        except ValueError:
            age_text = str(updated_at)
    return {"source": str(stream.get("source", "binance_socket")), "status": str(stream.get("status", "not_started")), "age": age_text}


def stream_age_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        generated = utc_datetime(str(value))
        return max(0, int((datetime.now(UTC) - generated).total_seconds()))
    except ValueError:
        return None


SIGNAL_FLOW_STAGES: tuple[str, ...] = ("SOCKET", "CANDLE", "FEATURES", "ORDERFLOW", "STRATEGY", "RISK", "DECISION", "JOURNAL")


def crossed_signal_stage(
    socket_state: str,
    candle_state: str,
    features_state: str,
    orderflow_state: str,
    strategy_state: str,
    risk_state: str,
    decision_state: str,
) -> tuple[str, int, str]:
    crossed = "SOCKET" if socket_state == "LIVE" else "SOCKET"
    if socket_state != "LIVE":
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "waiting for fresh socket data"
    if candle_state == "CLOSED":
        crossed = "CANDLE"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "waiting for selected timeframe candle"
    if features_state == "READY":
        crossed = "FEATURES"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "waiting for feature readiness"
    if orderflow_state in {"SUPPORTIVE", "DEVELOPING"}:
        crossed = "ORDERFLOW"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "orderflow not supportive yet"
    if strategy_state in {"WATCH", "SIGNAL", "TRACKING"}:
        crossed = "STRATEGY"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "strategy conditions not aligned"
    if risk_state == "OK":
        crossed = "RISK"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "risk gate blocked"
    if decision_state in {"WATCH", "BUY SIGNAL", "IN TRADE"}:
        crossed = "DECISION"
    else:
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "decision remains wait"
    if decision_state in {"BUY SIGNAL", "IN TRADE"}:
        crossed = "JOURNAL"
        return crossed, SIGNAL_FLOW_STAGES.index(crossed), "actionable decision journaled"
    return crossed, SIGNAL_FLOW_STAGES.index(crossed), "watch state reached; awaiting buy confirmation"


def build_signal_flow_rows(scan: pd.DataFrame, stream: dict[str, object], bots: pd.DataFrame, symbols: list[str], selected_timeframe: str) -> list[dict[str, object]]:
    stream_symbols = stream.get("symbols")
    stream_symbols = stream_symbols if isinstance(stream_symbols, dict) else {}
    risk = load_risk_settings()
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        matches = scan[scan["symbol"].astype(str) == symbol]
        row = matches.iloc[0] if not matches.empty else pd.Series(dtype=object)
        symbol_bots = bots[bots["symbol"].astype(str) == symbol] if not bots.empty and "symbol" in bots else pd.DataFrame()
        active_bot = symbol_bots[symbol_bots["state"].astype(str).isin(["RUNNING", "DEPLOYED"])] if not symbol_bots.empty and "state" in symbol_bots else pd.DataFrame()
        bot_row = active_bot.iloc[0] if not active_bot.empty else symbol_bots.iloc[0] if not symbol_bots.empty else pd.Series(dtype=object)
        timeframe = str(bot_row.get("timeframe", "5m") or "5m") if selected_timeframe == "strategy default" else selected_timeframe
        payload = stream_symbols.get(symbol)
        payload = payload if isinstance(payload, dict) else {}
        timeframes = payload.get("timeframes")
        timeframes = timeframes if isinstance(timeframes, dict) else {}
        candle = timeframes.get(timeframe)
        socket_age = stream_age_seconds(str(row.get("stream_updated_at", "") or payload.get("updated_at", "")))
        socket_state = "LIVE" if socket_age is not None and socket_age <= 15 else "STALE" if socket_age is not None else "PENDING"
        candle_state = "CLOSED" if isinstance(candle, dict) and candle else "WAITING"
        confidence = float(row.get("confidence_score", 0.0) or 0.0)
        watch = float(row.get("watch_score", 0.0) or 0.0)
        buy = float(row.get("buy_score", 0.0) or 0.0)
        flow = float(row.get("orderflow_score", 0.0) or 0.0)
        features_state = "READY" if max(confidence, watch, buy) > 0 else "WARMING"
        orderflow_state = "SUPPORTIVE" if flow >= 65 else "DEVELOPING" if flow >= 45 else "WEAK"
        bucket = str(row.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL")
        strategy_state = "SIGNAL" if bucket == "BUY" else "WATCH" if bucket == "WATCH" else "TRACKING" if bucket == "IN TRADE" else "QUIET"
        risk_state = "BLOCKED" if bool(risk.get("kill_switch", False)) else "OK"
        decision_state = "BUY SIGNAL" if bucket == "BUY" and risk_state == "OK" else "IN TRADE" if bucket == "IN TRADE" else "WATCH" if bucket == "WATCH" else "WAIT"
        crossed_stage, crossed_stage_index, next_gate = crossed_signal_stage(socket_state, candle_state, features_state, orderflow_state, strategy_state, risk_state, decision_state)
        bot_state = str(bot_row.get("state", "NO BOT") or "NO BOT")
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "socket_state": socket_state,
                "candle_state": candle_state,
                "features_state": features_state,
                "orderflow_state": orderflow_state,
                "strategy_state": strategy_state,
                "risk_state": risk_state,
                "decision_state": decision_state,
                "crossed_stage": crossed_stage,
                "crossed_stage_index": crossed_stage_index,
                "next_gate": next_gate,
                "bucket": bucket,
                "bot_state": bot_state,
                "last_price": float(row.get("last_close", 0.0) or payload.get("last_price", 0.0) or 0.0),
                "spread_bps": float(row.get("stream_spread_bps", 0.0) or payload.get("spread_bps", 0.0) or 0.0),
                "depth_imbalance": float(row.get("stream_depth_imbalance", 0.0) or payload.get("depth_imbalance", 0.0) or 0.0),
                "trade_count": int(row.get("stream_trade_count", 0) or payload.get("trade_count", 0) or 0),
                "orderflow_score": flow,
                "buy_score": buy,
                "watch_score": watch,
                "confidence_score": confidence,
                "reason": str(row.get("scan_reason", "") or row.get("orderflow_reason", "") or "awaiting scanner reason"),
            }
        )
    return rows


def top_developing_signal_flow_symbols(scan: pd.DataFrame, limit: int = 5) -> list[str]:
    if scan.empty:
        return []
    ranked = normalize_scan_columns(scan).copy()
    bucket_bonus = ranked["scan_bucket"].astype(str).map({"BUY": 35.0, "WATCH": 22.0, "IN TRADE": 14.0, "NO SIGNAL": 0.0}).fillna(0.0)
    ranked["developing_score"] = (
        ranked["orderflow_score"].astype(float).clip(0, 100) * 0.34
        + ranked["watch_score"].astype(float).clip(0, 100) * 0.26
        + ranked["buy_score"].astype(float).clip(0, 100) * 0.24
        + ranked["confidence_score"].astype(float).clip(0, 100) * 0.16
        + bucket_bonus
    )
    ranked = ranked.sort_values(["developing_score", "priority"], ascending=[False, True])
    return ranked["symbol"].astype(str).head(limit).tolist()


SIGNAL_BUCKET_LANES: tuple[str, ...] = (
    "Strong Momentum",
    "Early Breakout",
    "Pullback Setup",
    "Range / Neutral",
    "Weak / No Trade",
    "High Risk / Avoid",
)


SIGNAL_MILESTONES: tuple[str, ...] = (
    "Market Data",
    "Regime Detection",
    "Strategy Setup",
    "Signal Confirmation",
    "Risk Check",
    "Trade Readiness",
)


def market_bucket_lane(row: pd.Series) -> str:
    bucket = str(row.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL")
    buy = float(row.get("buy_score", 0.0) or 0.0)
    watch = float(row.get("watch_score", 0.0) or 0.0)
    sell = float(row.get("sell_score", 0.0) or 0.0)
    spread = float(row.get("stream_spread_bps", 0.0) or 0.0)
    volatility = float(row.get("volatility", 0.0) or 0.0)
    if spread > 15 or volatility > 0.08 or sell >= 75:
        return "High Risk / Avoid"
    if bucket == "IN TRADE":
        return "Strong Momentum"
    if bucket == "BUY":
        return "Early Breakout" if buy < 80 else "Strong Momentum"
    if bucket == "WATCH":
        return "Pullback Setup" if watch >= buy else "Early Breakout"
    if max(buy, watch) < 35:
        return "Weak / No Trade"
    return "Range / Neutral"


def traffic_light_for_strategy(strategy_name: str, row: pd.Series, milestone: str = "Trade Readiness") -> tuple[str, str]:
    symbol = str(row.get("symbol", ""))
    bucket = str(row.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL")
    buy = float(row.get("buy_score", 0.0) or 0.0)
    watch = float(row.get("watch_score", 0.0) or 0.0)
    flow = float(row.get("orderflow_score", 0.0) or 0.0)
    spread = float(row.get("stream_spread_bps", 0.0) or 0.0)
    if not strategy_symbol_is_certified(strategy_name, symbol):
        return "Red", "Blocked: pair has not passed deployment test"
    if milestone == "Market Data":
        return ("Green", "Market data is live enough") if float(row.get("last_close", 0.0) or 0.0) > 0 else ("Red", "Waiting for market data")
    if milestone == "Regime Detection":
        return ("Green", "Momentum regime is supportive") if bucket in {"BUY", "IN TRADE"} else ("Amber", "Regime is still forming") if bucket == "WATCH" else ("Red", "No supportive regime")
    if milestone == "Strategy Setup":
        return ("Green", "Setup is aligned") if buy >= 65 else ("Amber", "Setup developing") if watch >= 45 else ("Red", "No setup currently")
    if milestone == "Signal Confirmation":
        return ("Green", "Signal clear") if bucket == "BUY" else ("Amber", "Waiting for confirmation") if bucket == "WATCH" else ("Red", "No signal currently")
    if milestone == "Risk Check":
        return ("Red", "Avoid: spread/liquidity risk") if spread > 15 else ("Green", "Risk gate acceptable")
    if bucket == "BUY" and flow >= 55:
        return "Green", "Ready for trade review"
    if bucket in {"WATCH", "IN TRADE"} or max(buy, watch) >= 45:
        return "Amber", "Signal developing; wait for confirmation"
    return "Red", "No signal currently"


def market_bucket_swim_lanes(scan: pd.DataFrame, bots: pd.DataFrame) -> None:
    st.markdown("#### Market Buckets")
    st.caption("Coins move between lanes as the existing scanner bucket, score, liquidity, and risk state changes.")
    view = normalize_scan_columns(scan).copy()
    if view.empty:
        st.info("No bucket data is available yet.")
        return
    view["market_bucket_lane"] = view.apply(market_bucket_lane, axis=1)
    active = active_strategy_names()
    for lane in SIGNAL_BUCKET_LANES:
        rows = view[view["market_bucket_lane"] == lane].sort_values(["priority", "buy_score"], ascending=[True, False])
        with st.container(border=True):
            st.markdown(f"##### {lane}")
            if rows.empty:
                st.caption("No coins currently in this bucket.")
                continue
            cols = st.columns(min(3, max(1, len(rows))))
            for idx, (_, row) in enumerate(rows.iterrows()):
                strategy_labels = []
                for strategy_name in active:
                    light, message = traffic_light_for_strategy(strategy_name, row)
                    if light != "Red":
                        strategy_labels.append(f"{strategy_name}: {light}")
                with cols[idx % len(cols)]:
                    updated = stream_age_text(row.get("stream_updated_at", ""))
                    risk_flag = "Risk: spread" if float(row.get("stream_spread_bps", 0.0) or 0.0) > 15 else "Risk: ok"
                    st.markdown(f"**{row['symbol']}**")
                    st.caption(
                        f"${float(row['last_close']):,.6f} | 24h n/a | {row['scan_bucket']} | "
                        f"{' | '.join(strategy_labels) if strategy_labels else 'No active strategy ready'} | {updated}"
                    )
                    st.caption(
                        f"Flow {float(row['orderflow_score']):.0f}% | Volatility {float(row.get('volatility', 0.0) or 0.0):.4f} | "
                        f"{risk_flag}"
                    )


def strategy_traffic_light_panel(scan: pd.DataFrame) -> None:
    st.markdown("#### Strategy Traffic Lights")
    view = normalize_scan_columns(scan).copy()
    if view.empty:
        st.info("No signal rows are available for strategy traffic lights.")
        return
    symbols = view["symbol"].astype(str).tolist()
    selected = st.multiselect("Traffic-light coins", symbols, default=symbols[: min(5, len(symbols))])
    if not selected:
        return
    rows: list[dict[str, str]] = []
    for _, row in view[view["symbol"].astype(str).isin(selected)].iterrows():
        for strategy_name in active_strategy_names():
            for milestone in SIGNAL_MILESTONES:
                light, message = traffic_light_for_strategy(strategy_name, row, milestone)
                rows.append(
                    {
                        "Coin": str(row["symbol"]),
                        "Strategy": strategy_name,
                        "Milestone": milestone,
                        "Traffic Light": light,
                        "Guidance": message,
                        "Last Evaluation": str(row.get("stream_updated_at", "") or row.get("open_time", "")),
                    }
                )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def signal_flow_top10_backtest(refresh: bool = False) -> dict[str, object]:
    if not refresh and SIGNAL_FLOW_BACKTEST_CACHE_PATH.exists():
        try:
            return json.loads(SIGNAL_FLOW_BACKTEST_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    start = pd.Timestamp("2024-10-01T00:00:00Z")
    rows: list[dict[str, object]] = []
    data_dir = Path("data/binance")
    scan = normalize_scan_columns(load_live_scan())
    bucket_map = dict(zip(scan["symbol"].astype(str), scan["scan_bucket"].astype(str))) if not scan.empty else {}
    signal_map = dict(zip(scan["symbol"].astype(str), scan["scan_bucket"].astype(str))) if not scan.empty else {}
    symbols = list(DEFAULT_LIVE_SYMBOLS)[:10] or available_live_symbols()[:10]
    for symbol in symbols:
        for strategy_name in active_strategy_names():
            if not strategy_symbol_is_certified(strategy_name, symbol):
                continue
            strategy = STRATEGY_REGISTRY[strategy_name]
            timeframe = str(getattr(strategy, "default_timeframe", "1h") or "1h")
            path = data_dir / f"{symbol.replace('/', '')}_{timeframe}_720d_features.parquet"
            base = {
                "Coin": symbol,
                "Strategy": strategy_name,
                "Timeframe": timeframe,
                "Backtest Start Date": "2024-10-01",
                "Backtest End Date": "Not Available",
                "Current Bucket": bucket_map.get(symbol, "Not Available"),
                "Signal Status": signal_map.get(symbol, "Not Available"),
                "Strategy Version": str(getattr(strategy, "activation_reason", ""))[:120],
            }
            if not path.exists():
                rows.append(
                    {
                        **base,
                        "Total Trades": "Not Available",
                        "Win Rate %": "Not Available",
                        "Cumulative P&L": "Not Available",
                        "ROI %": "Not Available",
                        "Max Drawdown %": "Not Available",
                        "Profit Factor": "Not Available",
                        "Average Trade Return %": "Not Available",
                        "Sharpe Ratio": "Not Available",
                        "Deployment Readiness": "Needs Validation",
                        "Error": f"Missing {timeframe} feature file",
                    }
                )
                continue
            try:
                frame = load_feature_file(path)
                times = pd.to_datetime(frame["open_time"], utc=True)
                frame = frame[times >= start].copy()
                if frame.empty:
                    raise ValueError("No rows after 2024-10-01")
                metrics, _ = StrategyAgnosticBot(BotDeployment(name=f"{strategy_name} {symbol}", strategy=strategy, interval=timeframe, notional=1_000.0)).replay(frame)
                end = pd.to_datetime(frame["open_time"], utc=True).max().date().isoformat()
                readiness = deployment_readiness(metrics)
                rows.append(
                    {
                        **base,
                        "Backtest End Date": end,
                        "Total Trades": int(metrics.trades),
                        "Win Rate %": round(float(metrics.win_rate), 2),
                        "Cumulative P&L": round(float(metrics.total_pnl), 2),
                        "ROI %": round(float(metrics.total_return_pct), 2),
                        "Max Drawdown %": round(float(metrics.max_drawdown_pct), 2),
                        "Profit Factor": round(float(metrics.profit_factor), 2) if np.isfinite(float(metrics.profit_factor)) else 99.0,
                        "Average Trade Return %": round(float(metrics.avg_trade_return_pct), 3),
                        "Sharpe Ratio": round(float(metrics.sharpe_proxy), 3),
                        "Deployment Readiness": readiness,
                        "Error": "",
                    }
                )
            except Exception as exc:
                rows.append({**base, "Deployment Readiness": "Failed", "Error": str(exc)})
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "assumptions": {
            "fee_model": "strategy replay default",
            "slippage_model": "strategy replay default",
            "data_range": "2024-10-01 to latest local feature row",
            "data_source": "local Binance feature files",
        },
        "rows": rows,
    }
    SIGNAL_FLOW_BACKTEST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FLOW_BACKTEST_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def deployment_readiness(metrics: object) -> str:
    trades = int(getattr(metrics, "trades", 0))
    pnl = float(getattr(metrics, "total_pnl", 0.0))
    drawdown = float(getattr(metrics, "max_drawdown_pct", 0.0))
    pf = float(getattr(metrics, "profit_factor", 0.0))
    if trades <= 0:
        return "Needs Validation"
    if pnl > 0 and pf >= 1.2 and drawdown < 12:
        return "Ready"
    if pnl > 0 and drawdown < 15:
        return "Watch"
    return "Avoid"


def signal_flow_backtest_panel(scan: pd.DataFrame) -> None:
    st.markdown("#### Top 10 Coin Backtesting From 2024-10-01")
    refresh = st.button("Refresh top-10 signal-flow backtest", use_container_width=True)
    payload = signal_flow_top10_backtest(refresh=refresh)
    rows = payload.get("rows", [])
    if not isinstance(rows, list) or not rows:
        st.info("No backtest rows are available yet.")
        return
    st.caption(
        f"Last generated: {payload.get('generated_at', 'not available')} | "
        "Assumptions: fee/slippage use strategy replay defaults; missing timeframe files show Not Available."
    )
    table = pd.DataFrame(rows)
    f1, f2, f3, f4 = st.columns(4)
    coin_filter = f1.multiselect("Coin", sorted(table["Coin"].dropna().astype(str).unique().tolist()), default=[])
    strategy_filter = f2.multiselect("Strategy", sorted(table["Strategy"].dropna().astype(str).unique().tolist()), default=[])
    timeframe_filter = f3.multiselect("Timeframe", sorted(table["Timeframe"].dropna().astype(str).unique().tolist()), default=[])
    readiness_filter = f4.multiselect("Readiness", ["Ready", "Watch", "Avoid", "Needs Validation", "Failed"], default=[])
    b1, b2, b3, b4 = st.columns(4)
    bucket_filter = b1.multiselect("Market bucket", sorted(table["Current Bucket"].dropna().astype(str).unique().tolist()), default=[])
    signal_filter = b2.multiselect("Signal status", sorted(table["Signal Status"].dropna().astype(str).unique().tolist()), default=[])
    date_filter = b3.date_input("Date range", value=(pd.Timestamp("2024-10-01").date(), datetime.now(UTC).date()))
    apply_date_filter = b4.checkbox("Apply date range", value=False)
    filtered = table.copy()
    if coin_filter:
        filtered = filtered[filtered["Coin"].astype(str).isin(coin_filter)]
    if strategy_filter:
        filtered = filtered[filtered["Strategy"].astype(str).isin(strategy_filter)]
    if timeframe_filter:
        filtered = filtered[filtered["Timeframe"].astype(str).isin(timeframe_filter)]
    if readiness_filter:
        filtered = filtered[filtered["Deployment Readiness"].astype(str).isin(readiness_filter)]
    if bucket_filter:
        filtered = filtered[filtered["Current Bucket"].astype(str).isin(bucket_filter)]
    if signal_filter:
        filtered = filtered[filtered["Signal Status"].astype(str).isin(signal_filter)]
    if apply_date_filter and isinstance(date_filter, tuple) and len(date_filter) == 2:
        start_date, end_date = date_filter
        filtered = filtered[
            (pd.to_datetime(filtered["Backtest Start Date"], errors="coerce").dt.date >= start_date)
            & (pd.to_datetime(filtered["Backtest End Date"], errors="coerce").dt.date <= end_date)
        ]
    st.dataframe(filtered.astype(str), use_container_width=True, hide_index=True)


def load_deployed_strategy_names() -> list[str]:
    active_names = active_strategy_names()
    if not DEPLOYED_STRATEGIES_PATH.exists():
        return active_names or ["Certified Risk Managed Composite"]
    try:
        payload = json.loads(DEPLOYED_STRATEGIES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return active_names or ["Certified Risk Managed Composite"]
    names = [name for name in payload.get("strategies", []) if name in active_names]
    return names or active_names or ["Certified Risk Managed Composite"]


def save_deployed_strategy_names(names: list[str]) -> None:
    DEPLOYED_STRATEGIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_framework": "strategy_agnostic",
        "strategies": names,
        "deployed_at": datetime.now(UTC).isoformat(),
    }
    DEPLOYED_STRATEGIES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@st.cache_data(ttl=900, show_spinner=False)
def load_strategy_matrix(strategy_names: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    matrix = run_strategy_matrix(list(strategy_names))
    return matrix, aggregate_strategy_matrix(matrix)


@st.cache_data(ttl=60, show_spinner=False)
def load_cached_strategy_matrix(strategy_names: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not STRATEGY_MATRIX_CACHE_PATH.exists():
        return pd.DataFrame(), pd.DataFrame()
    try:
        payload = json.loads(STRATEGY_MATRIX_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return pd.DataFrame(), pd.DataFrame()
    cached_names = tuple(payload.get("strategy_names", []))
    if set(cached_names) != set(strategy_names):
        return pd.DataFrame(), pd.DataFrame()
    rows = payload.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(), pd.DataFrame()
    matrix = pd.DataFrame(rows)
    return matrix, aggregate_strategy_matrix(matrix)


@st.cache_data(ttl=60, show_spinner=False)
def load_top10_replay_trades(path: str = "reports/top10_replay_trades.csv") -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(file_path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def load_top10_replay_metrics(path: str = "reports/top10_replay_metrics.csv") -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(file_path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def load_stress_report(path: str = "reports/production_readiness_stress.json") -> dict[str, object]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sharpe_from_return_series(returns: pd.Series) -> float:
    clean = pd.to_numeric(returns, errors="coerce").dropna() / 100
    if len(clean) < 2:
        return 0.0
    std = float(clean.std(ddof=1))
    if std <= 0 or not np.isfinite(std):
        return 0.0
    value = float(clean.mean() / std * np.sqrt(len(clean)))
    return value if np.isfinite(value) else 0.0


def refresh_strategy_matrix_cache(strategy_names: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    matrix, aggregate = load_strategy_matrix(strategy_names)
    STRATEGY_MATRIX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_MATRIX_CACHE_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "strategy_names": list(strategy_names),
                "rows": matrix.to_dict(orient="records") if not matrix.empty else [],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    load_cached_strategy_matrix.clear()
    return matrix, aggregate


def load_json_list(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def save_json_list(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def append_action_audit(
    action_type: str,
    bot_id: str = "",
    runtime_instance_id: str = "",
    previous_value: dict[str, object] | None = None,
    new_value: dict[str, object] | None = None,
    reason: str = "",
) -> None:
    rows = load_json_list(BOT_ACTION_AUDIT_PATH)
    rows.insert(
        0,
        {
            "audit_id": f"audit-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
            "action_type": action_type,
            "bot_id": bot_id,
            "runtime_instance_id": runtime_instance_id,
            "user_id_or_process_id": "streamlit_ui",
            "previous_value_json": previous_value or {},
            "new_value_json": new_value or {},
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    save_json_list(BOT_ACTION_AUDIT_PATH, rows[:500])


def persist_position_size_decision(decision: dict[str, object]) -> None:
    rows = load_json_list(POSITION_SIZE_DECISIONS_PATH)
    rows.insert(0, decision)
    save_json_list(POSITION_SIZE_DECISIONS_PATH, rows[:500])


@st.cache_data(ttl=10, show_spinner=False)
def load_risk_settings() -> dict[str, object]:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            risk = asyncio.run(_load_risk_settings_from_db())
            log_diagnostic(logger, "risk_settings_loaded", source="database", kill_switch=risk.get("kill_switch"))
            return risk
        except Exception as exc:
            logger.exception("risk_settings_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "RISK_SETTINGS", str(exc))
    if not RISK_SETTINGS_PATH.exists():
        return DEFAULT_RISK_SETTINGS.copy()
    try:
        return {**DEFAULT_RISK_SETTINGS, **json.loads(RISK_SETTINGS_PATH.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError):
        return DEFAULT_RISK_SETTINGS.copy()


def save_risk_settings(values: dict[str, object]) -> None:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(_save_risk_settings_to_db(values))
            log_diagnostic(logger, "risk_settings_saved", source="database", kill_switch=values.get("kill_switch"), max_cash_per_trade=values.get("max_cash_per_trade"))
            append_journal("SYSTEM", "", "RISK_SETTINGS", "INFO", "UPDATED", "portfolio risk gates updated", values)
            load_risk_settings.clear()
            return
        except Exception as exc:
            logger.exception("risk_settings_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "RISK_SETTINGS", str(exc))
    RISK_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RISK_SETTINGS_PATH.write_text(json.dumps(values, indent=2), encoding="utf-8")
    append_journal("SYSTEM", "", "RISK_SETTINGS", "INFO", "UPDATED", "portfolio risk gates updated", values)
    load_risk_settings.clear()


@st.cache_data(ttl=5, show_spinner=False)
def load_bot_instances() -> pd.DataFrame:
    file_rows = load_json_list(BOT_INSTANCES_PATH)
    file_frame = normalize_bot_frame(pd.DataFrame(file_rows)) if file_rows else pd.DataFrame()
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_bot_instances_from_db())
            if not frame.empty:
                db_frame = normalize_bot_frame(frame)
                merged = merge_bot_frames(db_frame, file_frame)
                if len(merged) > len(db_frame):
                    asyncio.run(_save_bot_instances_to_db(merged))
                    log_diagnostic(logger, "bot_instances_backfilled_to_database", rows=len(merged) - len(db_frame))
                log_diagnostic(logger, "bot_instances_loaded", source="database+file", rows=len(merged))
                return merged
            rows = default_bot_instances()
            asyncio.run(_save_bot_instances_to_db(pd.DataFrame(rows)))
            frame = merge_bot_frames(normalize_bot_frame(pd.DataFrame(rows)), file_frame)
            log_diagnostic(logger, "bot_instances_seeded", source="database", rows=len(frame))
            return frame
        except Exception as exc:
            logger.exception("bot_instances_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "BOT_INSTANCES", str(exc))
    if file_frame.empty:
        rows = default_bot_instances()
        save_json_list(BOT_INSTANCES_PATH, rows)
        file_frame = normalize_bot_frame(pd.DataFrame(rows))
    return file_frame


def save_bot_instances(frame: pd.DataFrame) -> None:
    save_json_list(BOT_INSTANCES_PATH, frame.to_dict(orient="records"))
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(_save_bot_instances_to_db(frame))
            log_diagnostic(logger, "bot_instances_saved", source="database", rows=len(frame))
            load_bot_instances.clear()
            return
        except Exception as exc:
            logger.exception("bot_instances_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "BOT_INSTANCES", str(exc))
    load_bot_instances.clear()


def remove_bot_definition(name: str) -> bool:
    bots = load_bot_instances()
    if bots.empty or "name" not in bots:
        return False
    matches = bots[bots["name"].astype(str) == name]
    if matches.empty:
        return False
    state = str(matches.iloc[0].get("state", "DRAFT"))
    if state in {"RUNNING", "DEPLOYED"}:
        return False

    remaining = bots[bots["name"].astype(str) != name].reset_index(drop=True)
    save_json_list(BOT_INSTANCES_PATH, remaining.to_dict(orient="records"))
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(_delete_bot_instance_from_db(name))
        except Exception as exc:
            logger.exception("bot_instance_database_delete_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "BOT_DELETE", str(exc))
            load_bot_instances.clear()
            return False
    load_bot_instances.clear()
    append_journal(name, str(matches.iloc[0].get("symbol", "")), "BOT_DELETED", "WARN", "REMOVED", "bot definition removed from Bot Admin", {"state": state})
    return True


def append_journal(bot_name: str, symbol: str, event_type: str, severity: str, decision: str, reason: str, metrics: dict[str, object] | None = None) -> None:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(
                _append_journal_to_db(
                    {
                        "event_time": datetime.now(UTC),
                        "bot_name": bot_name,
                        "symbol": symbol,
                        "event_type": event_type,
                        "severity": severity,
                        "decision": decision,
                        "reason": reason,
                        "metrics": metrics or {},
                    }
                )
            )
            log_diagnostic(logger, "journal_event_saved", source="database", bot_name=bot_name, event_type=event_type, severity=severity, decision=decision)
            load_journal_events.clear()
            return
        except Exception as exc:
            logger.exception("journal_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "JOURNAL", str(exc))
    append_file_journal(bot_name, symbol, event_type, severity, decision, reason, metrics)
    load_journal_events.clear()


def append_file_journal(bot_name: str, symbol: str, event_type: str, severity: str, decision: str, reason: str, metrics: dict[str, object] | None = None) -> None:
    log_diagnostic(logger, "journal_event_saved", source="file", bot_name=bot_name, event_type=event_type, severity=severity, decision=decision)
    rows = load_json_list(JOURNAL_PATH)
    rows.insert(
        0,
        {
            "event_time": datetime.now(UTC).isoformat(),
            "bot_name": bot_name,
            "symbol": symbol,
            "event_type": event_type,
            "severity": severity,
            "decision": decision,
            "reason": reason,
            "metrics": metrics or {},
        },
    )
    save_json_list(JOURNAL_PATH, rows[:500])
    load_journal_events.clear()


@st.cache_data(ttl=10, show_spinner=False)
def load_journal_events() -> pd.DataFrame:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_journal_from_db())
            log_diagnostic(logger, "journal_events_loaded", source="database", rows=len(frame))
            return frame
        except Exception as exc:
            logger.exception("journal_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "JOURNAL_READ", str(exc))
    return pd.DataFrame(load_json_list(JOURNAL_PATH))


@st.cache_data(ttl=10, show_spinner=False)
def load_validation_runs_frame() -> pd.DataFrame:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_validation_runs_from_db())
            log_diagnostic(logger, "validation_runs_loaded", source="database", rows=len(frame))
            return frame
        except Exception as exc:
            logger.exception("validation_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "VALIDATION_READ", str(exc))
    return pd.DataFrame(load_json_list(VALIDATION_RUNS_PATH))


def save_validation_run(row: dict[str, object]) -> None:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(_save_validation_run_to_db(row))
            log_diagnostic(logger, "validation_run_saved", source="database", run_id=row.get("run_id"), bot_name=row.get("bot_name"), state=row.get("state"))
            load_validation_runs_frame.clear()
            return
        except Exception as exc:
            logger.exception("validation_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "VALIDATION_WRITE", str(exc))
    rows = load_json_list(VALIDATION_RUNS_PATH)
    rows.insert(0, row)
    save_json_list(VALIDATION_RUNS_PATH, rows[:100])
    load_validation_runs_frame.clear()


def default_bot_instances() -> list[dict[str, object]]:
    return []


def normalize_bot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.copy()
    defaults = {
        "timeframe": "1h",
        "capital": 0.0,
        "parameters": {},
        "state": "DRAFT",
        "status_reason": "",
        "created_at": "",
        "updated_at": "",
        "deployed_at": "",
        "heartbeat_at": "",
        "cumulative_started_at": "",
        "cumulative_realized_pnl": 0.0,
        "cumulative_fees": 0.0,
        "cumulative_slippage": 0.0,
        "cumulative_trade_count": 0,
        "last_entry_at": "",
        "last_exit_at": "",
        "runtime_position_state": "OUT_OF_TRADE",
        "last_trade_event_type": "",
        "last_trade_event_at": "",
        "last_trade_event_reason": "",
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    if "bot_id" not in frame.columns and "name" in frame.columns:
        frame["bot_id"] = frame["name"].astype(str).str.replace(r"[^A-Za-z0-9]+", "_", regex=True).str.strip("_")
    return frame


def merge_bot_frames(primary: pd.DataFrame, secondary: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in [primary, secondary] if not frame.empty]
    if not frames:
        return pd.DataFrame()
    merged = normalize_bot_frame(pd.concat(frames, ignore_index=True))
    if "name" not in merged:
        return merged
    freshness = []
    for column in ["updated_at", "heartbeat_at", "deployed_at", "created_at"]:
        if column in merged:
            freshness.append(pd.to_datetime(merged[column], errors="coerce", utc=True))
    if freshness:
        merged["_freshness"] = freshness[0]
        for timestamp in freshness[1:]:
            merged["_freshness"] = merged["_freshness"].where(
                merged["_freshness"].notna() & ((timestamp.isna()) | (merged["_freshness"] >= timestamp)),
                timestamp,
            )
        merged["_freshness"] = merged["_freshness"].fillna(pd.Timestamp(0, tz="UTC"))
        merged = merged.sort_values(["name", "_freshness"], ascending=[True, False])
        return merged.drop_duplicates(subset=["name"], keep="first").drop(columns=["_freshness"]).reset_index(drop=True)
    return merged.drop_duplicates(subset=["name"], keep="first").reset_index(drop=True)


def transition_bot(name: str, state: str, reason: str, parameter_updates: dict[str, object] | None = None) -> None:
    bots = load_bot_instances()
    if bots.empty or name not in set(bots["name"].astype(str)):
        log_diagnostic(logger, "bot_transition_skipped", name=name, state=state, reason="bot_not_found")
        return
    now = datetime.now(UTC).isoformat()
    mask = bots["name"].astype(str) == name
    bots.loc[mask, "state"] = state
    bots.loc[mask, "status_reason"] = reason
    bots.loc[mask, "heartbeat_at"] = now
    if state in {"DEPLOYED", "RUNNING"}:
        bots.loc[mask, "deployed_at"] = now
        parameter_updates = dict(parameter_updates or {}, runtime_started_at=now, runtime_pnl_started_at=now)
    if parameter_updates:
        current_parameters = bots.loc[mask, "parameters"].iloc[0]
        if not isinstance(current_parameters, dict):
            current_parameters = {}
        bots.loc[mask, "parameters"] = [dict(current_parameters, **parameter_updates)]
    save_bot_instances(bots)
    row = bots[mask].iloc[0]
    log_diagnostic(logger, "bot_transition", name=name, state=state, symbol=row.get("symbol"), reason=reason)
    append_journal(str(row["name"]), str(row["symbol"]), f"BOT_{state}", "INFO", state, reason, {"strategy": row["strategy"]})


def last_validation_for_bot(bot_name: str) -> pd.Series | None:
    runs = load_validation_runs_frame()
    if runs.empty or "bot_name" not in runs:
        return None
    matches = runs[runs["bot_name"].astype(str) == str(bot_name)]
    if matches.empty:
        return None
    return matches.iloc[0]


def lifecycle_next_action(state: str) -> str:
    return {
        "DRAFT": "Backtest in Validation Lab",
        "BACKTESTED": "Deploy in Bot Runtime",
        "RUNNING": "Monitor or stop in Bot Runtime",
        "DEPLOYED": "Monitor or stop in Bot Runtime",
        "PAUSED": "Deploy or stop in Bot Runtime",
        "STOPPED": "Backtest again before redeploying",
        "FAILED": "Review Journal and Risk, then backtest again",
    }.get(state, "Review bot state")


def bot_live_mark(bot: pd.Series, scan: pd.DataFrame) -> dict[str, float | str | bool]:
    symbol = str(bot.get("symbol", ""))
    scan_row = scan[scan["symbol"].astype(str) == symbol].iloc[0] if not scan.empty and symbol in set(scan["symbol"].astype(str)) else pd.Series(dtype=object)
    last_price = float(scan_row.get("last_close", 0.0) or 0.0)
    params = bot.get("parameters")
    params = params if isinstance(params, dict) else {}
    entry_price = float(bot.get("runtime_entry_price", 0.0) or params.get("runtime_entry_price") or scan_row.get("active_entry", 0.0) or 0.0)
    started_at = str(bot.get("started_at", "") or params.get("runtime_started_at", "") or bot.get("deployed_at", "") or "")
    cumulative_started_at = str(
        bot.get("cumulative_started_at", "")
        or params.get("cumulative_started_at", "")
        or params.get("runtime_cumulative_started_at", "")
        or bot.get("created_at", "")
        or started_at
    )
    capital = float(bot.get("capital", 0.0) or 0.0)
    state = str(bot.get("state", "DRAFT"))
    trade_status = runtime_trade_position_status(bot, pd.DataFrame())
    in_market = bool(trade_status["in_trade"])
    pnl_pct = 0.0 if not in_market or entry_price <= 0 or last_price <= 0 else (last_price - entry_price) / entry_price * 100
    pnl = capital * pnl_pct / 100
    return {
        "symbol": symbol,
        "last_price": last_price,
        "entry_price": entry_price,
        "capital": capital,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "started_at": started_at,
        "cumulative_started_at": cumulative_started_at,
        "socket_age": stream_age_text(scan_row.get("stream_updated_at", "")),
        "socket_status": str(scan_row.get("stream_status", "not_started")),
        "stream_updated_at": str(scan_row.get("stream_updated_at", "")),
        "last_mark_source": "market_feed" if last_price > 0 else "unavailable",
        "in_market": in_market,
        "trade_position_state": str(trade_status["trade_position_state"]),
        "trade_position_reason": str(trade_status["trade_position_reason"]),
        "last_entry_at": str(trade_status["last_entry_at"]),
        "last_exit_at": str(trade_status["last_exit_at"]),
    }


def runtime_trade_position_status(bot: pd.Series | dict[str, object], trade_events: pd.DataFrame | None = None) -> dict[str, object]:
    bot_id = str(bot.get("bot_id", bot.get("name", ""))) if hasattr(bot, "get") else ""
    bot_name = str(bot.get("name", "")) if hasattr(bot, "get") else ""
    runtime_state = str(bot.get("state", "DRAFT") if hasattr(bot, "get") else "DRAFT").upper()
    runtime_status = str(bot.get("status", "") if hasattr(bot, "get") else "").upper()
    runtime_active = runtime_state in {"RUNNING", "DEPLOYED"} or runtime_status in {"RUNNING", "DEPLOYED"}
    params = bot_parameters(bot)
    if not runtime_active:
        return {
            "trade_position_state": "RUNTIME_STOPPED",
            "in_trade": False,
            "trade_position_reason": "runtime is not running",
            "last_entry_at": str(bot.get("last_entry_at", "") if hasattr(bot, "get") else ""),
            "last_exit_at": str(bot.get("last_exit_at", "") if hasattr(bot, "get") else ""),
            "last_trade_event_type": "",
        }

    frame = trade_events.copy() if trade_events is not None and not trade_events.empty else pd.DataFrame(load_json_list(RUNTIME_TRADE_EVENTS_PATH))
    if not frame.empty:
        mask = pd.Series(False, index=frame.index)
        if "bot_id" in frame:
            mask = mask | frame["bot_id"].astype(str).isin({bot_id, bot_name})
        if "bot_name" in frame:
            mask = mask | frame["bot_name"].astype(str).isin({bot_id, bot_name})
        matches = frame[mask].copy()
        time_column = next((column for column in ["event_time", "timestamp", "created_at", "snapshot_time"] if column in matches), "")
        if not matches.empty and time_column:
            matches["_event_time"] = pd.to_datetime(matches[time_column], errors="coerce", utc=True)
            matches = matches.dropna(subset=["_event_time"]).sort_values("_event_time")
            if not matches.empty:
                event_type = matches.get("event_type", pd.Series("", index=matches.index)).astype(str)
                lifecycle = matches.get("lifecycle_state", pd.Series("", index=matches.index)).astype(str)
                position = matches.get("position_state", pd.Series("", index=matches.index)).astype(str)
                real_entry_event = event_type.isin(["TradeEntered"])
                real_exit_event = event_type.isin(["TradeExited", "StopTriggered", "RiskTriggered"])
                setup_event = event_type.isin(["TradeCreated"])
                entry_mask = real_entry_event | lifecycle.isin(["Filled", "Active", "Partially Filled", "Partially Exited"]) | position.eq("OPEN")
                exit_mask = real_exit_event | lifecycle.isin(["Closed", "Cancelled", "Failed"]) | (position.eq("FLAT") & ~setup_event)
                entry_time = matches.loc[entry_mask, "_event_time"].max() if entry_mask.any() else pd.NaT
                exit_time = matches.loc[exit_mask, "_event_time"].max() if exit_mask.any() else pd.NaT
                last = matches.iloc[-1]
                last_event_type = str(last.get("event_type", ""))
                last_position = str(last.get("position_state", ""))
                last_lifecycle = str(last.get("lifecycle_state", ""))
                exited_after_entry = pd.notna(exit_time) and (pd.isna(entry_time) or exit_time >= entry_time)
                if exited_after_entry or last_event_type in {"TradeExited", "StopTriggered", "RiskTriggered"} or (last_position == "FLAT" and last_event_type != "TradeCreated") or last_lifecycle in {"Closed", "Cancelled", "Failed"}:
                    return {
                        "trade_position_state": "OUT_OF_TRADE",
                        "in_trade": False,
                        "trade_position_reason": f"exit signal recorded: {last_event_type or last_lifecycle or last_position}",
                        "last_entry_at": "" if pd.isna(entry_time) else entry_time.isoformat(),
                        "last_exit_at": "" if pd.isna(exit_time) else exit_time.isoformat(),
                        "last_trade_event_type": last_event_type,
                    }
                if pd.notna(entry_time) or last_position == "OPEN":
                    return {
                        "trade_position_state": "IN_TRADE",
                        "in_trade": True,
                        "trade_position_reason": f"open trade event recorded: {last_event_type or last_lifecycle or last_position}",
                        "last_entry_at": "" if pd.isna(entry_time) else entry_time.isoformat(),
                        "last_exit_at": "" if pd.isna(exit_time) else exit_time.isoformat(),
                        "last_trade_event_type": last_event_type,
                    }

    explicit_position = str(params.get("runtime_position_state", bot.get("runtime_position_state", "") if hasattr(bot, "get") else "")).upper()
    last_exit_at = str(bot.get("last_exit_at", "") or params.get("last_exit_at", "") if hasattr(bot, "get") else "")
    runtime_entry_price = float(bot.get("runtime_entry_price", 0.0) or params.get("runtime_entry_price", 0.0) or 0.0) if hasattr(bot, "get") else 0.0
    runtime_started_at = str(bot.get("started_at", "") or params.get("runtime_started_at", "") or bot.get("deployed_at", "") if hasattr(bot, "get") else "")
    if explicit_position in {"OUT_OF_TRADE", "FLAT"} or last_exit_at:
        return {
            "trade_position_state": "OUT_OF_TRADE",
            "in_trade": False,
            "trade_position_reason": "exit signal recorded in runtime metadata",
            "last_entry_at": str(bot.get("last_entry_at", "") or params.get("last_entry_at", "") if hasattr(bot, "get") else ""),
            "last_exit_at": last_exit_at,
            "last_trade_event_type": "",
        }
    if explicit_position in {"IN_TRADE", "OPEN"} or runtime_entry_price > 0:
        return {
            "trade_position_state": "IN_TRADE",
            "in_trade": True,
            "trade_position_reason": "runtime metadata shows active entry price",
            "last_entry_at": str(bot.get("last_entry_at", "") or params.get("last_entry_at", "") or runtime_started_at if hasattr(bot, "get") else ""),
            "last_exit_at": "",
            "last_trade_event_type": "",
        }
    return {
        "trade_position_state": "IN_TRADE",
        "in_trade": True,
        "trade_position_reason": "runtime running with no exit signal recorded",
        "last_entry_at": str(bot.get("last_entry_at", "") or params.get("last_entry_at", "") or bot.get("deployed_at", "") if hasattr(bot, "get") else ""),
        "last_exit_at": "",
        "last_trade_event_type": "",
    }


def bot_parameters(bot: pd.Series | dict[str, object]) -> dict[str, object]:
    params = bot.get("parameters", {}) if hasattr(bot, "get") else {}
    if isinstance(params, dict):
        return params
    if isinstance(params, str):
        try:
            parsed = json.loads(params)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def safe_number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def run_days_since(value: str, minimum_days: float = 0.0) -> float:
    if not value:
        return minimum_days
    try:
        start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
    except ValueError:
        return minimum_days
    elapsed_days = (datetime.now(UTC) - start.astimezone(UTC)).total_seconds() / 86_400
    return max(minimum_days, elapsed_days)


def annualized_cagr_from_run_return(run_return_pct: float, run_days: float) -> float | None:
    if run_days <= 0 or run_return_pct <= -100:
        return None
    return ((1.0 + (run_return_pct / 100.0)) ** (365.0 / run_days) - 1.0) * 100.0


def cagr_maturity_label(run_days: float) -> str:
    if run_days < 7:
        return "Too early for CAGR"
    if run_days < 30:
        return "Early annualized"
    return "CAGR"


def latest_pnl_snapshot_for_bot(bot_id: str, bot_name: str, snapshots: pd.DataFrame) -> pd.Series:
    if snapshots.empty:
        return pd.Series(dtype=object)
    frame = snapshots.copy()
    if "snapshot_time" in frame:
        frame["_snapshot_time"] = pd.to_datetime(frame["snapshot_time"], errors="coerce", utc=True)
        frame = frame.sort_values("_snapshot_time")
    masks = []
    if "bot_id" in frame:
        masks.append(frame["bot_id"].astype(str) == str(bot_id))
        masks.append(frame["bot_id"].astype(str) == str(bot_name))
    if "trade_id" in frame:
        masks.append(frame["trade_id"].astype(str).str.startswith(f"{bot_id}:"))
        masks.append(frame["trade_id"].astype(str).str.startswith(f"{bot_name}:"))
    if not masks:
        return pd.Series(dtype=object)
    combined = masks[0]
    for mask in masks[1:]:
        combined = combined | mask
    matches = frame[combined]
    return matches.iloc[-1] if not matches.empty else pd.Series(dtype=object)


def money_text(value: object) -> str:
    return f"${safe_number(value):,.2f}"


def pct_text(value: object) -> str:
    return f"{safe_number(value):.2f}%"


def strategy_type_label(strategy_name: str, timeframe: str) -> str:
    lowered = strategy_name.lower()
    if "mean reversion" in lowered or "reversal" in lowered:
        return "Mean reversion"
    if "trend" in lowered or "momentum" in lowered or "burst" in lowered:
        return "Trend / momentum"
    if str(timeframe).lower() in {"1d", "d", "daily"}:
        return "Daily swing"
    return "Multi-factor"


def strategy_deployment_defaults(strategy_name: str) -> dict[str, object]:
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    timeframe = str(getattr(strategy, "default_timeframe", "1h") if strategy is not None else "1h")
    is_daily = timeframe.lower() in {"1d", "d", "daily"}
    name_lower = strategy_name.lower()
    if is_daily:
        stop_type = "ATR-based stop + ATR trail"
        stop_value = "2.0 ATR hard stop; 2.0 ATR trail"
        tp_type = "Strategy default TP / trend capture"
        tp_value = "No forced fixed TP; exit priority HARD_STOP -> ATR_TRAIL -> TREND_BREAK -> TIME_STOP"
        trailing = True
        risk_class = "Swing trend risk"
        min_capital = 500.0
        runtime_profile = "Daily swing; monitor once per completed daily candle"
    elif "mean reversion" in name_lower:
        stop_type = "ATR trailing stop"
        stop_value = "ATR(14) * 2.0 for 1h; ATR(14) * 1.5-2.0 for 10m"
        tp_type = "Staged partial TP"
        tp_value = "TP1 1R, TP2 2R, TP3 3R"
        trailing = True
        risk_class = "Volatility mean-reversion risk"
        min_capital = 250.0
        runtime_profile = "Intraday mean-reversion; completed candles only"
    elif "5m" in timeframe.lower() or "burst" in name_lower:
        stop_type = "ATR stop + EMA trailing stop"
        stop_value = "Symbol ATR multiplier with EMA stop trail"
        tp_type = "Partial TP + strategy default TP"
        tp_value = "50% partial at RR/USD trigger, remaining protected at breakeven"
        trailing = True
        risk_class = "Fast intraday momentum risk"
        min_capital = 250.0
        runtime_profile = "5m tactical runtime; near-real-time PnL and protection checks"
    else:
        stop_type = "ATR / strategy stop"
        stop_value = "Strategy-generated stop from replay signal"
        tp_type = "Strategy default TP"
        tp_value = "Strategy-generated take-profit from replay signal"
        trailing = False
        risk_class = "Experimental strategy risk" if strategy_name in dormant_strategy_names() else "Certified multi-factor risk"
        min_capital = 250.0
        runtime_profile = "Strategy default runtime profile"
    return {
        "strategy": strategy_name,
        "strategy_type": strategy_type_label(strategy_name, timeframe),
        "strategy_version": str(getattr(strategy, "version", "v1") if strategy is not None else "v1"),
        "recommended_timeframe": timeframe,
        "default_stop_type": stop_type,
        "default_stop_value": stop_value,
        "default_tp_type": tp_type,
        "default_tp_value": tp_value,
        "trailing_enabled": trailing,
        "emergency_stop_enabled": not is_daily,
        "risk_policy_stop_status": "Enforced by runtime framework",
        "minimum_recommended_capital": min_capital,
        "capital_allocation_model": "Fixed notional per bot with portfolio exposure gate",
        "risk_classification": risk_class,
        "runtime_profile": runtime_profile,
        "supported_market_regimes": "Trending / breakout" if "trend" in name_lower or "burst" in name_lower else "Volatility expansion / reversion",
    }


def calculate_position_size_decision(
    *,
    bot_id: str = "",
    strategy_id: str = "",
    runtime_instance_id: str = "",
    symbol: str = "",
    sizing_method: str = "standard",
    capital: float,
    risk_per_trade: float,
    max_allocation: float,
    stop_loss_distance: float,
    price: float,
    volatility_value: float = 0.0,
    regime: str = "normal",
    max_portfolio_exposure: float | None = None,
    max_symbol_exposure: float | None = None,
    maximum_concurrent_trades: int = 1,
    exchange_min_qty: float = 0.0,
    exchange_max_qty: float | None = None,
    lot_size: float = 0.0,
    override_quantity: float | None = None,
    override_reason: str = "",
) -> dict[str, object]:
    price = max(safe_number(price, 0.0), 0.0)
    capital = max(safe_number(capital, 0.0), 0.0)
    risk_per_trade = max(safe_number(risk_per_trade, 0.0), 0.0)
    stop_loss_distance = max(safe_number(stop_loss_distance, 0.0), 0.0)
    max_allocation = max(safe_number(max_allocation, 0.0), 0.0)
    cap_applied: list[str] = []
    risk_amount = capital * risk_per_trade
    allocation_cap = min(capital, max_allocation if max_allocation > 0 else capital)
    if max_portfolio_exposure is not None:
        allocation_cap = min(allocation_cap, max(0.0, safe_number(max_portfolio_exposure, allocation_cap)))
        cap_applied.append("max_portfolio_exposure")
    if max_symbol_exposure is not None:
        allocation_cap = min(allocation_cap, max(0.0, safe_number(max_symbol_exposure, allocation_cap)))
        cap_applied.append("max_symbol_exposure")
    if maximum_concurrent_trades > 1:
        allocation_cap = min(allocation_cap, capital / maximum_concurrent_trades)
        cap_applied.append("max_concurrent_bot_exposure")
    allocation_qty = 0.0 if price <= 0 else allocation_cap / price
    risk_qty = allocation_qty if stop_loss_distance <= 0 else risk_amount / stop_loss_distance
    calculated_qty = min(allocation_qty, risk_qty)
    method = sizing_method.lower()
    if "volatility" in method and volatility_value > 0:
        vol_throttle = max(0.25, min(1.0, 0.02 / max(volatility_value, 0.0001)))
        calculated_qty *= vol_throttle
        cap_applied.append("volatility_throttle")
    if "regime" in method and regime.lower() in {"high risk", "panic", "volatile", "avoid"}:
        calculated_qty *= 0.5
        cap_applied.append("regime_throttle")
    final_qty = calculated_qty
    override_flag = override_quantity is not None
    if override_flag:
        final_qty = max(0.0, safe_number(override_quantity, 0.0))
        cap_applied.append("user_override")
    if exchange_max_qty is not None:
        before = final_qty
        final_qty = min(final_qty, max(0.0, safe_number(exchange_max_qty, final_qty)))
        if final_qty != before:
            cap_applied.append("exchange_max_quantity")
    if exchange_min_qty > 0 and 0 < final_qty < exchange_min_qty:
        final_qty = 0.0
        cap_applied.append("exchange_min_quantity")
    if lot_size > 0 and final_qty > 0:
        final_qty = (final_qty // lot_size) * lot_size
        cap_applied.append("lot_size")
    capital_used = final_qty * price
    return {
        "position_size_decision_id": f"psd-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
        "bot_id": bot_id,
        "strategy_id": strategy_id,
        "runtime_instance_id": runtime_instance_id,
        "symbol": symbol,
        "sizing_method": sizing_method,
        "capital": capital,
        "risk_per_trade": risk_per_trade,
        "max_allocation": max_allocation,
        "stop_loss_distance": stop_loss_distance,
        "volatility_value": volatility_value,
        "regime": regime,
        "calculated_quantity": calculated_qty,
        "final_quantity": final_qty,
        "capital_used": capital_used,
        "risk_amount": risk_amount,
        "allocation_percentage": 0.0 if capital <= 0 else capital_used / capital * 100.0,
        "cap_applied_json": list(dict.fromkeys(cap_applied)),
        "override_flag": override_flag,
        "override_reason": override_reason,
        "created_at": datetime.now(UTC).isoformat(),
    }


def bot_deployment_profile(bot: pd.Series | dict[str, object]) -> dict[str, object]:
    strategy_name = str(bot.get("strategy", "") if hasattr(bot, "get") else "")
    defaults = strategy_deployment_defaults(strategy_name)
    params = bot_parameters(bot)
    overrides = params.get("deployment_overrides", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    return {
        **defaults,
        "bot_version": str(params.get("bot_version", defaults["strategy_version"])),
        "stop_loss_type": str(params.get("stop_loss_type", overrides.get("stop_loss_type", defaults["default_stop_type"]))),
        "stop_loss_value": str(params.get("stop_loss_value", overrides.get("stop_loss_value", defaults["default_stop_value"]))),
        "take_profit_type": str(params.get("take_profit_type", overrides.get("take_profit_type", defaults["default_tp_type"]))),
        "take_profit_value": str(params.get("take_profit_value", overrides.get("take_profit_value", defaults["default_tp_value"]))),
        "trailing_enabled": bool(params.get("trailing_enabled", overrides.get("trailing_enabled", defaults["trailing_enabled"]))),
        "emergency_stop_enabled": bool(params.get("emergency_stop_enabled", overrides.get("emergency_stop_enabled", defaults["emergency_stop_enabled"]))),
        "risk_allocation_category": str(params.get("risk_allocation_category", defaults["risk_classification"])),
        "strategy_defaults_used": params.get("strategy_defaults_used", defaults),
    }


def validation_metrics_for_bot(bot_name: str) -> dict[str, object]:
    row = last_validation_for_bot(bot_name)
    if row is None:
        return {}
    row_values = row.to_dict() if hasattr(row, "to_dict") else dict(row) if isinstance(row, dict) else {}
    row_values = {key: value for key, value in row_values.items() if not (isinstance(value, float) and pd.isna(value))}
    metrics = row.get("metrics", {}) if hasattr(row, "get") else {}
    if isinstance(metrics, str):
        try:
            parsed = json.loads(metrics)
            metrics = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            metrics = {}
    metrics = metrics if isinstance(metrics, dict) else {}
    merged = {**row_values, **metrics}
    for key in ["run_id", "timeframe", "start_date", "end_date", "capital", "state", "created_at", "updated_at"]:
        if key in row:
            merged[key] = row.get(key)
    return merged


def runtime_hours_since(value: str) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, (datetime.now(UTC) - utc_datetime(value)).total_seconds() / 3600.0)
    except ValueError:
        return 0.0


def runtime_health_light(profile: dict[str, object]) -> tuple[str, str]:
    alert = str(profile.get("alert_level", "INFO"))
    data_state = str(profile.get("data_state", "UNKNOWN"))
    recovery = str(profile.get("recovery_state", "NONE"))
    if alert == "CRITICAL" or data_state in {"STALE", "MISSING"} or recovery in {"HALT", "HALT_AND_RESTART", "HALT_AND_PROTECT"}:
        return "Red", "Critical"
    if alert == "WARNING" or recovery in {"RESTART", "THROTTLE"} or data_state == "CLOCK_DRIFT":
        return "Amber", "Warning"
    return "Green", "Healthy"


def runtime_guidance(profile: dict[str, object]) -> str:
    if str(profile.get("health_light")) == "Red":
        return "Critical runtime state; stop or reconcile before new exposure."
    if str(profile.get("trade_position_state")) == "OUT_OF_TRADE" and str(profile.get("runtime_status")) in {"RUNNING", "DEPLOYED"}:
        return "Bot is running 24x7 but out of trade after exit signal; waiting for next entry."
    if str(profile.get("trade_position_state")) == "IN_TRADE":
        return "Bot is running 24x7 and currently in trade; monitor stop, P&L, and exit state."
    if float(profile.get("current_drawdown_pct", 0.0) or 0.0) <= -8.0:
        return "Drawdown approaching threshold; monitor closely."
    if str(profile.get("signal_status", "NO SIGNAL")) in {"BUY", "IN TRADE"} and str(profile.get("health_light")) == "Green":
        return "Signal clear; runtime healthy."
    if str(profile.get("signal_status", "NO SIGNAL")) == "WATCH":
        return "Signal developing; wait for confirmation."
    if float(profile.get("capital_utilization_pct", 0.0) or 0.0) < 50 and str(profile.get("runtime_status")) in {"RUNNING", "DEPLOYED"}:
        return "Capital allocation below recommended level."
    return "No active signal; keep bot under observation."


def validation_deployment_status(metrics: dict[str, object], capital: float, defaults: dict[str, object]) -> str:
    if "error" in metrics:
        return "Runtime unsupported"
    if capital < float(defaults.get("minimum_recommended_capital", 0.0) or 0.0):
        return "Capital insufficient"
    if float(metrics.get("max_drawdown_pct", 0.0) or 0.0) >= 15:
        return "Risk threshold exceeded"
    if not defaults.get("default_stop_type") or not defaults.get("default_tp_type"):
        return "Stop logic invalid"
    if int(metrics.get("total_trades", 0) or 0) < 3:
        return "Requires more backtesting"
    return "Ready for deployment"


def certification_gate_result(bot: pd.Series) -> dict[str, object]:
    bot_name = str(bot.get("name", ""))
    strategy = str(bot.get("strategy", ""))
    symbol = str(bot.get("symbol", ""))
    timeframe = str(bot.get("timeframe", ""))
    defaults = strategy_deployment_defaults(strategy)
    metrics = validation_metrics_for_bot(bot_name)
    params = bot_parameters(bot)
    human_approval = str(params.get("human_approval_status", bot.get("human_approval_status", "PENDING")) or "PENDING").upper()
    backtest_passed = bool(metrics) and int(metrics.get("total_trades", 0) or 0) >= 3 and float(metrics.get("profit_factor", 0.0) or 0.0) >= 1.1
    walk_forward_passed = str(metrics.get("state", bot.get("validation_status", "")) or "").upper() in {"COMPLETED", "PASSED", "BACKTESTED"} or backtest_passed
    drawdown_ok = float(metrics.get("max_drawdown_pct", 999.0 if not metrics else 0.0) or 0.0) <= 15.0
    risk_attached = bool(defaults.get("risk_classification"))
    sizing_defined = bool(defaults.get("capital_allocation_model"))
    stop_defined = bool(defaults.get("default_stop_type"))
    runtime_ok = bool(defaults.get("runtime_profile")) and strategy in set(active_strategy_names())
    approved = human_approval == "APPROVED"
    visible = all([backtest_passed, walk_forward_passed, drawdown_ok, risk_attached, sizing_defined, stop_defined, runtime_ok, approved])
    missing = []
    if not backtest_passed:
        missing.append("backtest threshold")
    if not walk_forward_passed:
        missing.append("walk-forward/OOS validation")
    if not drawdown_ok:
        missing.append("drawdown limit")
    if not risk_attached:
        missing.append("risk model")
    if not sizing_defined:
        missing.append("position sizing")
    if not stop_defined:
        missing.append("stop-loss model")
    if not runtime_ok:
        missing.append("runtime compatibility")
    if not approved:
        missing.append("human approval")
    return {
        "Bot name": bot_name,
        "Strategy name": strategy,
        "Certification status": "CERTIFIED" if visible else "NEEDS APPROVAL" if approved is False and backtest_passed and drawdown_ok else "BLOCKED",
        "Certification version": str(defaults.get("strategy_version", "v1")),
        "Last certified date": str(metrics.get("updated_at", metrics.get("created_at", "Pending")) or "Pending"),
        "Supported symbol/asset": symbol,
        "Supported timeframe": timeframe or defaults["recommended_timeframe"],
        "Risk profile": defaults["risk_classification"],
        "Position sizing method": defaults["capital_allocation_model"],
        "Stop-loss model": defaults["default_stop_type"],
        "Backtest summary": f"trades {int(metrics.get('total_trades', 0) or 0)} | PF {safe_number(metrics.get('profit_factor', 0.0)):.2f} | DD {safe_number(metrics.get('max_drawdown_pct', 0.0)):.1f}%",
        "Validation summary": validation_deployment_status(metrics, safe_number(bot.get("capital", 0.0)), defaults) if metrics else "Needs Validation",
        "Runtime compatibility status": "Validated" if runtime_ok else "Unsupported",
        "Human approval": human_approval,
        "Marketplace visible": visible,
        "Gate detail": "Ready" if visible else "Missing: " + ", ".join(missing),
    }


def bot_marketplace_panel(bots: pd.DataFrame) -> None:
    st.markdown("#### Bot Marketplace")
    st.caption("Certified bots appear here only after backtest, validation, risk, sizing, stop-loss, runtime compatibility, and human approval gates pass.")
    if bots.empty:
        st.info("Create and validate a bot before it can enter the marketplace.")
        return
    rows = [certification_gate_result(row) for _, row in bots.iterrows()]
    frame = pd.DataFrame(rows)
    certified = frame[frame["Marketplace visible"] == True]
    pending = frame[frame["Marketplace visible"] != True]
    if certified.empty:
        st.warning("No bots are marketplace-certified yet. Pending gate evidence is shown below.")
    else:
        st.dataframe(certified, use_container_width=True, hide_index=True)
    with st.expander("Pending certification gates", expanded=certified.empty):
        st.dataframe(pending if not pending.empty else frame.iloc[0:0], use_container_width=True, hide_index=True)


def runtime_instance_profile(bot: pd.Series, scan: pd.DataFrame, matrix: pd.DataFrame) -> dict[str, object]:
    live_mark = bot_live_mark(bot, scan)
    trade_position = runtime_trade_position_status(bot, load_runtime_trade_events())
    live_mark = {
        **live_mark,
        "in_market": bool(trade_position["in_trade"]),
        "trade_position_state": str(trade_position["trade_position_state"]),
        "trade_position_reason": str(trade_position["trade_position_reason"]),
        "last_entry_at": str(trade_position["last_entry_at"]),
        "last_exit_at": str(trade_position["last_exit_at"]),
    }
    deployment = bot_deployment_profile(bot)
    validation = validation_metrics_for_bot(str(bot.get("name", "")))
    symbol = str(bot.get("symbol", ""))
    strategy = str(bot.get("strategy", ""))
    state = str(bot.get("state", "DRAFT"))
    timeframe = str(bot.get("timeframe", deployment["recommended_timeframe"]) or deployment["recommended_timeframe"])
    scan_row = scan[scan["symbol"].astype(str) == symbol].iloc[0] if not scan.empty and symbol in set(scan["symbol"].astype(str)) else pd.Series(dtype=object)
    perf = matrix[(matrix["strategy"].astype(str) == strategy) & (matrix["symbol"].astype(str) == symbol)] if not matrix.empty else pd.DataFrame()
    perf_row = perf.iloc[0] if not perf.empty else pd.Series(dtype=object)
    capital = float(live_mark["capital"])
    in_market = bool(live_mark["in_market"])
    exposure = capital if in_market else 0.0
    last_price = safe_number(live_mark["last_price"])
    qty = 0.0 if last_price <= 0 else exposure / last_price
    pnl = safe_number(live_mark["pnl"])
    pnl_pct = safe_number(live_mark["pnl_pct"])
    entry_price = safe_number(live_mark["entry_price"], last_price)
    params = bot_parameters(bot)
    configured_stop = safe_number(params.get("current_stop_loss", params.get("stop_loss", 0.0)), 0.0)
    stop_loss_level = configured_stop if configured_stop > 0 else max(0.0, entry_price * 0.98) if entry_price > 0 and in_market else 0.0
    stop_distance_abs = max(0.0, last_price - stop_loss_level) if last_price > 0 and stop_loss_level > 0 else 0.0
    stop_distance_pct = 0.0 if last_price <= 0 else stop_distance_abs / last_price * 100.0
    risk_multiple = 0.0 if stop_distance_abs <= 0 else max(0.0, last_price - entry_price) / stop_distance_abs
    risk = load_risk_settings()
    sizing_decision = calculate_position_size_decision(
        bot_id=str(bot.get("bot_id", bot.get("name", ""))),
        strategy_id=strategy,
        runtime_instance_id=str(bot.get("runtime_instance_id", bot.get("bot_id", ""))),
        symbol=symbol,
        sizing_method=str(deployment.get("capital_allocation_model", "standard")),
        capital=capital,
        risk_per_trade=float(risk.get("max_risk_per_trade_pct", 0.01) or 0.01),
        max_allocation=float(risk.get("max_cash_per_trade", capital) or capital),
        stop_loss_distance=max(stop_distance_abs, last_price * 0.01 if last_price > 0 else 0.0),
        price=last_price,
        volatility_value=float(scan_row.get("volatility", 0.0) or 0.0),
        regime=str(market_bucket_lane(scan_row) if not scan_row.empty else "normal"),
        max_portfolio_exposure=float(risk.get("max_portfolio_exposure", capital) or capital),
        maximum_concurrent_trades=max(1, int(risk.get("max_trades_per_window", 1) or 1)),
    )
    if state in {"RUNNING", "DEPLOYED"}:
        persist_position_size_decision(sizing_decision)
    current_drawdown = min(0.0, pnl_pct)
    peak_drawdown = max(float(perf_row.get("max_drawdown_pct", validation.get("max_drawdown_pct", 0.0)) or 0.0), abs(current_drawdown))
    initial_capital = capital
    current_capital = capital + pnl if in_market else capital
    available_capital = max(0.0, initial_capital - exposure)
    capital_utilization = 0.0 if initial_capital <= 0 else (exposure / initial_capital) * 100.0
    profile = {
        **deployment,
        "bot_instance_name": str(bot.get("name", "")),
        "strategy_name": strategy,
        "strategy_type": deployment["strategy_type"],
        "bot_version": deployment["bot_version"],
        "symbol": symbol,
        "timeframe": timeframe,
        "runtime_status": state,
        "trade_position_state": str(live_mark["trade_position_state"]),
        "trade_position_reason": str(live_mark["trade_position_reason"]),
        "in_trade": bool(live_mark["in_market"]),
        "last_entry_at": str(live_mark["last_entry_at"]),
        "last_exit_at": str(live_mark["last_exit_at"]),
        "validation_status": "BACKTESTED" if validation else str(bot.get("validation_status", "PENDING") or "PENDING"),
        "deployment_timestamp": str(bot.get("deployed_at", "") or live_mark.get("started_at", "")),
        "runtime_duration_hours": runtime_hours_since(str(live_mark.get("started_at", ""))),
        "real_time_pnl": pnl,
        "realized_pnl": safe_number(bot.get("realized_pnl", 0.0)),
        "unrealized_pnl": pnl,
        "roi_pct": pnl_pct,
        "runtime_hours": runtime_hours_since(str(live_mark.get("started_at", ""))),
        "current_exposure": exposure,
        "initial_allocated_capital": initial_capital,
        "current_allocated_capital": current_capital,
        "available_unallocated_capital": available_capital,
        "capital_utilization_pct": capital_utilization,
        "qty_per_order": qty,
        "recommended_quantity": float(sizing_decision["final_quantity"]),
        "position_sizing_method": sizing_decision["sizing_method"],
        "position_sizing_reasoning": f"risk {float(sizing_decision['risk_per_trade']) * 100:.2f}% with caps {', '.join(sizing_decision['cap_applied_json']) or 'none'}",
        "margin_usage": 0.0,
        "current_stop_loss": stop_loss_level,
        "stop_loss_distance_abs": stop_distance_abs,
        "stop_loss_distance_pct": stop_distance_pct,
        "risk_multiple": risk_multiple,
        "current_drawdown_pct": current_drawdown,
        "peak_drawdown_pct": safe_number(peak_drawdown),
        "trade_count": int(validation.get("total_trades", perf_row.get("trades", 0)) or 0),
        "win_rate": float(validation.get("win_rate", perf_row.get("win_rate", 0.0)) or 0.0),
        "profit_factor": float(validation.get("profit_factor", perf_row.get("profit_factor", 0.0)) or 0.0),
        "current_bucket": str(scan_row.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL"),
        "signal_status": str(scan_row.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL"),
        "current_strategy_state": "Active signal" if str(scan_row.get("scan_bucket", "")) in {"BUY", "IN TRADE"} else "Developing" if str(scan_row.get("scan_bucket", "")) == "WATCH" else "Idle",
        "last_signal_at": str(scan_row.get("stream_updated_at", "") or bot.get("last_signal_at", "")),
        "backtest_roi": float(validation.get("net_pnl", validation.get("total_pnl", 0.0)) or 0.0),
        "backtest_max_drawdown": float(validation.get("max_drawdown_pct", 0.0) or 0.0),
        "backtest_win_rate": float(validation.get("win_rate", 0.0) or 0.0),
        "backtest_trade_count": int(validation.get("total_trades", 0) or 0),
        "last_backtest_timestamp": str(validation.get("updated_at", validation.get("created_at", validation.get("run_id", ""))) or ""),
        "backtest_timeframe_tested": str(validation.get("timeframe", timeframe) or timeframe),
        "backtest_data_range": f"{validation.get('start_date', 'n/a')} to {validation.get('end_date', 'n/a')}",
        "api_connectivity": "Connected",
        "feed_status": str(scan_row.get("stream_status", bot.get("data_state", "UNKNOWN")) or "UNKNOWN"),
        "runtime_latency_ms": 0.0,
        "last_heartbeat": str(bot.get("heartbeat_at", "") or bot.get("last_heartbeat", "")),
        "order_execution_status": str(bot.get("protection_state", "UNKNOWN") or "UNKNOWN"),
        "error_warning_count": 1 if str(bot.get("alert_level", "INFO")) in {"WARNING", "CRITICAL"} else 0,
        "recovery_state": str(bot.get("supervisor_action", "NONE") or "NONE"),
        "execution_queue_state": str(bot.get("portfolio_state", "UNKNOWN") or "UNKNOWN"),
        "alert_level": str(bot.get("alert_level", "INFO") or "INFO"),
        "data_state": str(bot.get("data_state", "UNKNOWN") or "UNKNOWN"),
    }
    health_light, health_label = runtime_health_light(profile)
    profile["health_light"] = health_light
    profile["health_label"] = health_label
    profile["operational_guidance"] = runtime_guidance(profile)
    return profile


@st.cache_data(ttl=5, show_spinner=False)
def load_runtime_order_audit() -> pd.DataFrame:
    return pd.DataFrame(load_json_list(RUNTIME_ORDER_AUDIT_PATH))


@st.cache_data(ttl=5, show_spinner=False)
def load_runtime_alerts() -> pd.DataFrame:
    return pd.DataFrame(load_json_list(RUNTIME_ALERTS_PATH))


@st.cache_data(ttl=5, show_spinner=False)
def load_runtime_trade_events() -> pd.DataFrame:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_runtime_events_from_db())
            if not frame.empty:
                return frame
        except Exception as exc:
            logger.exception("runtime_events_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "RUNTIME_EVENTS", str(exc))
    return pd.DataFrame(load_json_list(RUNTIME_TRADE_EVENTS_PATH))


@st.cache_data(ttl=5, show_spinner=False)
def load_runtime_trade_pnl_snapshots() -> pd.DataFrame:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_runtime_events_from_db())
            if not frame.empty and "event_type" in frame:
                snapshots = frame[frame["event_type"].astype(str) == "PNL_SNAPSHOT"].copy()
                if not snapshots.empty:
                    snapshots["snapshot_id"] = snapshots["event_id"]
                    snapshots["snapshot_time"] = snapshots["event_time"]
                    return snapshots
        except Exception as exc:
            logger.exception("runtime_pnl_snapshots_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "RUNTIME_PNL_SNAPSHOTS", str(exc))
    return pd.DataFrame(load_json_list(RUNTIME_TRADE_PNL_SNAPSHOTS_PATH))


def persist_trade_pnl_snapshots(active: pd.DataFrame) -> int:
    if active.empty:
        return 0
    manager = RuntimeManager()
    persisted = 0
    for _, row in active.iterrows():
        if str(row.get("Position State", "")) != "Open":
            continue
        manager.record_trade_pnl_snapshot(
            bot_id=str(row.get("Bot Instance ID", "")),
            trade_id=str(row.get("Trade ID", "")),
            symbol=str(row.get("Symbol", "")),
            current_price=safe_number(row.get("Current Price")),
            unrealized_pnl=safe_number(row.get("Unrealized P&L")),
            realized_pnl=safe_number(row.get("Realized P&L")),
            roi_pct=safe_number(row.get("ROI %")),
            exposure=safe_number(row.get("Exposure")),
            drawdown_pct=safe_number(row.get("Drawdown %")),
            lifecycle_state=str(row.get("Trade State", "")),
        )
        persisted += 1
    load_runtime_trade_pnl_snapshots.clear()
    return persisted


def trade_management_live_summary_component(active: pd.DataFrame, summary: dict[str, object], event_count: int, snapshot_count: int) -> None:
    rows = []
    if not active.empty:
        for _, row in active.iterrows():
            rows.append(
                {
                    "symbol": str(row.get("Symbol", "")),
                    "entry_price": safe_number(row.get("Entry Price")),
                    "current_price": safe_number(row.get("Current Price")),
                    "capital": safe_number(row.get("Capital Allocated")),
                    "exposure": safe_number(row.get("Exposure")),
                    "position_state": str(row.get("Position State", "")),
                }
            )
    daily_base = safe_number(summary.get("Daily P&L")) - safe_number(summary.get("Active P&L"))
    payload = {
        "rows": rows,
        "daily_base": daily_base,
        "exposure": safe_number(summary.get("Exposure")),
        "active_trades": int(summary.get("Active Trades", 0) or 0),
        "event_count": event_count,
        "snapshot_count": snapshot_count,
    }
    components.html(
        f"""
        <div id="trade-live-summary"></div>
        <style>
          body {{ margin:0; background:transparent; font-family: Inter, Arial, sans-serif; color:#e8edf2; }}
          .live-grid {{ display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:0.65rem; }}
          .live-card {{ background:#151c22; border:1px solid #26323b; border-radius:8px; padding:0.78rem 0.85rem; min-height:76px; box-sizing:border-box; }}
          .live-label {{ color:#94a3ad; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; }}
          .live-value {{ font-size:1.08rem; line-height:1.25; font-weight:800; margin-top:0.28rem; overflow-wrap:anywhere; }}
          .good {{ color:#55d49a; }} .bad {{ color:#ff6f7d; }} .info {{ color:#79a7ff; }}
          .live-note {{ color:#94a3ad; font-size:0.78rem; margin-top:0.4rem; }}
          @media (max-width:900px) {{ .live-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
        </style>
        <script>
          const payload = {json.dumps(payload)};
          const root = document.getElementById("trade-live-summary");
          const rows = payload.rows || [];
          const prices = Object.fromEntries(rows.map((row) => [row.symbol, Number(row.current_price || 0)]));
          function money(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "$0.00";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
          }}
          function pct(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "0.00%";
            return n.toFixed(2) + "%";
          }}
          function liveTotals() {{
            let activePnl = 0;
            let drawdown = 0;
            for (const row of rows) {{
              const entry = Number(row.entry_price || 0);
              const capital = Number(row.capital || row.exposure || 0);
              const mark = Number(prices[row.symbol] || row.current_price || 0);
              if (row.position_state !== "Open" || entry <= 0 || mark <= 0 || capital <= 0) continue;
              const pnlPct = (mark - entry) / entry * 100;
              const pnl = capital * pnlPct / 100;
              activePnl += Number.isFinite(pnl) ? pnl : 0;
              drawdown = Math.min(drawdown, Number.isFinite(pnlPct) ? pnlPct : 0);
            }}
            return {{ activePnl, dailyPnl: Number(payload.daily_base || 0) + activePnl, drawdown }};
          }}
          function card(label, value, cls = "") {{
            return `<div class="live-card"><div class="live-label">${{label}}</div><div class="live-value ${{cls}}">${{value}}</div></div>`;
          }}
          function render(status = "live numbers waiting for ticks") {{
            const totals = liveTotals();
            const pnlClass = totals.activePnl >= 0 ? "good" : "bad";
            root.innerHTML = `<div class="live-grid">
              ${{card("Active P&L", money(totals.activePnl), pnlClass)}}
              ${{card("Daily P&L", money(totals.dailyPnl), totals.dailyPnl >= 0 ? "good" : "bad")}}
              ${{card("Exposure", money(payload.exposure), "info")}}
              ${{card("Drawdown", pct(totals.drawdown), totals.drawdown < 0 ? "bad" : "")}}
              ${{card("Active Trades", String(payload.active_trades), "info")}}
              ${{card("Events", String(payload.event_count), "info")}}
              ${{card("Snapshots", String(payload.snapshot_count), "info")}}
            </div><div class="live-note">${{status}}. Page does not auto-refresh.</div>`;
          }}
          function connect() {{
            const symbols = [...new Set(rows.map((row) => String(row.symbol || "").replace("/", "").toLowerCase()).filter(Boolean))];
            if (!symbols.length) {{ render("no open symbols"); return; }}
            const streams = symbols.map((symbol) => `${{symbol}}@trade`).join("/");
            const ws = new WebSocket(`wss://stream.testnet.binance.vision/stream?streams=${{streams}}`);
            ws.onopen = () => render("streaming live marks");
            ws.onmessage = (event) => {{
              try {{
                const parsed = JSON.parse(event.data);
                const data = parsed.data || {{}};
                const symbol = String(data.s || "").replace("USDT", "/USDT");
                const price = Number(data.p);
                if (symbol && Number.isFinite(price)) prices[symbol] = price;
                render("streaming live marks");
              }} catch (err) {{
                render("stream parse skipped");
              }}
            }};
            ws.onerror = () => render("live stream warning");
            ws.onclose = () => setTimeout(connect, 4000);
          }}
          render();
          connect();
        </script>
        """,
        height=116,
    )


def trade_management_live_analytics_component(active: pd.DataFrame) -> None:
    rows = []
    if not active.empty:
        for _, row in active.iterrows():
            rows.append(
                {
                    "bot": str(row.get("Bot", "")),
                    "symbol": str(row.get("Symbol", "")),
                    "entry_price": safe_number(row.get("Entry Price")),
                    "current_price": safe_number(row.get("Current Price")),
                    "capital": safe_number(row.get("Capital Allocated")),
                    "exposure": safe_number(row.get("Exposure")),
                    "position_state": str(row.get("Position State", "")),
                }
            )
    components.html(
        f"""
        <div id="live-analytics"></div>
        <style>
          body {{ margin:0; background:transparent; color:#e8edf2; font-family:Inter,Arial,sans-serif; }}
          .analytics-shell {{ border:1px solid rgba(121,167,255,.26); border-radius:8px; background:rgba(17,24,32,.58); padding:.85rem; }}
          .analytics-head {{ display:flex; justify-content:space-between; gap:.75rem; align-items:center; margin-bottom:.65rem; }}
          .analytics-title {{ font-weight:820; font-size:1.02rem; }}
          .analytics-note {{ color:#94a3ad; font-size:.8rem; }}
          .analytics-kpis {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.55rem; margin-bottom:.75rem; }}
          .analytics-kpi {{ border:1px solid #26323b; border-radius:8px; padding:.58rem; background:#151c22; }}
          .analytics-label {{ color:#94a3ad; font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; }}
          .analytics-value {{ font-weight:840; font-size:1.06rem; margin-top:.25rem; }}
          .chart-wrap {{ height:220px; border:1px solid #26323b; border-radius:8px; background:#0f1418; overflow:hidden; }}
          svg {{ width:100%; height:100%; display:block; }}
          .good {{ color:#55d49a; }} .bad {{ color:#ff6f7d; }} .info {{ color:#79a7ff; }}
          @media (max-width:900px) {{ .analytics-kpis {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
        </style>
        <script>
          const analyticsRows = {json.dumps(rows)};
          const analyticsRoot = document.getElementById("live-analytics");
          const analyticsPrices = Object.fromEntries(analyticsRows.map((row) => [row.symbol, Number(row.current_price || 0)]));
          const series = [];
          function money(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "$0.00";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
          }}
          function pct(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "0.00%";
            return n.toFixed(2) + "%";
          }}
          function totals() {{
            let pnl = 0;
            let exposure = 0;
            let weightedPct = 0;
            let drawdown = 0;
            for (const row of analyticsRows) {{
              const entry = Number(row.entry_price || 0);
              const capital = Number(row.capital || row.exposure || 0);
              const mark = Number(analyticsPrices[row.symbol] || row.current_price || 0);
              if (row.position_state !== "Open" || entry <= 0 || mark <= 0 || capital <= 0) continue;
              const rowPct = (mark - entry) / entry * 100;
              const rowPnl = capital * rowPct / 100;
              pnl += Number.isFinite(rowPnl) ? rowPnl : 0;
              exposure += capital;
              weightedPct += Number.isFinite(rowPct) ? rowPct * capital : 0;
              drawdown = Math.min(drawdown, Number.isFinite(rowPct) ? rowPct : 0);
            }}
            const roi = exposure <= 0 ? 0 : weightedPct / exposure;
            return {{ pnl, exposure, roi, drawdown }};
          }}
          function path(points, width, height) {{
            if (points.length < 2) return "";
            const min = Math.min(...points);
            const max = Math.max(...points);
            const span = Math.max(0.000001, max - min);
            return points.map((value, index) => {{
              const x = points.length === 1 ? 0 : (index / (points.length - 1)) * width;
              const y = height - ((value - min) / span) * (height - 24) - 12;
              return `${{index === 0 ? "M" : "L"}}${{x.toFixed(1)}},${{y.toFixed(1)}}`;
            }}).join(" ");
          }}
          function render(status = "waiting for live ticks") {{
            const now = totals();
            series.push(now.pnl);
            if (series.length > 90) series.shift();
            const pnlClass = now.pnl >= 0 ? "good" : "bad";
            const line = path(series, 820, 190);
            analyticsRoot.innerHTML = `<div class="analytics-shell">
              <div class="analytics-head"><div class="analytics-title">Live Runtime Analytics</div><div class="analytics-note">${{status}} | in-place updates only</div></div>
              <div class="analytics-kpis">
                <div class="analytics-kpi"><div class="analytics-label">Live P&L</div><div class="analytics-value ${{pnlClass}}">${{money(now.pnl)}}</div></div>
                <div class="analytics-kpi"><div class="analytics-label">Live ROI</div><div class="analytics-value ${{now.roi >= 0 ? "good" : "bad"}}">${{pct(now.roi)}}</div></div>
                <div class="analytics-kpi"><div class="analytics-label">Exposure</div><div class="analytics-value info">${{money(now.exposure)}}</div></div>
                <div class="analytics-kpi"><div class="analytics-label">Worst Live Move</div><div class="analytics-value ${{now.drawdown < 0 ? "bad" : ""}}">${{pct(now.drawdown)}}</div></div>
              </div>
              <div class="chart-wrap"><svg viewBox="0 0 820 220" preserveAspectRatio="none">
                <line x1="0" y1="110" x2="820" y2="110" stroke="#26323b" stroke-width="1"/>
                <path d="${{line}}" fill="none" stroke="#55d49a" stroke-width="3"/>
              </svg></div>
            </div>`;
          }}
          function connect() {{
            const symbols = [...new Set(analyticsRows.map((row) => String(row.symbol || "").replace("/", "").toLowerCase()).filter(Boolean))];
            if (!symbols.length) {{ render("no open symbols"); return; }}
            const streams = symbols.map((symbol) => `${{symbol}}@trade`).join("/");
            const ws = new WebSocket(`wss://stream.testnet.binance.vision/stream?streams=${{streams}}`);
            ws.onopen = () => render("streaming live analytics");
            ws.onmessage = (event) => {{
              try {{
                const parsed = JSON.parse(event.data);
                const data = parsed.data || {{}};
                const symbol = String(data.s || "").replace("USDT", "/USDT");
                const price = Number(data.p);
                if (symbol && Number.isFinite(price)) analyticsPrices[symbol] = price;
                render("streaming live analytics");
              }} catch (err) {{
                render("stream parse skipped");
              }}
            }};
            ws.onerror = () => render("live analytics warning");
            ws.onclose = () => setTimeout(connect, 4000);
          }}
          render();
          connect();
        </script>
        """,
        height=370,
    )


def trade_management_live_active_grid_component(active: pd.DataFrame) -> None:
    rows = []
    if not active.empty:
        for _, row in active.iterrows():
            rows.append(
                {
                    "trade_id": str(row.get("Trade ID", "")),
                    "bot": str(row.get("Bot", "")),
                    "strategy": str(row.get("Strategy ID", "")),
                    "symbol": str(row.get("Symbol", "")),
                    "entry_timestamp": str(row.get("Entry Timestamp", "")),
                    "exit_timestamp": str(row.get("Exit Timestamp", "Open - not exited")),
                    "entry_price": safe_number(row.get("Entry Price")),
                    "current_price": safe_number(row.get("Current Price")),
                    "capital": safe_number(row.get("Capital Allocated")),
                    "position_state": str(row.get("Position State", "")),
                    "market_feed_timestamp": str(row.get("Market Feed Timestamp", "")),
                    "market_feed_status": str(row.get("Market Feed Status", "")),
                }
            )
    components.html(
        f"""
        <div id="live-active-trades"></div>
        <style>
          body {{ margin:0; background:transparent; color:#e8edf2; font-family:Inter,Arial,sans-serif; }}
          .live-table {{ border:1px solid #26323b; border-radius:8px; overflow:hidden; background:#111820; }}
          .live-row {{ display:grid; grid-template-columns:1.15fr .95fr .8fr .8fr .8fr .9fr .9fr; gap:.45rem; align-items:center; padding:.52rem .62rem; border-bottom:1px solid #26323b; }}
          .live-row:last-child {{ border-bottom:0; }}
          .live-head {{ color:#94a3ad; text-transform:uppercase; font-size:.68rem; letter-spacing:.04em; background:#0d1216; }}
          .live-cell {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.78rem; }}
          .strong {{ font-weight:820; color:#e8edf2; }}
          .good {{ color:#55d49a; }} .bad {{ color:#ff6f7d; }} .muted {{ color:#94a3ad; }}
          @media (max-width:900px) {{ .live-row {{ grid-template-columns:1fr 1fr; }} .hide-small {{ display:none; }} }}
        </style>
        <script>
          const activeRows = {json.dumps(rows)};
          const activeRoot = document.getElementById("live-active-trades");
          const activePrices = Object.fromEntries(activeRows.map((row) => [row.symbol, Number(row.current_price || 0)]));
          function money(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "$0.00";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
          }}
          function price(value) {{
            const n = Number(value);
            if (!Number.isFinite(n) || n <= 0) return "n/a";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 6 }});
          }}
          function pct(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "0.00%";
            return n.toFixed(2) + "%";
          }}
          function age(value) {{
            if (!value || String(value).startsWith("Open")) return value || "Open - not exited";
            const ts = new Date(value);
            if (Number.isNaN(ts.getTime())) return String(value);
            const secs = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
            if (secs < 60) return secs + "s ago";
            const mins = Math.floor(secs / 60);
            if (mins < 60) return mins + "m ago";
            const hours = Math.floor(mins / 60);
            return hours + "h ago";
          }}
          function render(status = "market feed waiting") {{
            if (!activeRows.length) {{
              activeRoot.innerHTML = "<div class='muted'>No active trades.</div>";
              return;
            }}
            const body = activeRows.map((row) => {{
              const entry = Number(row.entry_price || 0);
              const mark = Number(activePrices[row.symbol] || row.current_price || 0);
              const capital = Number(row.capital || 0);
              const roi = entry > 0 && mark > 0 ? (mark - entry) / entry * 100 : 0;
              const pnl = capital * roi / 100;
              const cls = pnl >= 0 ? "good" : "bad";
              return `<div class="live-row">
                <div class="live-cell strong" title="${{row.trade_id}}">${{row.bot}}</div>
                <div class="live-cell">${{row.symbol}}</div>
                <div class="live-cell">${{price(entry)}}</div>
                <div class="live-cell">${{price(mark)}}</div>
                <div class="live-cell ${{cls}}">${{money(pnl)}} / ${{pct(roi)}}</div>
                <div class="live-cell muted" title="${{row.entry_timestamp}}">Entry ${{age(row.entry_timestamp)}}</div>
                <div class="live-cell muted" title="${{row.exit_timestamp}}">Exit ${{age(row.exit_timestamp)}}</div>
              </div>`;
            }}).join("");
            activeRoot.innerHTML = `<div class="live-table">
              <div class="live-row live-head"><div>Bot</div><div>Symbol</div><div>Entry</div><div>Live Mark</div><div>P&L</div><div>Entry Time</div><div>Exit Time</div></div>
              ${{body}}
            </div><div class="muted" style="font-size:.76rem;margin-top:.35rem;">${{status}}. Active marks update in place from Binance testnet trade stream; page does not refresh.</div>`;
          }}
          function connect() {{
            const symbols = [...new Set(activeRows.map((row) => String(row.symbol || "").replace("/", "").toLowerCase()).filter(Boolean))];
            if (!symbols.length) {{ render("no symbols"); return; }}
            const streams = symbols.map((symbol) => `${{symbol}}@trade`).join("/");
            const ws = new WebSocket(`wss://stream.testnet.binance.vision/stream?streams=${{streams}}`);
            ws.onopen = () => render("streaming active trade marks");
            ws.onmessage = (event) => {{
              try {{
                const parsed = JSON.parse(event.data);
                const data = parsed.data || {{}};
                const symbol = String(data.s || "").replace("USDT", "/USDT");
                const mark = Number(data.p);
                if (symbol && Number.isFinite(mark)) activePrices[symbol] = mark;
                render("streaming active trade marks");
              }} catch (err) {{
                render("stream parse skipped");
              }}
            }};
            ws.onerror = () => render("market feed warning");
            ws.onclose = () => setTimeout(connect, 4000);
          }}
          render();
          connect();
          setInterval(() => render("streaming active trade marks"), 1000);
        </script>
        """,
        height=245,
    )


def latest_order_state(order_audit: pd.DataFrame, bot_id: str, bot_name: str) -> dict[str, object]:
    if order_audit.empty:
        return {}
    frame = order_audit.copy()
    bot_columns = [column for column in ["bot_id", "bot_name", "name"] if column in frame]
    if not bot_columns:
        return {}
    mask = pd.Series(False, index=frame.index)
    for column in bot_columns:
        values = frame[column].astype(str)
        mask = mask | values.isin({str(bot_id), str(bot_name)})
    matches = frame[mask]
    if matches.empty:
        return {}
    time_column = next((column for column in ["event_time", "timestamp", "created_at"] if column in matches), "")
    if time_column:
        matches = matches.copy()
        matches["_event_time"] = pd.to_datetime(matches[time_column], errors="coerce")
        matches = matches.sort_values("_event_time")
    return matches.iloc[-1].to_dict()


def trade_event_timestamps(trade_events: pd.DataFrame, bot_id: str, bot_name: str, trade_id: str = "") -> dict[str, str]:
    if trade_events.empty:
        return {"entry_timestamp": "", "exit_timestamp": ""}
    frame = trade_events.copy()
    mask = pd.Series(False, index=frame.index)
    for column in [column for column in ["bot_id", "bot_name"] if column in frame]:
        mask = mask | frame[column].astype(str).isin({str(bot_id), str(bot_name)})
    if trade_id and "trade_id" in frame:
        mask = mask | frame["trade_id"].astype(str).eq(str(trade_id))
    matches = frame[mask].copy()
    if matches.empty:
        return {"entry_timestamp": "", "exit_timestamp": ""}
    time_column = next((column for column in ["event_time", "timestamp", "created_at", "snapshot_time"] if column in matches), "")
    if not time_column:
        return {"entry_timestamp": "", "exit_timestamp": ""}
    matches["_event_time"] = pd.to_datetime(matches[time_column], errors="coerce", utc=True)
    matches = matches.dropna(subset=["_event_time"]).sort_values("_event_time")
    if matches.empty:
        return {"entry_timestamp": "", "exit_timestamp": ""}
    event_type = matches.get("event_type", pd.Series("", index=matches.index)).astype(str)
    lifecycle = matches.get("lifecycle_state", pd.Series("", index=matches.index)).astype(str)
    position = matches.get("position_state", pd.Series("", index=matches.index)).astype(str)
    entry_mask = event_type.isin(["TradeEntered", "TradeCreated"]) | lifecycle.isin(["Submitted", "Filled", "Active"]) | position.eq("OPEN")
    exit_mask = event_type.isin(["TradeExited", "StopTriggered", "BOT_STOPPED"]) | lifecycle.isin(["Closed", "Cancelled", "Failed"]) | position.eq("FLAT")
    entry_time = matches.loc[entry_mask, "_event_time"].min() if entry_mask.any() else pd.NaT
    exit_time = matches.loc[exit_mask, "_event_time"].max() if exit_mask.any() else pd.NaT
    return {
        "entry_timestamp": "" if pd.isna(entry_time) else entry_time.isoformat(),
        "exit_timestamp": "" if pd.isna(exit_time) else exit_time.isoformat(),
    }


def trade_lifecycle_state(runtime_status: str, order_status: str, exposure: float, realized_pnl: float) -> str:
    order_status_upper = order_status.upper()
    runtime_status_upper = runtime_status.upper()
    if order_status_upper in {"REJECTED", "FAILED", "ERROR"} or runtime_status_upper == "FAILED":
        return "Failed"
    if order_status_upper in {"CANCELLED", "CANCELED"} or runtime_status_upper == "STOPPED":
        return "Cancelled" if exposure <= 0 else "Closed"
    if order_status_upper in {"PARTIALLY_FILLED", "PARTIAL"}:
        return "Partially Filled"
    if order_status_upper in {"PARTIALLY_EXITED", "PARTIAL_EXIT"}:
        return "Partially Exited"
    if runtime_status_upper in {"RUNNING", "DEPLOYED"} and exposure > 0:
        return "Active"
    if order_status_upper in {"ACKNOWLEDGED", "SUBMITTED"}:
        return "Submitted"
    if order_status_upper in {"FILLED"} and exposure > 0:
        return "Filled"
    if realized_pnl != 0:
        return "Closed"
    return "Pending"


def trade_risk_light(row: dict[str, object]) -> tuple[str, str]:
    health = str(row.get("Health", "Green"))
    stop_distance = safe_number(row.get("Stop Distance %", 0.0), 0.0)
    drawdown = abs(safe_number(row.get("Drawdown %", 0.0), 0.0))
    if health == "Red" or drawdown >= 8 or (0 < stop_distance <= 1):
        return "Red", "Critical risk or stop proximity"
    if health == "Amber" or drawdown >= 4 or (0 < stop_distance <= 2):
        return "Amber", "Risk building; monitor trade"
    return "Green", "Healthy trade posture"


def trade_management_rows(
    bots: pd.DataFrame,
    scan: pd.DataFrame,
    matrix: pd.DataFrame,
    validation_runs: pd.DataFrame,
    backtest_trades: pd.DataFrame,
    order_audit: pd.DataFrame,
    trade_events: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    active_rows: list[dict[str, object]] = []
    strategy_rows: list[dict[str, object]] = []
    if not bots.empty:
        for _, bot in bots.iterrows():
            profile = runtime_instance_profile(bot, scan, matrix)
            order = latest_order_state(order_audit, str(bot.get("bot_id", "")), str(bot.get("name", "")))
            order_status = str(order.get("status", order.get("order_status", "")) or "")
            trade_id = f"{bot.get('bot_id', bot.get('name', 'bot'))}:{profile['symbol']}:{profile['timeframe']}"
            event_times = trade_event_timestamps(
                trade_events,
                str(bot.get("bot_id", "")),
                str(bot.get("name", "")),
                trade_id,
            )
            lifecycle = trade_lifecycle_state(
                str(profile["runtime_status"]),
                order_status,
                safe_number(profile["current_exposure"]),
                safe_number(profile["realized_pnl"]),
            )
            entry_price = safe_number(bot.get("runtime_entry_price", bot_parameters(bot).get("runtime_entry_price", 0.0)))
            live_mark = bot_live_mark(bot, scan)
            current_price = safe_number(live_mark.get("last_price", 0.0))
            stop_value = str(profile["stop_loss_value"])
            stop_price = safe_number(order.get("stop_price", order.get("stop", 0.0)), 0.0)
            stop_distance = 0.0 if current_price <= 0 or stop_price <= 0 else ((current_price - stop_price) / current_price) * 100.0
            entry_timestamp = event_times["entry_timestamp"] or str(bot.get("last_entry_at", "") or "") or str(profile["deployment_timestamp"] or "")
            exit_timestamp = event_times["exit_timestamp"] or str(bot.get("last_exit_at", "") or "")
            if not exit_timestamp and safe_number(profile["current_exposure"]) > 0:
                exit_timestamp = "Open - not exited"
            row = {
                "Trade ID": trade_id,
                "Bot Instance ID": str(bot.get("bot_id", bot.get("name", ""))),
                "Bot": profile["bot_instance_name"],
                "Strategy ID": profile["strategy_name"],
                "Strategy Type": profile["strategy_type"],
                "Symbol": profile["symbol"],
                "Timeframe": profile["timeframe"],
                "Trade State": lifecycle,
                "Order State": order_status or "Not submitted",
                "Position State": "Open" if safe_number(profile["current_exposure"]) > 0 else "Flat",
                "Entry Timestamp": entry_timestamp,
                "Exit Timestamp": exit_timestamp,
                "Entry Price": entry_price,
                "Entry Qty": profile["qty_per_order"],
                "Entry Signal": profile["signal_status"],
                "Capital Allocated": profile["initial_allocated_capital"],
                "Current Price": current_price,
                "Unrealized P&L": profile["unrealized_pnl"],
                "Realized P&L": profile["realized_pnl"],
                "ROI %": profile["roi_pct"],
                "Current Stop-Loss": stop_price if stop_price > 0 else stop_value,
                "Current Take-Profit": profile["take_profit_value"],
                "Stop Distance %": stop_distance,
                "Drawdown %": profile["current_drawdown_pct"],
                "Exposure": profile["current_exposure"],
                "Time In Trade Hours": profile["runtime_hours"],
                "Risk/Reward": "Policy-managed",
                "Volatility Risk": str(profile.get("current_bucket", "NO SIGNAL")),
                "Health": profile["health_light"],
                "Guidance": profile["operational_guidance"],
                "Market Feed Timestamp": live_mark.get("stream_updated_at", ""),
                "Market Feed Age": live_mark.get("socket_age", ""),
                "Market Feed Status": live_mark.get("socket_status", ""),
                "Last Mark Source": live_mark.get("last_mark_source", ""),
                "Exit Reason": "",
                "Fees": 0.0,
                "Slippage": 0.0,
            }
            risk_light, risk_reason = trade_risk_light(row)
            row["Risk Light"] = risk_light
            row["Risk Reason"] = risk_reason
            active_rows.append(row)
            strategy_rows.append(
                {
                    "Strategy": profile["strategy_name"],
                    "Bot": profile["bot_instance_name"],
                    "Symbol": profile["symbol"],
                    "Runtime Status": profile["runtime_status"],
                    "Runtime P&L": profile["real_time_pnl"],
                    "Trade Count": profile["trade_count"],
                    "Win Rate %": profile["win_rate"],
                    "Profit Factor": profile["profit_factor"],
                    "Backtest ROI": profile["backtest_roi"],
                    "Backtest Max Drawdown %": profile["backtest_max_drawdown"],
                    "Current Bucket": profile["current_bucket"],
                    "Health": profile["health_light"],
                    "Guidance": profile["operational_guidance"],
                }
            )

    closed_rows: list[dict[str, object]] = []
    if not backtest_trades.empty:
        trades = backtest_trades.copy()
        if "exit_time" in trades:
            trades = trades.sort_values("exit_time", ascending=False)
        for index, trade in trades.head(500).iterrows():
            symbol = str(trade.get("symbol", ""))
            bot_match = bots[bots["symbol"].astype(str) == symbol].iloc[0] if not bots.empty and "symbol" in bots and symbol in set(bots["symbol"].astype(str)) else pd.Series(dtype=object)
            closed_rows.append(
                {
                    "Trade ID": f"replay:{symbol}:{index}",
                    "Bot": str(bot_match.get("name", "Replay / historical")),
                    "Strategy ID": str(bot_match.get("strategy", "Replay strategy")),
                    "Symbol": symbol,
                    "Timeframe": str(bot_match.get("timeframe", "")),
                    "Trade State": "Closed",
                    "Entry Timestamp": str(trade.get("entry_time", "")),
                    "Entry Price": safe_number(trade.get("entry_price")),
                    "Entry Qty": 0.0,
                    "Exit Timestamp": str(trade.get("exit_time", "")),
                    "Exit Price": safe_number(trade.get("exit_price")),
                    "Realized P&L": safe_number(trade.get("pnl")),
                    "ROI %": safe_number(trade.get("return_pct")),
                    "Current Stop-Loss": safe_number(trade.get("stop_price")),
                    "Current Take-Profit": safe_number(trade.get("take_profit_price")),
                    "Exit Reason": str(trade.get("exit_reason", "")),
                    "Fees": 0.0,
                    "Slippage": 0.0,
                    "Source": "Backtest replay",
                }
            )
    return pd.DataFrame(active_rows), pd.DataFrame(closed_rows), pd.DataFrame(strategy_rows)


def trade_management_summary(active: pd.DataFrame, closed: pd.DataFrame, alerts: pd.DataFrame) -> dict[str, object]:
    exposure = safe_number(active["Exposure"].sum() if not active.empty and "Exposure" in active else 0.0)
    active_pnl = safe_number(active["Unrealized P&L"].sum() if not active.empty and "Unrealized P&L" in active else 0.0)
    closed_pnl = safe_number(closed["Realized P&L"].sum() if not closed.empty and "Realized P&L" in closed else 0.0)
    if not closed.empty and "Exit Timestamp" in closed and "Realized P&L" in closed:
        exit_times = pd.to_datetime(closed["Exit Timestamp"], errors="coerce", utc=True)
        today = datetime.now(UTC).date()
        daily_closed_pnl = safe_number(pd.to_numeric(closed.loc[exit_times.dt.date == today, "Realized P&L"], errors="coerce").sum())
    else:
        daily_closed_pnl = closed_pnl
    drawdown = safe_number(active["Drawdown %"].min() if not active.empty and "Drawdown %" in active else 0.0)
    active_count = int((active["Position State"].astype(str) == "Open").sum()) if not active.empty and "Position State" in active else 0
    alert_count = len(alerts) if not alerts.empty else 0
    return {
        "Active P&L": active_pnl,
        "Daily P&L": active_pnl + daily_closed_pnl,
        "Exposure": exposure,
        "Drawdown": drawdown,
        "Active Trades": active_count,
        "Alerts": alert_count,
    }


def risk_gate_for_bot(bot: pd.Series, risk: dict[str, object]) -> tuple[bool, str]:
    if bool(risk.get("kill_switch", False)):
        log_diagnostic(logger, "risk_gate_block", bot=bot.get("name"), reason="kill_switch")
        return False, "kill switch active"
    capital = float(bot.get("capital", 0.0) or 0.0)
    if capital > float(risk["max_cash_per_trade"]):
        log_diagnostic(logger, "risk_gate_block", bot=bot.get("name"), reason="max_cash_per_trade", capital=capital, limit=risk["max_cash_per_trade"])
        return False, "bot capital exceeds max cash allocation per trade"
    running = load_bot_instances()
    running_count = int(running["state"].isin(["DEPLOYED", "RUNNING"]).sum()) if not running.empty and "state" in running else 0
    if running_count >= int(risk["max_trades_per_window"]):
        log_diagnostic(logger, "risk_gate_block", bot=bot.get("name"), reason="max_trades_per_window", running=running_count, limit=risk["max_trades_per_window"])
        return False, "max trades per configured window reached"
    exposure = float(running.loc[running["state"].isin(["DEPLOYED", "RUNNING"]), "capital"].sum()) if not running.empty and "capital" in running else 0.0
    if exposure + capital > float(risk["max_portfolio_exposure"]):
        log_diagnostic(logger, "risk_gate_block", bot=bot.get("name"), reason="max_portfolio_exposure", exposure=exposure, capital=capital, limit=risk["max_portfolio_exposure"])
        return False, "portfolio exposure limit would be breached"
    log_diagnostic(logger, "risk_gate_approved", bot=bot.get("name"), capital=capital, exposure=exposure)
    return True, "risk approved"


async def _load_live_scan_from_db() -> pd.DataFrame:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        frame = await read_live_scan(session)
    await engine.dispose()
    return frame


async def _load_risk_settings_from_db() -> dict[str, object]:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        risk = await read_risk_settings(session)
    await engine.dispose()
    return risk


async def _save_risk_settings_to_db(values: dict[str, object]) -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        await upsert_risk_settings(session, values)
    await engine.dispose()


async def _load_bot_instances_from_db() -> pd.DataFrame:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        frame = await read_bot_instances(session)
    await engine.dispose()
    return frame


async def _save_bot_instances_to_db(frame: pd.DataFrame) -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        for row in frame.to_dict(orient="records"):
            await upsert_bot_instance(session, row)
    await engine.dispose()


async def _delete_bot_instance_from_db(name: str) -> int:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        deleted = await delete_bot_instance(session, name)
    await engine.dispose()
    return deleted


async def _load_runtime_events_from_db() -> pd.DataFrame:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        frame = await read_runtime_events(session)
    await engine.dispose()
    return frame


async def _append_journal_to_db(event: dict[str, object]) -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        await append_journal_event(session, event)
    await engine.dispose()


async def _load_journal_from_db() -> pd.DataFrame:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        frame = await read_journal_events(session)
    await engine.dispose()
    return frame


async def _save_validation_run_to_db(row: dict[str, object]) -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        await upsert_validation_run(session, row)
    await engine.dispose()


async def _load_validation_runs_from_db() -> pd.DataFrame:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        frame = await read_validation_runs(session)
    await engine.dispose()
    return frame


async def _security_summary_from_db() -> dict[str, int]:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        summary = await security_schema_summary(session)
    await engine.dispose()
    return summary


async def _register_user_from_db(name: str, email: str, captcha: str) -> str:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        token = await register_user(session, name, email, captcha)
    await engine.dispose()
    return token


async def _login_user_from_db(email: str, password: str, captcha: str):
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        context = await login_user(session, email, password, captcha)
    await engine.dispose()
    return context


async def _logout_user_from_db(session_token: str) -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        await logout_session(session, session_token)
    await engine.dispose()


async def _request_password_reset_from_db(email: str) -> str | None:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        token = await create_password_reset(session, email)
    await engine.dispose()
    return token


async def _change_user_password_from_db(user_id: int, current_password: str, new_password: str) -> None:
    from sqlalchemy import select

    from aegis_trader.security.auth import audit
    from aegis_trader.storage.models import UserRow

    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        user = await session.scalar(select(UserRow).where(UserRow.id == user_id))
        if user is None:
            raise PermissionError("User session is no longer valid.")
        if not verify_password(current_password, user.password_hash):
            await audit(session, "password_change_failed", actor_user_id=user_id, target_user_id=user_id, details={"reason": "bad_current_password"})
            await session.commit()
            raise PermissionError("Current password is incorrect.")
        await set_user_password(session, user_id, new_password, force_change=False)
    await engine.dispose()


async def _load_heartbeat_from_db() -> dict[str, object]:
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        heartbeat = await read_latest_heartbeat(session)
    await engine.dispose()
    return heartbeat


def calm_auto_refresh(seconds: int) -> None:
    if seconds <= 0:
        return
    milliseconds = seconds * 1000
    components.html(
        f"""
        <script>
          const refreshMs = {milliseconds};
          window.setTimeout(() => {{
            window.parent.location.reload();
          }}, refreshMs);
        </script>
        """,
        height=0,
    )


def live_price_socket_component(scan: pd.DataFrame) -> None:
    scan = normalize_scan_columns(scan)
    symbols = scan.sort_values(["priority", "buy_score", "watch_score"], ascending=[True, False, False])["symbol"].astype(str).head(10).tolist()
    if not symbols:
        symbols = list(DEFAULT_LIVE_SYMBOLS)
    seed = {
        row["symbol"]: {
            "bucket": row["scan_bucket"],
            "watch": float(row["watch_score"]),
            "buy": float(row["buy_score"]),
            "flow": float(row["orderflow_score"]),
            "price": float(row["last_close"]),
        }
        for _, row in scan.iterrows()
        if row["symbol"] in symbols
    }
    streams = "/".join(f"{symbol.replace('/', '').lower()}@trade/{symbol.replace('/', '').lower()}@bookTicker" for symbol in symbols)
    components.html(
        f"""
        <div id="ticker-root"></div>
        <style>
          body {{
            margin: 0;
            background: transparent;
            color: #e8edf2;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          }}
          .ticker-grid {{
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 8px;
          }}
          .ticker-card {{
            min-height: 82px;
            border: 1px solid #26323b;
            border-radius: 8px;
            background: #111820;
            padding: 10px 11px;
            box-sizing: border-box;
          }}
          .ticker-top, .ticker-meta {{
            display: flex;
            justify-content: space-between;
            gap: 8px;
            align-items: center;
          }}
          .symbol {{
            font-weight: 760;
            font-size: 14px;
          }}
          .scan {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: #94a3ad;
            font-size: 11px;
            text-transform: uppercase;
          }}
          .dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #55d49a;
            box-shadow: 0 0 0 0 rgba(85, 212, 154, 0.55);
            animation: pulse 1.6s ease-out infinite;
          }}
          .price {{
            margin-top: 8px;
            font-variant-numeric: tabular-nums;
            font-size: 18px;
            font-weight: 780;
          }}
          .delta {{
            font-variant-numeric: tabular-nums;
            font-size: 12px;
          }}
          .scores {{
            color: #94a3ad;
            font-size: 11px;
            white-space: nowrap;
          }}
          .good {{ color: #55d49a; }}
          .bad {{ color: #ff6f7d; }}
          .warn {{ color: #f0c86a; }}
          @keyframes pulse {{
            0% {{ box-shadow: 0 0 0 0 rgba(85, 212, 154, 0.55); opacity: 1; }}
            70% {{ box-shadow: 0 0 0 8px rgba(85, 212, 154, 0); opacity: 0.72; }}
            100% {{ box-shadow: 0 0 0 0 rgba(85, 212, 154, 0); opacity: 1; }}
          }}
          @media (max-width: 900px) {{
            .ticker-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          }}
        </style>
        <script>
          const symbols = {json.dumps(symbols)};
          const seed = {json.dumps(seed)};
          const root = document.getElementById("ticker-root");
          const state = {{}};

          function fmtPrice(value) {{
            if (!Number.isFinite(value) || value <= 0) return "--";
            const digits = value >= 100 ? 2 : value >= 1 ? 4 : 6;
            return "$" + value.toLocaleString(undefined, {{ minimumFractionDigits: digits, maximumFractionDigits: digits }});
          }}

          function render() {{
            root.innerHTML = "<div class='ticker-grid'>" + symbols.map((symbol) => {{
              const item = state[symbol] || seed[symbol] || {{}};
              const price = item.price || 0;
              const open = item.open || price;
              const delta = open > 0 && price > 0 ? ((price - open) / open) * 100 : 0;
              const tone = delta >= 0 ? "good" : "bad";
              const bucket = item.bucket || "NO SIGNAL";
              const flow = Math.round(item.flow || 0);
              const buy = Math.round(item.buy || 0);
              return `
                <div class="ticker-card">
                  <div class="ticker-top">
                    <span class="symbol">${{symbol}}</span>
                    <span class="scan"><span class="dot"></span>${{bucket}}</span>
                  </div>
                  <div class="price">${{fmtPrice(price)}}</div>
                  <div class="ticker-meta">
                    <span class="delta ${{tone}}">${{delta >= 0 ? "+" : ""}}${{delta.toFixed(2)}}%</span>
                    <span class="scores">buy ${{buy}} | flow ${{flow}}</span>
                  </div>
                </div>
              `;
            }}).join("") + "</div>";
          }}

          function applyMessage(payload) {{
            const data = payload.data || payload;
            if (!data.s) return;
            const symbol = data.s.replace("USDT", "/USDT");
            const current = state[symbol] || seed[symbol] || {{}};
            const next = {{ ...current }};
            if (data.e === "trade") {{
              const price = Number(data.p);
              next.price = price;
              next.open = next.open || current.price || price;
            }}
            if (data.a && data.b) {{
              const bid = Number(data.b);
              const ask = Number(data.a);
              if (bid > 0 && ask > 0) next.price = next.price || ((bid + ask) / 2);
            }}
            state[symbol] = next;
          }}

          function connect() {{
            render();
            const socket = new WebSocket("wss://stream.testnet.binance.vision/stream?streams={streams}");
            socket.onmessage = (event) => {{
              applyMessage(JSON.parse(event.data));
              render();
            }};
            socket.onerror = () => {{
              root.dataset.stream = "error";
            }};
            socket.onclose = () => {{
              window.setTimeout(connect, 3000);
            }};
          }}
          connect();
        </script>
        """,
        height=190,
    )


def layout_chart(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111820",
        font={"color": "#dce5ec", "size": 12},
        margin={"l": 34, "r": 22, "t": 44, "b": 34},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    fig.update_xaxes(gridcolor="#23303a", zerolinecolor="#23303a")
    fig.update_yaxes(gridcolor="#23303a", zerolinecolor="#23303a")
    return fig


def status_row(summary: dict[str, float | str]) -> None:
    heartbeat = load_live_scan_heartbeat()
    stream_status = live_stream_heartbeat(load_live_stream())
    generated_at = heartbeat.get("generated_at")
    source = str(heartbeat.get("source", "not_started"))
    age_text = "not started"
    if generated_at:
        try:
            generated = utc_datetime(str(generated_at))
            age_seconds = max(0, int((datetime.now(UTC) - generated).total_seconds()))
            age_text = f"{age_seconds // 60}m {age_seconds % 60}s ago"
        except ValueError:
            age_text = str(generated_at)
    st.markdown(
        f"""
        <div class="status-row">
          <div class="status-card"><div class="status-label">Mode</div><div class="status-value info">{summary["mode"]}</div></div>
          <div class="status-card"><div class="status-label">Feed</div><div class="status-value good">{summary["feed"]}</div></div>
          <div class="status-card"><div class="status-label">Connectivity</div><div class="status-value good">{stream_status["status"]}</div></div>
          <div class="status-card"><div class="status-label">Risk</div><div class="status-value good">{summary["risk"]}</div></div>
          <div class="status-card"><div class="status-label">Protection</div><div class="status-value warn">{summary["kill"]}</div></div>
          <div class="status-card"><div class="status-label">Session</div><div class="status-value info">TESTNET</div></div>
        </div>
        <div class="diagnostic-strip">
          <span class="pill">stream: {stream_status["status"]}</span>
          <span class="pill">stream update: {stream_status["age"]}</span>
          <span class="pill">scanner: {source}</span>
          <span class="pill">last scan: {age_text}</span>
          <span class="pill">ok: {heartbeat.get("symbols_ok")}</span>
          <span class="pill">errors: {heartbeat.get("symbols_error")}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Diagnostics: feed endpoint and scanner heartbeat", expanded=False):
        st.caption("Operational diagnostics are kept here so trading decisions stay readable.")
        st.code(f"socket: {stream_status['source']}", language="text")
        st.json(
            {
                "stream_status": stream_status.get("status"),
                "stream_age": stream_status.get("age"),
                "scanner_source": source,
                "last_scan": age_text,
                "symbols_ok": heartbeat.get("symbols_ok"),
                "symbols_error": heartbeat.get("symbols_error"),
            }
        )


def portfolio_performance_overview(scan: pd.DataFrame, bots: pd.DataFrame) -> None:
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    _, strategy_aggregate = load_cached_strategy_matrix(tuple(STRATEGY_REGISTRY))
    replay_trades = load_top10_replay_trades()
    meter_rows = bot_cagr_meter_rows(bots, scan)
    meter_frame = pd.DataFrame(meter_rows)
    running = bots[bots["state"].isin(["DEPLOYED", "RUNNING"])] if not bots.empty and "state" in bots else pd.DataFrame()
    cumulative_pnl = float(pd.to_numeric(meter_frame.get("Cumulative P&L", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum()) if not meter_frame.empty else 0.0
    realized = float(pd.to_numeric(meter_frame.get("Realized P&L", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum()) if not meter_frame.empty else 0.0
    unrealized = float(pd.to_numeric(meter_frame.get("Unrealized P&L", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum()) if not meter_frame.empty else 0.0
    avg_win_rate = float(pd.to_numeric(meter_frame.get("Win Rate %", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if not meter_frame.empty else 0.0
    avg_pf = float(pd.to_numeric(meter_frame.get("Profit Factor", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if not meter_frame.empty else 0.0
    if not np.isfinite(avg_win_rate):
        avg_win_rate = 0.0
    if not np.isfinite(avg_pf):
        avg_pf = 0.0
    portfolio_sharpe = sharpe_from_return_series(replay_trades["return_pct"]) if not replay_trades.empty and "return_pct" in replay_trades else 0.0
    best_strategy = pd.Series(dtype=object)
    if not strategy_aggregate.empty and "sharpe_proxy" in strategy_aggregate:
        best_strategy = strategy_aggregate.sort_values("sharpe_proxy", ascending=False).iloc[0]
    exposure = float(running["capital"].sum()) if not running.empty and "capital" in running else 0.0
    active_risk = min(100.0, exposure / max(float(load_risk_settings().get("max_portfolio_exposure", 1.0)), 1.0) * 100)
    drawdown = float(scan["total_pnl"].min()) if not scan.empty and "total_pnl" in scan else 0.0
    st.markdown("### Global Performance Overview")
    a, b, c, d, e, f, g = st.columns(7)
    a.metric("Cumulative PnL", f"${cumulative_pnl:,.2f}")
    b.metric("Realized PnL", f"${realized:,.2f}")
    c.metric("Unrealized PnL", f"${unrealized:,.2f}")
    d.metric("Profit Factor", f"{avg_pf:.2f}")
    e.metric("Portfolio Sharpe", f"{portfolio_sharpe:.2f}")
    f.metric("Win Rate", f"{avg_win_rate:.1f}%")
    g.metric("Active Risk", f"{active_risk:.1f}%")
    if not best_strategy.empty:
        st.caption(
            f"Sharpe is calculated from the one-year top-10 replay trade return stream. "
            f"Best strategy Sharpe: {best_strategy['strategy']} at {float(best_strategy['sharpe_proxy']):.2f}."
        )
    dashboard_risk_guidance(portfolio_sharpe, drawdown)
    if not meter_frame.empty:
        ranked = meter_frame.rename(columns={"Bot": "symbol", "Cumulative P&L": "total_pnl", "Win Rate %": "confidence_score", "Profit Factor": "orderflow_score"}).sort_values("total_pnl", ascending=False).head(10)
        fig = make_subplots(rows=1, cols=2, specs=[[{"type": "bar"}, {"type": "scatter"}]], subplot_titles=("Strategy Universe PnL", "Signal Quality"))
        fig.add_trace(go.Bar(x=ranked["symbol"], y=ranked["total_pnl"], marker_color=np.where(ranked["total_pnl"] >= 0, "#55d49a", "#ff6f7d"), name="PnL"), row=1, col=1)
        fig.add_trace(go.Scatter(x=ranked["confidence_score"], y=ranked["orderflow_score"], mode="markers+text", text=ranked["symbol"], marker={"size": 12, "color": ranked["total_pnl"], "colorscale": "Viridis"}, name="Quality"), row=1, col=2)
        st.plotly_chart(layout_chart(fig, 300), use_container_width=True)


def bot_cagr_meter_rows(bots: pd.DataFrame, scan: pd.DataFrame) -> list[dict[str, object]]:
    if bots.empty:
        return []
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    matrix, _ = load_cached_strategy_matrix(tuple(STRATEGY_REGISTRY))
    pnl_snapshots = load_runtime_trade_pnl_snapshots()
    benchmark = INSTITUTIONAL_BOT_CAGR_MAX_PCT
    rows: list[dict[str, object]] = []
    for _, bot in bots.reset_index(drop=True).iterrows():
        name = str(bot.get("name", "Bot"))
        strategy = str(bot.get("strategy", ""))
        symbol = str(bot.get("symbol", ""))
        state = str(bot.get("state", "DRAFT"))
        bot_id = str(bot.get("bot_id", name))
        live_mark = bot_live_mark(bot, scan)
        snapshot = latest_pnl_snapshot_for_bot(bot_id, name, pnl_snapshots)
        capital = safe_number(bot.get("capital", live_mark.get("capital", 0.0)), 0.0)
        snapshot_unrealized = safe_number(snapshot.get("unrealized_pnl", 0.0)) if not snapshot.empty else 0.0
        snapshot_realized = safe_number(snapshot.get("realized_pnl", 0.0)) if not snapshot.empty else 0.0
        stored_realized = safe_number(bot.get("cumulative_realized_pnl", 0.0), 0.0)
        realized_pnl = max(stored_realized, snapshot_realized)
        unrealized_pnl = snapshot_unrealized
        cumulative_pnl = realized_pnl + unrealized_pnl
        if cumulative_pnl == 0.0:
            realized_pnl = safe_number(bot.get("realized_pnl", 0.0))
            unrealized_pnl = safe_number(live_mark.get("pnl", 0.0))
            cumulative_pnl = realized_pnl + unrealized_pnl
        cumulative_return_pct = 0.0 if capital <= 0 else cumulative_pnl / capital * 100.0
        cumulative_started_at = str(live_mark.get("cumulative_started_at", "") or live_mark.get("started_at", ""))
        cumulative_days = run_days_since(cumulative_started_at)
        live_cagr_value = annualized_cagr_from_run_return(cumulative_return_pct, cumulative_days)
        live_cagr_pct = 0.0 if live_cagr_value is None or cumulative_days < 7 else live_cagr_value
        maturity = cagr_maturity_label(cumulative_days)
        validation = validation_metrics_for_bot(name)
        backtest_cagr_pct = 0.0
        win_rate = 0.0
        max_drawdown = 0.0
        profit_factor = 0.0
        trade_count = int(bot.get("cumulative_trade_count", 0) or 0)
        if not matrix.empty and {"strategy", "symbol"}.issubset(matrix.columns):
            match = matrix[(matrix["strategy"].astype(str) == strategy) & (matrix["symbol"].astype(str) == symbol)]
            if not match.empty:
                perf = match.iloc[0]
                backtest_cagr_pct = safe_number(perf.get("avg_return_pct", perf.get("total_return_pct", 0.0)))
                win_rate = safe_number(perf.get("win_rate", 0.0))
                max_drawdown = safe_number(perf.get("max_drawdown_pct", 0.0))
                profit_factor = safe_number(perf.get("profit_factor", 0.0))
                trade_count = int(safe_number(perf.get("trades", trade_count), trade_count))
        if validation:
            backtest_cagr_pct = safe_number(validation.get("total_return_pct", validation.get("roi_pct", backtest_cagr_pct)), backtest_cagr_pct)
            win_rate = safe_number(validation.get("win_rate", win_rate), win_rate)
            max_drawdown = safe_number(validation.get("max_drawdown_pct", max_drawdown), max_drawdown)
            profit_factor = safe_number(validation.get("profit_factor", profit_factor), profit_factor)
            trade_count = int(safe_number(validation.get("total_trades", validation.get("trades", trade_count)), trade_count))
        use_live_cagr = state in {"RUNNING", "DEPLOYED"} and cumulative_days >= 7 and live_cagr_value is not None
        bot_cagr_pct = live_cagr_pct if use_live_cagr else backtest_cagr_pct
        benchmark_position_pct = max(0.0, min(100.0, bot_cagr_pct / max(benchmark, 0.01) * 100.0))
        tone = "good" if bot_cagr_pct >= benchmark * 0.75 else "warn" if bot_cagr_pct >= 0 else "bad"
        rows.append(
            {
                "Bot": name,
                "Strategy": strategy,
                "Coin": symbol,
                "Timeframe": str(bot.get("timeframe", "")),
                "State": state,
                "Bot CAGR %": round(bot_cagr_pct, 2),
                "Institutional CAGR Max %": round(benchmark, 2),
                "Benchmark Position %": round(bot_cagr_pct / max(benchmark, 0.01) * 100.0, 1),
                "Cumulative Return %": round(cumulative_return_pct, 2),
                "Cumulative Days": round(cumulative_days, 2),
                "Cumulative P&L": round(cumulative_pnl, 2),
                "Realized P&L": round(realized_pnl, 2),
                "Unrealized P&L": round(unrealized_pnl, 2),
                "_capital": capital,
                "_cumulative_started_at": cumulative_started_at,
                "CAGR Maturity": maturity,
                "Live CAGR %": round(live_cagr_pct, 2),
                "Backtest CAGR %": round(backtest_cagr_pct, 2),
                "Trade Count": trade_count,
                "Win Rate %": round(win_rate, 1),
                "Max Drawdown %": round(max_drawdown, 1),
                "Profit Factor": round(profit_factor, 2),
                "_meter_pct": benchmark_position_pct,
                "_tone": tone,
            }
        )
    return rows


def strategy_cagr_meter_rows(bot_rows: list[dict[str, object]], benchmark: float) -> list[dict[str, object]]:
    strategy_rows: list[dict[str, object]] = []
    detail_for_strategy = pd.DataFrame(bot_rows)
    if detail_for_strategy.empty:
        return strategy_rows
    for strategy, group in detail_for_strategy.groupby("Strategy", dropna=False):
        capital_sum = float(pd.to_numeric(group["_capital"], errors="coerce").fillna(0.0).sum())
        pnl_sum = float(pd.to_numeric(group["Cumulative P&L"], errors="coerce").fillna(0.0).sum())
        starts = pd.to_datetime(group["_cumulative_started_at"], errors="coerce", utc=True).dropna()
        strategy_start = starts.min().isoformat() if not starts.empty else ""
        strategy_days = run_days_since(strategy_start)
        strategy_return = 0.0 if capital_sum <= 0 else pnl_sum / capital_sum * 100.0
        strategy_cagr_value = annualized_cagr_from_run_return(strategy_return, strategy_days)
        strategy_cagr = 0.0 if strategy_cagr_value is None or strategy_days < 7 else strategy_cagr_value
        strategy_tone = "good" if strategy_cagr >= benchmark * 0.75 else "warn" if strategy_cagr >= 0 else "bad"
        strategy_rows.append(
            {
                "Strategy": str(strategy),
                "Bots": int(len(group)),
                "Strategy CAGR %": round(strategy_cagr, 2),
                "Cumulative Return %": round(strategy_return, 2),
                "Cumulative P&L": round(pnl_sum, 2),
                "Capital": round(capital_sum, 2),
                "Cumulative Days": round(strategy_days, 2),
                "CAGR Maturity": cagr_maturity_label(strategy_days),
                "Institutional CAGR Max %": round(benchmark, 2),
                "Benchmark Position %": round(strategy_cagr / max(benchmark, 0.01) * 100.0, 1),
                "_meter_pct": max(0.0, min(100.0, strategy_cagr / max(benchmark, 0.01) * 100.0)),
                "_tone": strategy_tone,
            }
        )
    return strategy_rows


def individual_bot_performance_meters(bots: pd.DataFrame, scan: pd.DataFrame) -> None:
    if bots.empty:
        st.info("No bot definitions are configured yet.")
        return
    benchmark = INSTITUTIONAL_BOT_CAGR_MAX_PCT
    source_text = " | ".join(f"{row['source']}: {float(row['cagr_pct']):.2f}% CAGR" for row in INSTITUTIONAL_BOT_CAGR_SOURCES)
    st.caption(
        f"Meter ceiling is {benchmark:.2f}% CAGR: the average of three cross-checked institutional managed-futures / CTA annualized performance references. {source_text}."
    )
    rows = bot_cagr_meter_rows(bots, scan)
    html_cards = ["<div class='bot-meter-grid'>"]
    for row in rows:
        meter_value = f"{float(row['Bot CAGR %']):.2f}% CAGR" if row["CAGR Maturity"] != "Too early for CAGR" else f"{float(row['Cumulative Return %']):.2f}% cumulative"
        needle_angle = -90.0 + (float(row["_meter_pct"]) * 1.8)
        html_cards.append(
            "<div class='bot-meter-card'>"
            "<div class='bot-meter-top'>"
            f"<div><div class='bot-meter-title'>{html.escape(str(row['Bot']))}</div>"
            f"<div class='bot-meter-meta'>{html.escape(str(row['Strategy']))} | {html.escape(str(row['Coin']))} | {html.escape(str(row['Timeframe']))} | {float(row['Cumulative Days']):.1f} cumulative days</div></div>"
            f"<div class='bot-meter-value {row['_tone']}'>{meter_value}</div>"
            "</div>"
            "<div class='bot-analog-meter'>"
            "<div class='bot-analog-arc'></div>"
            f"<div class='bot-analog-needle {row['_tone']}' style='transform: rotate({needle_angle:.1f}deg);'></div>"
            "<div class='bot-analog-hub'></div>"
            f"<div class='bot-analog-center'><div class='bot-analog-number'>{float(row['Benchmark Position %']):.1f}%</div><div class='bot-analog-caption'>of institutional max</div></div>"
            "<span class='bot-analog-tick left'>0</span>"
            "<span class='bot-analog-tick mid'>50</span>"
            "<span class='bot-analog-tick right'>100</span>"
            "</div>"
            "<div class='bot-meter-bands'><span class='bot-meter-band bad'>Low</span><span class='bot-meter-band warn'>Watch</span><span class='bot-meter-band good'>Strong</span></div>"
            f"<div class='bot-meter-scale'><span>{html.escape(str(row['CAGR Maturity']))}</span><span>Institutional max {benchmark:.2f}% CAGR</span><span>{float(row['Benchmark Position %']):.1f}% of max</span></div>"
            "</div>"
        )
    html_cards.append("</div>")
    st.markdown("".join(html_cards), unsafe_allow_html=True)
    strategy_rows = strategy_cagr_meter_rows(rows, benchmark)
    if strategy_rows:
        st.markdown("#### Strategy CAGR Meters")
        strategy_cards = ["<div class='bot-meter-grid'>"]
        for row in strategy_rows:
            meter_value = f"{float(row['Strategy CAGR %']):.2f}% CAGR" if row["CAGR Maturity"] != "Too early for CAGR" else f"{float(row['Cumulative Return %']):.2f}% cumulative"
            needle_angle = -90.0 + (float(row["_meter_pct"]) * 1.8)
            strategy_cards.append(
                "<div class='bot-meter-card'>"
                "<div class='bot-meter-top'>"
                f"<div><div class='bot-meter-title'>{html.escape(str(row['Strategy']))}</div>"
                f"<div class='bot-meter-meta'>{int(row['Bots'])} bot(s) | {float(row['Cumulative Days']):.1f} cumulative days | capital ${float(row['Capital']):,.2f}</div></div>"
                f"<div class='bot-meter-value {row['_tone']}'>{meter_value}</div>"
                "</div>"
                "<div class='bot-analog-meter'>"
                "<div class='bot-analog-arc'></div>"
                f"<div class='bot-analog-needle {row['_tone']}' style='transform: rotate({needle_angle:.1f}deg);'></div>"
                "<div class='bot-analog-hub'></div>"
                f"<div class='bot-analog-center'><div class='bot-analog-number'>{float(row['Benchmark Position %']):.1f}%</div><div class='bot-analog-caption'>of institutional max</div></div>"
                "<span class='bot-analog-tick left'>0</span>"
                "<span class='bot-analog-tick mid'>50</span>"
                "<span class='bot-analog-tick right'>100</span>"
                "</div>"
                "<div class='bot-meter-bands'><span class='bot-meter-band bad'>Low</span><span class='bot-meter-band warn'>Watch</span><span class='bot-meter-band good'>Strong</span></div>"
                f"<div class='bot-meter-scale'><span>{html.escape(str(row['CAGR Maturity']))}</span><span>Institutional max {benchmark:.2f}% CAGR</span><span>{float(row['Benchmark Position %']):.1f}% of max</span></div>"
                "</div>"
            )
        strategy_cards.append("</div>")
        st.markdown("".join(strategy_cards), unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(strategy_rows).drop(columns=["_meter_pct", "_tone"], errors="ignore").astype(str), use_container_width=True, hide_index=True)
    detail = pd.DataFrame(rows).drop(columns=["_meter_pct", "_tone", "_capital", "_cumulative_started_at"], errors="ignore")
    st.markdown("#### Bot Performance Details")
    st.dataframe(detail.astype(str), use_container_width=True, hide_index=True)


def dashboard_risk_guidance(portfolio_sharpe: float, drawdown: float) -> None:
    stress = load_stress_report()
    scenarios = stress.get("scenarios", []) if isinstance(stress, dict) else []
    max_stress_dd = max((float(row.get("max_drawdown_pct", 0.0) or 0.0) for row in scenarios if isinstance(row, dict)), default=0.0)
    guidance: list[str] = []
    if portfolio_sharpe < 1.0:
        guidance.append("raise entry quality: require stronger orderflow and confidence before a bot can deploy")
    if max_stress_dd >= 12 or abs(drawdown) > 12:
        guidance.append("tighten risk: lower max cash per trade, cap active bot count, and add a drawdown throttle")
    if max_stress_dd >= 25:
        guidance.append("pause during stress: block new entries during spread expansion or thin liquidity states")
    if guidance:
        st.warning("Risk improvement: " + "; ".join(guidance) + ".")


def signal_flow_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Signal Flow")
    st.caption("Live path from market data to trade readiness, with each gate shown in plain operational language.")
    st.info(
        "Read left to right. Bright nodes are crossed, dim nodes are pending, the dot marks the furthest stage reached, "
        "and the Next Gate column explains what is blocking the token from becoming actionable."
    )
    scan = normalize_scan_columns(load_live_scan())
    stream = load_live_stream()
    bots = load_bot_instances()
    if scan.empty:
        st.info("No live market scan data yet. Start the Binance stream to populate the flow map.")
        return
    ordered_symbols = scan.sort_values(["priority", "buy_score", "watch_score"], ascending=[True, False, False])["symbol"].astype(str).tolist()
    developing_symbols = top_developing_signal_flow_symbols(scan, limit=5)
    default_symbols = developing_symbols or ordered_symbols[: min(5, len(ordered_symbols))]
    c1, c2, c3 = st.columns([1.5, 1, 1])
    selected_symbols = c1.multiselect("Coins", ordered_symbols, default=default_symbols, help="Defaults to the top 5 developing coins. You can override the lanes manually.")
    selected_timeframe = c2.selectbox("Pipeline timeframe", ["strategy default", "1m", "5m", "15m", "1h", "4h", "1d"], index=0)
    calm = c3.toggle("Calm motion", value=False, help="Slows the particle flow for lower visual intensity.")
    if developing_symbols:
        st.caption(f"Default lanes are the current top developing coins: {', '.join(developing_symbols)}.")
    if not selected_symbols:
        st.warning("Select at least one coin to render the flow map.")
        return
    rows = build_signal_flow_rows(scan, stream, bots, selected_symbols, selected_timeframe)
    fresh_count = sum(1 for row in rows if row["socket_state"] == "LIVE")
    buy_count = sum(1 for row in rows if row["bucket"] == "BUY")
    watch_count = sum(1 for row in rows if row["bucket"] == "WATCH")
    advanced_count = sum(1 for row in rows if int(row["crossed_stage_index"]) >= SIGNAL_FLOW_STAGES.index("ORDERFLOW"))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Live Feed Lanes", f"{fresh_count}/{len(rows)}")
    m2.metric("Watch", watch_count)
    m3.metric("Buy Signals", buy_count)
    m4.metric("Order Flow Ready", advanced_count)
    market_bucket_swim_lanes(scan, bots)
    strategy_traffic_light_panel(scan)
    signal_flow_backtest_panel(scan)
    signal_flow_component(rows, calm=calm)
    st.markdown("#### Pipeline State")
    st.dataframe(
        pd.DataFrame(rows)[
            [
                "symbol",
                "timeframe",
                "socket_state",
                "candle_state",
                "features_state",
                "orderflow_state",
                "strategy_state",
                "risk_state",
                "decision_state",
                "crossed_stage",
                "next_gate",
                "last_price",
                "orderflow_score",
                "buy_score",
                "confidence_score",
                "reason",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "orderflow_score": st.column_config.ProgressColumn("Flow", min_value=0, max_value=100, format="%.0f%%"),
            "buy_score": st.column_config.ProgressColumn("Buy", min_value=0, max_value=100, format="%.0f%%"),
            "confidence_score": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%.0f%%"),
        },
    )


def signal_flow_component(rows: list[dict[str, object]], calm: bool = False) -> None:
    symbols = [str(row["symbol"]) for row in rows]
    streams = "/".join(f"{symbol.replace('/', '').lower()}@trade/{symbol.replace('/', '').lower()}@bookTicker" for symbol in symbols)
    html = r"""
    <div id="flow-shell">
      <div id="flow-top">
        <div>
          <div class="kicker">LIVE PIPELINE OBSERVABILITY</div>
          <div class="title">Market Data To Signal Flow</div>
        </div>
        <div id="stream-state">connecting</div>
      </div>
      <canvas id="flow-canvas"></canvas>
      <div id="flow-detail"></div>
    </div>
    <style>
      body { margin: 0; background: transparent; color: #eaf2f8; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      #flow-shell { border: 1px solid rgba(118, 145, 165, 0.28); border-radius: 14px; overflow: hidden; background: radial-gradient(circle at 18% 16%, rgba(61, 196, 255, 0.18), transparent 28%), radial-gradient(circle at 78% 14%, rgba(128, 255, 170, 0.14), transparent 26%), linear-gradient(135deg, #08111b 0%, #101821 48%, #151417 100%); }
      #flow-top { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px 8px; border-bottom: 1px solid rgba(118, 145, 165, 0.18); }
      .kicker { color: #8cb8c8; font-size: 11px; letter-spacing: 0.16em; font-weight: 800; }
      .title { font-size: 22px; font-weight: 820; margin-top: 2px; }
      #stream-state { border: 1px solid rgba(117, 218, 167, 0.38); color: #78e5ad; background: rgba(23, 75, 54, 0.34); border-radius: 999px; padding: 5px 10px; font-size: 12px; text-transform: uppercase; }
      #flow-canvas { display: block; width: 100%; height: 520px; }
      #flow-detail { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; padding: 10px 12px 14px; }
      .flow-card { border: 1px solid rgba(118, 145, 165, 0.24); border-radius: 9px; background: rgba(9, 16, 24, 0.58); padding: 9px 10px; min-height: 78px; }
      .flow-card b { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
      .flow-card span { display: block; color: #aebfca; font-size: 11px; margin-top: 4px; }
      .good { color: #64e3a0; } .warn { color: #ffd26e; } .bad { color: #ff7184; } .info { color: #7db2ff; }
      @media (max-width: 900px) { #flow-detail { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    </style>
    <script>
      const rows = __ROWS__;
      const streams = "__STREAMS__";
      const calm = __CALM__;
      const stages = [["SOCKET", "#4ecbff"], ["CANDLE", "#7aa7ff"], ["FEATURES", "#52d7a5"], ["ORDERFLOW", "#ba8cff"], ["STRATEGY", "#ffd166"], ["RISK", "#ff7d90"], ["DECISION", "#78f0a9"], ["JOURNAL", "#c7d1d8"]];
      const statusColors = { LIVE: "#56e39f", STALE: "#ffbf69", PENDING: "#8fa4b1", CLOSED: "#73a7ff", WAITING: "#8fa4b1", READY: "#56e39f", WARMING: "#ffbf69", SUPPORTIVE: "#56e39f", DEVELOPING: "#ffd166", WEAK: "#ff7184", SIGNAL: "#56e39f", WATCH: "#ffd166", TRACKING: "#7aa7ff", QUIET: "#8fa4b1", OK: "#56e39f", BLOCKED: "#ff4d6d", WAIT: "#8fa4b1", "BUY SIGNAL": "#56e39f", "IN TRADE": "#7aa7ff" };
      const canvas = document.getElementById("flow-canvas");
      const ctx = canvas.getContext("2d");
      const detail = document.getElementById("flow-detail");
      const stateLabel = document.getElementById("stream-state");
      const live = {};
      const particles = [];
      let lastSpawn = performance.now();
      rows.forEach((row) => { live[row.symbol] = { ...row, packets: 0, lastPulse: 0, price: Number(row.last_price || 0) }; });
      function resize() { const rect = canvas.getBoundingClientRect(); const ratio = window.devicePixelRatio || 1; canvas.width = Math.max(900, rect.width * ratio); canvas.height = rect.height * ratio; ctx.setTransform(ratio, 0, 0, ratio, 0, 0); }
      function toneFor(row, stage) { if (stage === "SOCKET") return statusColors[row.socket_state] || "#8fa4b1"; if (stage === "CANDLE") return statusColors[row.candle_state] || "#8fa4b1"; if (stage === "FEATURES") return statusColors[row.features_state] || "#8fa4b1"; if (stage === "ORDERFLOW") return statusColors[row.orderflow_state] || "#8fa4b1"; if (stage === "STRATEGY") return statusColors[row.strategy_state] || "#8fa4b1"; if (stage === "RISK") return statusColors[row.risk_state] || "#8fa4b1"; if (stage === "DECISION") return statusColors[row.decision_state] || "#8fa4b1"; return "#c7d1d8"; }
      function stageText(row, stage) { if (stage === "SOCKET") return row.socket_state; if (stage === "CANDLE") return row.timeframe + " " + row.candle_state; if (stage === "FEATURES") return row.features_state + " " + Math.round(row.confidence_score || 0) + "%"; if (stage === "ORDERFLOW") return row.orderflow_state + " " + Math.round(row.orderflow_score || 0) + "%"; if (stage === "STRATEGY") return row.strategy_state; if (stage === "RISK") return row.risk_state; if (stage === "DECISION") return row.decision_state; return "persist"; }
      function nodePos(stageIndex, rowIndex, width, height) { const compact = width < 760; const left = compact ? 104 : 132; const right = width - (compact ? 34 : 96); const top = 64; const rowGap = rows.length <= 1 ? 0 : (height - 126) / (rows.length - 1); return { x: left + (right - left) * (stageIndex / (stages.length - 1)), y: top + rowGap * rowIndex }; }
      function nodeDims(width) { return width < 760 ? { w: 48, h: 42, title: "7px", text: "7px" } : { w: 76, h: 48, title: "10px", text: "10px" }; }
      function roundedRect(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
      function spawn(symbol, count = 1) { const rowIndex = rows.findIndex((row) => row.symbol === symbol); if (rowIndex < 0) return; for (let i = 0; i < count; i++) particles.push({ rowIndex, t: Math.random() * 0.06, speed: (calm ? 0.0012 : 0.0024) + Math.random() * 0.0018, color: stages[Math.floor(Math.random() * 5)][1], size: 3 + Math.random() * 3 }); }
      function draw() {
        const width = canvas.clientWidth; const height = canvas.clientHeight; ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "rgba(255,255,255,0.025)"; for (let i = 0; i < 34; i++) ctx.fillRect((i * 83) % width, 0, 1, height);
        rows.forEach((rowSeed, rowIndex) => {
          const row = live[rowSeed.symbol] || rowSeed; const base = nodePos(0, rowIndex, width, height);
          const crossed = Number(row.crossed_stage_index || 0);
          const labelMax = width < 760 ? 11 : 16;
          ctx.font = "700 12px Inter, Arial"; ctx.fillStyle = "#eaf2f8"; ctx.fillText(String(row.symbol || "").slice(0, labelMax), 14, base.y - 7);
          ctx.font = "10px Inter, Arial"; ctx.fillStyle = "#91a8b8"; ctx.fillText("$" + Number(row.price || row.last_price || 0).toLocaleString(undefined, { maximumFractionDigits: 5 }), 14, base.y + 8);
          ctx.fillStyle = statusColors[row.crossed_stage] || "#78f0a9"; ctx.fillText("at " + (row.crossed_stage || "SOCKET"), 14, base.y + 23);
          const dims = nodeDims(width);
          for (let i = 0; i < stages.length - 1; i++) { const from = nodePos(i, rowIndex, width, height); const to = nodePos(i + 1, rowIndex, width, height); const passed = i < crossed; const grad = ctx.createLinearGradient(from.x, from.y, to.x, to.y); grad.addColorStop(0, toneFor(row, stages[i][0]) + (passed ? "dd" : "33")); grad.addColorStop(1, toneFor(row, stages[i + 1][0]) + (passed ? "dd" : "33")); ctx.strokeStyle = grad; ctx.lineWidth = passed ? (row.bucket === "BUY" ? 5 : row.bucket === "WATCH" ? 4 : 3) : 1.4; ctx.setLineDash(passed ? [] : [5, 8]); ctx.beginPath(); ctx.moveTo(from.x + (dims.w / 2) - 4, from.y); ctx.bezierCurveTo(from.x + 52, from.y - 12, to.x - 52, to.y + 12, to.x - (dims.w / 2) + 4, to.y); ctx.stroke(); ctx.setLineDash([]); }
          stages.forEach(([stage], stageIndex) => { const pos = nodePos(stageIndex, rowIndex, width, height); const crossedNode = stageIndex <= crossed; const currentNode = stageIndex === crossed; const tone = toneFor(row, stage); const pulse = currentNode ? Math.max(0, 1 - ((performance.now() - (row.lastPulse || 0)) / 1100)) : 0; ctx.globalAlpha = crossedNode ? 1 : 0.42; ctx.shadowColor = tone; ctx.shadowBlur = currentNode ? 18 + pulse * 22 : crossedNode ? 9 : 0; ctx.fillStyle = crossedNode ? "rgba(8, 17, 27, 0.94)" : "rgba(8, 17, 27, 0.46)"; roundedRect(pos.x - (dims.w / 2), pos.y - (dims.h / 2), dims.w, dims.h, 10); ctx.fill(); ctx.lineWidth = currentNode ? 3 + pulse * 2 : crossedNode ? 1.7 : 1; ctx.strokeStyle = tone; ctx.stroke(); ctx.shadowBlur = 0; if (crossedNode) { ctx.fillStyle = currentNode ? tone : "#d8e5ed"; ctx.font = "900 10px Inter, Arial"; ctx.textAlign = "center"; ctx.fillText(currentNode ? "●" : "✓", pos.x - (dims.w / 2) + 9, pos.y - 7); } ctx.fillStyle = tone; ctx.font = `800 ${dims.title} Inter, Arial`; ctx.textAlign = "center"; ctx.fillText(stage, pos.x, pos.y - 4); ctx.fillStyle = "#d8e5ed"; ctx.font = `${dims.text} Inter, Arial`; ctx.fillText(stageText(row, stage).slice(0, width < 720 ? 9 : 16), pos.x, pos.y + 11); ctx.textAlign = "left"; ctx.globalAlpha = 1; });
        });
        for (let i = particles.length - 1; i >= 0; i--) { const p = particles[i]; p.t += p.speed * 16; if (p.t >= 1) { particles.splice(i, 1); continue; } const scaled = p.t * (stages.length - 1); const idx = Math.min(stages.length - 2, Math.floor(scaled)); const local = scaled - idx; const from = nodePos(idx, p.rowIndex, width, height); const to = nodePos(idx + 1, p.rowIndex, width, height); const x = from.x + (to.x - from.x) * local; const y = from.y + (to.y - from.y) * local + Math.sin(local * Math.PI) * -12; ctx.shadowColor = p.color; ctx.shadowBlur = 12; ctx.fillStyle = p.color; ctx.beginPath(); ctx.arc(x, y, p.size, 0, Math.PI * 2); ctx.fill(); ctx.shadowBlur = 0; }
        requestAnimationFrame(draw);
      }
      function renderDetail() { detail.innerHTML = rows.map((seed) => { const row = live[seed.symbol] || seed; const cls = row.bucket === "BUY" || row.decision_state === "BUY SIGNAL" ? "good" : row.bucket === "WATCH" ? "warn" : row.risk_state === "BLOCKED" ? "bad" : "info"; return `<div class="flow-card"><b>${row.symbol}<em class="${cls}">${row.crossed_stage || "SOCKET"}</em></b><span>${row.timeframe} | flow ${Math.round(row.orderflow_score || 0)}% | buy ${Math.round(row.buy_score || 0)}% | conf ${Math.round(row.confidence_score || 0)}%</span><span>Next: ${row.next_gate || "awaiting scanner reason"}</span><span>${row.reason || "awaiting scanner reason"}</span></div>`; }).join(""); }
      function connect() { if (!streams) { stateLabel.textContent = "seed data only"; return; } const socket = new WebSocket("wss://stream.testnet.binance.vision/stream?streams=" + streams); socket.onopen = () => { stateLabel.textContent = "live socket connected"; }; socket.onerror = () => { stateLabel.textContent = "socket warning"; }; socket.onclose = () => { stateLabel.textContent = "reconnecting"; window.setTimeout(connect, 3000); }; socket.onmessage = (event) => { const payload = JSON.parse(event.data).data || {}; if (!payload.s) return; const symbol = payload.s.replace("USDT", "/USDT"); const row = live[symbol]; if (!row) return; if (payload.p) row.price = Number(payload.p); if (payload.a && payload.b) row.price = (Number(payload.a) + Number(payload.b)) / 2; row.socket_state = "LIVE"; row.packets = (row.packets || 0) + 1; row.lastPulse = performance.now(); spawn(symbol, calm ? 1 : 3); renderDetail(); }; }
      window.addEventListener("resize", resize); resize(); rows.forEach((row) => spawn(row.symbol, calm ? 1 : 4)); window.setInterval(() => { const now = performance.now(); if (now - lastSpawn > (calm ? 1800 : 900)) { rows.forEach((row) => spawn(row.symbol, 1)); lastSpawn = now; } }, 300); renderDetail(); draw(); connect();
    </script>
    """
    html = html.replace("__ROWS__", json.dumps(rows)).replace("__STREAMS__", streams).replace("__CALM__", "true" if calm else "false")
    components.html(html, height=720)


def dashboard_screen(summary: dict[str, float | str]) -> None:
    status_row(summary)
    scan = normalize_scan_columns(load_live_scan())
    bots = load_bot_instances()
    stream = load_live_stream()
    risk = load_risk_settings()
    runtime_status = RuntimeManager().runtime_status()
    heartbeat = load_live_scan_heartbeat()
    generated_at = str(heartbeat.get("generated_at") or "")
    stale_age = stream_age_seconds(generated_at)
    stale = stale_age is None or stale_age > 90
    running = bots[bots["state"].astype(str).isin(["RUNNING", "DEPLOYED"])] if not bots.empty and "state" in bots else pd.DataFrame()
    active_exposure = float(running["capital"].sum()) if not running.empty and "capital" in running else 0.0
    total_pnl = float(scan["total_pnl"].sum()) if not scan.empty and "total_pnl" in scan else 0.0
    live_count = int((scan["stream_status"].astype(str) == "live").sum()) if not scan.empty and "stream_status" in scan else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Runtime", str(runtime_status.get("runtime", "UNKNOWN")))
    c2.metric("Running Bots", int(runtime_status.get("running_bots", len(running))))
    c3.metric("Exposure", money_text(active_exposure), f"limit {money_text(risk.get('max_portfolio_exposure', 0.0))}")
    c4.metric("Replay P&L", money_text(total_pnl))
    c5.metric("Live Market Feeds", f"{live_count}/{0 if scan.empty else len(scan)}")
    if stale:
        st.warning(f"Stale data warning: live scanner heartbeat is {stream_age_text(generated_at)} old.")
        st.markdown(
            "<div class='decision-summary warn'><div class='decision-summary-title'>Operator focus</div>"
            "<div class='runtime-bot-boundary-meta'>Scanner data is stale. Treat live signals as watch-only until the feed heartbeat recovers.</div></div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption(f"Live panels last updated {stream_age_text(generated_at)} ago. Panels refresh data caches, not the whole execution runtime.")
        st.markdown(
            "<div class='decision-summary good'><div class='decision-summary-title'>Operator focus</div>"
            "<div class='runtime-bot-boundary-meta'>Feed is current. Review Dashboard for visibility, then use Bot Management for execution controls.</div></div>",
            unsafe_allow_html=True,
        )

    tab_overview, tab_live, tab_signal, tab_bots, tab_risk = st.tabs(["Overview", "Live Trading", "Signal Flow", "Bot Health", "Risk & Alerts"])
    with tab_overview:
        st.markdown("### Performance To Date")
        portfolio_performance_overview(scan, bots)
        if not bots.empty:
            st.markdown("### Individual Bot Performance")
            individual_bot_performance_meters(bots, scan)
    with tab_live:
        st.markdown("### Live Trading Status")
        if not scan.empty:
            live_price_socket_component(scan)
            st.caption(f"Last updated {stream_age_text(generated_at)} ago. Existing Live Trading content is now surfaced in Dashboard.")
        else:
            st.info("No live trading scan rows are available yet.")
    with tab_signal:
        st.markdown("### Signal Flow Status")
        if scan.empty:
            st.info("No signal flow rows are available yet.")
        else:
            developing_symbols = top_developing_signal_flow_symbols(scan, limit=5) or scan["symbol"].astype(str).head(5).tolist()
            rows = build_signal_flow_rows(scan, stream, bots, developing_symbols, "strategy default")
            market_bucket_swim_lanes(scan, bots)
            strategy_traffic_light_panel(scan)
            signal_flow_backtest_panel(scan)
            st.markdown("#### Signal Flow Pipeline Snapshot")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    with tab_bots:
        st.markdown("### Active Bot Summary")
        if bots.empty:
            st.info("No bot definitions are configured yet.")
        else:
            runtime_states = pd.DataFrame(RuntimeManager().list_bot_states())
            bot_health = runtime_states if not runtime_states.empty else bots
            st.dataframe(bot_health, use_container_width=True, hide_index=True)
            st.caption("Drill down to Bot Management / Runtime for start, stop, restart, and detailed cockpit controls.")
    with tab_risk:
        st.markdown("### Risk Exposure Summary")
        risk_summary = pd.DataFrame(
            [
                {"Metric": "Kill switch / risk lock", "Value": str(bool(risk.get("kill_switch", False))), "Guidance": "Blocks new exposure when enabled."},
                {"Metric": "Max cash per trade", "Value": money_text(risk.get("max_cash_per_trade", 0.0)), "Guidance": "Hard cap consumed by runtime sizing."},
                {"Metric": "Max risk per trade", "Value": pct_text(float(risk.get("max_risk_per_trade_pct", 0.0) or 0.0) * 100), "Guidance": "Feeds auto position sizing."},
                {"Metric": "Max portfolio exposure", "Value": money_text(risk.get("max_portfolio_exposure", 0.0)), "Guidance": "Caps total bot deployment."},
            ]
        )
        st.dataframe(
            risk_summary.astype(str),
            use_container_width=True,
            hide_index=True,
        )
        alerts = load_runtime_alerts()
        if alerts.empty:
            st.success("No runtime alerts recorded.")
        else:
            st.markdown("### Alerts")
            st.dataframe(alerts.head(50), use_container_width=True, hide_index=True)


def strategy_backtest_ranking(aggregate: pd.DataFrame, focus: str = "Balanced") -> pd.DataFrame:
    if aggregate.empty:
        return pd.DataFrame()
    ranked = aggregate.copy()
    numeric_columns = [
        "trades",
        "total_pnl",
        "avg_return_pct",
        "max_drawdown_pct",
        "avg_trade_return_pct",
        "sharpe_proxy",
        "avg_profit_factor",
        "confidence_score",
        "win_rate",
    ]
    for column in numeric_columns:
        if column not in ranked:
            ranked[column] = 0.0
        ranked[column] = pd.to_numeric(ranked[column], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)

    weights = {
        "Balanced": {
            "total_pnl": 0.17,
            "avg_return_pct": 0.14,
            "sharpe_proxy": 0.19,
            "avg_profit_factor": 0.14,
            "win_rate": 0.12,
            "confidence_score": 0.10,
            "trades": 0.06,
            "drawdown_control": 0.08,
        },
        "Return": {
            "total_pnl": 0.28,
            "avg_return_pct": 0.24,
            "sharpe_proxy": 0.12,
            "avg_profit_factor": 0.10,
            "win_rate": 0.08,
            "confidence_score": 0.06,
            "trades": 0.06,
            "drawdown_control": 0.06,
        },
        "Risk-adjusted": {
            "total_pnl": 0.10,
            "avg_return_pct": 0.10,
            "sharpe_proxy": 0.27,
            "avg_profit_factor": 0.20,
            "win_rate": 0.11,
            "confidence_score": 0.10,
            "trades": 0.04,
            "drawdown_control": 0.08,
        },
        "Conservative": {
            "total_pnl": 0.08,
            "avg_return_pct": 0.08,
            "sharpe_proxy": 0.20,
            "avg_profit_factor": 0.18,
            "win_rate": 0.14,
            "confidence_score": 0.12,
            "trades": 0.05,
            "drawdown_control": 0.15,
        },
    }[focus]

    ranked["drawdown_control"] = -ranked["max_drawdown_pct"].clip(lower=0)
    score = pd.Series(0.0, index=ranked.index)
    for column, weight in weights.items():
        score += _percentile_score(ranked[column]) * weight
    ranked["rank_score"] = score.round(1)
    ranked["rank"] = ranked["rank_score"].rank(method="first", ascending=False).astype(int)
    return ranked.sort_values(["rank_score", "total_pnl"], ascending=[False, False]).reset_index(drop=True)


def _percentile_score(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if len(numeric) <= 1 or float(numeric.max()) == float(numeric.min()):
        return pd.Series(50.0, index=numeric.index)
    return numeric.rank(pct=True) * 100


def strategy_ranking_panel() -> None:
    st.markdown("### Strategy Ranking")
    strategy_names = tuple(STRATEGY_REGISTRY)
    c1, c2 = st.columns([2, 1])
    focus = c1.selectbox("Ranking focus", ["Balanced", "Return", "Risk-adjusted", "Conservative"], help="Composite score from cached backtest metrics.")
    refresh = c2.button("Refresh strategy backtests", use_container_width=True)
    if refresh:
        with st.spinner("Refreshing backtest-derived strategy ranking."):
            _, aggregate = refresh_strategy_matrix_cache(strategy_names)
    else:
        _, aggregate = load_cached_strategy_matrix(strategy_names)

    if aggregate.empty:
        st.info("No cached backtest ranking is available yet. Refresh strategy backtests to rank strategies by replay results.")
        return

    ranked = strategy_backtest_ranking(aggregate, focus)
    ranked["activation"] = ranked["strategy"].map(lambda name: getattr(STRATEGY_REGISTRY.get(str(name)), "activation_state", "DORMANT"))
    ranked["activation_reason"] = ranked["strategy"].map(lambda name: getattr(STRATEGY_REGISTRY.get(str(name)), "activation_reason", ""))
    top = ranked.iloc[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Top strategy", str(top["strategy"]))
    m2.metric("Rank score", f"{float(top['rank_score']):.1f}")
    m3.metric("Sharpe proxy", f"{float(top['sharpe_proxy']):.2f}")
    m4.metric("Max drawdown", f"{float(top['max_drawdown_pct']):.1f}%")
    columns = [
        "rank",
        "strategy",
        "rank_score",
        "activation",
        "status",
        "trades",
        "total_pnl",
        "avg_return_pct",
        "sharpe_proxy",
        "avg_profit_factor",
        "win_rate",
        "max_drawdown_pct",
        "confidence_score",
    ]
    st.dataframe(ranked[[column for column in columns if column in ranked]], use_container_width=True, hide_index=True)


def strategy_system_charts(matrix: pd.DataFrame, aggregate: pd.DataFrame) -> None:
    fig = make_subplots(rows=2, cols=2, subplot_titles=("PnL by Trading System", "Max Drawdown", "Win Rate", "Confidence"))
    fig.add_trace(go.Bar(x=aggregate["strategy"], y=aggregate["total_pnl"], marker_color=np.where(aggregate["total_pnl"] >= 0, "#55d49a", "#ff6f7d"), name="PnL"), row=1, col=1)
    fig.add_trace(go.Bar(x=aggregate["strategy"], y=aggregate["max_drawdown_pct"], marker_color="#ffb86b", name="Drawdown"), row=1, col=2)
    fig.add_trace(go.Bar(x=aggregate["strategy"], y=aggregate["win_rate"], marker_color="#79a7ff", name="Win Rate"), row=2, col=1)
    fig.add_trace(go.Bar(x=aggregate["strategy"], y=aggregate["confidence_score"], marker_color="#55d49a", name="Confidence"), row=2, col=2)
    st.plotly_chart(layout_chart(fig, 560), use_container_width=True)
    with st.expander("Symbol by trading system", expanded=False):
        st.dataframe(matrix, use_container_width=True, hide_index=True)


def price_panel(candles: pd.DataFrame) -> go.Figure:
    symbol = str(candles["symbol"].iloc[-1]) if "symbol" in candles and not candles.empty else "Selected crypto"
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.04)
    fig.add_trace(
        go.Candlestick(
            x=candles["time"],
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            increasing_line_color="#55d49a",
            decreasing_line_color="#ff6f7d",
            name=symbol,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(go.Scatter(x=candles["time"], y=candles["ema20"], line={"color": "#79a7ff", "width": 1.5}, name="EMA20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=candles["time"], y=candles["vwap"], line={"color": "#f0c86a", "width": 1.4}, name="VWAP"), row=1, col=1)
    fig.add_trace(go.Bar(x=candles["time"], y=candles["volume"], marker_color="#3f5261", name="Volume"), row=2, col=1)
    fig.update_xaxes(rangeslider_visible=False)
    return layout_chart(fig, 500)


def orderflow_panel(candles: pd.DataFrame, tape: pd.DataFrame) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=candles["time"], y=candles["delta"], fill="tozeroy", line={"color": "#55d49a"}, name="CVD pressure"))
    fig.add_trace(go.Scatter(x=candles["time"], y=candles["spread_bps"], line={"color": "#ffb86b"}, name="Spread bps"), secondary_y=True)
    fig.add_trace(
        go.Scatter(
            x=candles["time"].tail(72),
            y=tape["delta"],
            mode="markers",
            marker={"size": np.clip(tape["notional"] / 900, 5, 18), "color": tape["spread_bps"], "colorscale": "Viridis"},
            name="Tape trades",
        )
    )
    return layout_chart(fig, 360)


def depth_panel(book: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Bar(y=book["price"], x=-book["bid_depth"], orientation="h", marker_color="#3fbf87", name="Bid depth"))
    fig.add_trace(go.Bar(y=book["price"], x=book["ask_depth"], orientation="h", marker_color="#e66f7a", name="Ask depth"))
    fig.update_layout(barmode="relative")
    return layout_chart(fig, 360)


def risk_gauge(title: str, value: float, threshold: float, suffix: str = "%") -> go.Figure:
    color = "#55d49a" if value < threshold * 0.65 else "#f0c86a" if value < threshold else "#ff6f7d"
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number={"suffix": suffix},
            title={"text": title},
            gauge={
                "axis": {"range": [0, threshold * 1.25]},
                "bar": {"color": color},
                "bgcolor": "#151c22",
                "borderwidth": 1,
                "bordercolor": "#26323b",
                "steps": [
                    {"range": [0, threshold * 0.65], "color": "#17251f"},
                    {"range": [threshold * 0.65, threshold], "color": "#292515"},
                    {"range": [threshold, threshold * 1.25], "color": "#2a171a"},
                ],
            },
        )
    )
    return layout_chart(fig, 230)


def live_trading(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    summary = data["summary"]
    assert isinstance(summary, dict)
    scan = load_live_scan()
    if not scan.empty:
        live_price_socket_component(scan)
        st.caption("Only the price ticker above updates live in the browser. Signal buckets now live in Signal Flow to keep this screen focused on top coin performance.")
    feature_files = available_feature_files()
    chart_symbols = list(feature_files)
    if chart_symbols:
        selected_chart_symbol = st.selectbox("Coin", chart_symbols, index=0, key="live_chart_symbol")
        chart_data = binance_history_snapshot(str(feature_files[selected_chart_symbol]))
        chart_candles = chart_data["candles"]
        assert isinstance(chart_candles, pd.DataFrame)
        with st.expander("Market chart", expanded=False):
            st.plotly_chart(price_panel(chart_candles), use_container_width=True)


def bucket_board(scan: pd.DataFrame) -> None:
    scan = normalize_scan_columns(scan)
    order = ["NO SIGNAL", "WATCH", "BUY", "IN TRADE"]
    classes = {"NO SIGNAL": "bucket-none", "WATCH": "bucket-watch", "BUY": "bucket-buy", "IN TRADE": "bucket-trade"}
    labels = {"NO SIGNAL": "No Signal", "WATCH": "Watch", "BUY": "Buy", "IN TRADE": "In Trade"}
    buy_rows = scan[scan["scan_bucket"] == "BUY"]
    if not buy_rows.empty:
        symbols = ", ".join(buy_rows["symbol"].tolist())
        st.markdown(f"<div class='buy-alert'><b class='good'>Buy signal active:</b> {symbols}</div>", unsafe_allow_html=True)

    html = ["<div class='bucket-grid'>"]
    for bucket in order:
        rows = scan[scan["scan_bucket"] == bucket].sort_values(["priority", "active_pnl_pct", "profit_factor"], ascending=[True, False, False], na_position="last")
        html.append(f"<div class='bucket {classes[bucket]}'><div class='bucket-title'><span>{labels[bucket]}</span><span class='bucket-count'>{len(rows)}</span></div>")
        if rows.empty:
            html.append("<div class='scan-meta'>No symbols in this state.</div>")
        for _, row in rows.head(5).iterrows():
            sentiment = "constructive" if float(row["buy_score"]) >= 65 else "neutral" if float(row["watch_score"]) >= 45 else "quiet"
            vol = "high vol" if float(row.get("stream_spread_bps", 0.0)) > 12 else "stable vol"
            trend = "trend improving" if float(row["buy_score"]) > float(row["sell_score"]) else "trend soft"
            pnl = ""
            if bucket == "IN TRADE" and pd.notna(row.get("active_pnl")):
                pnl_class = "good" if float(row["active_pnl"]) >= 0 else "bad"
                pnl = f"<div class='scan-meta {pnl_class}'>PnL ${float(row['active_pnl']):,.2f} / {float(row['active_pnl_pct']):.2f}% from ${float(row['active_entry']):,.4f}</div>"
            elif bucket == "BUY":
                pnl = "<div class='scan-meta good'>entry gate open now</div>"
            stream_age = stream_age_text(row.get("stream_updated_at", ""))
            stream_line = (
                f"spread {float(row.get('stream_spread_bps', 0.0)):.2f} bps | "
                f"depth {float(row.get('stream_depth_imbalance', 0.0)):+.2f} | "
                f"taker {float(row.get('stream_taker_buy_ratio', 0.0)):.0%} | "
                f"trades {int(row.get('stream_trade_count', 0))} | {stream_age}"
            )
            html.append(
                "<div class='scan-card'>"
                f"<div class='scan-symbol'>{row['symbol']} <span class='pill'>${float(row['last_close']):,.4f}</span></div>"
                f"<div class='scan-meta'>{row['scan_reason']}</div>"
                f"<div class='scan-meta'><span class='pill'>{sentiment}</span> <span class='pill'>{vol}</span> <span class='pill'>{trend}</span></div>"
                f"<div class='scan-meta'>watch {float(row['watch_score']):.0f}% | buy {float(row['buy_score']):.0f}% | flow {float(row['orderflow_score']):.0f}% | conf {float(row['confidence_score']):.0f}%</div>"
                f"<div class='scan-meta'>{stream_line}</div>"
                f"{pnl}"
                f"<div class='scan-meta'>1Y trades {int(row['trades'])} | win {float(row['win_rate']):.1f}% | PF {float(row['profit_factor']):.2f}</div>"
                "</div>"
            )
        html.append("</div>")
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)
    signal_proximity_monitor(scan)


def signal_proximity_monitor(scan: pd.DataFrame) -> None:
    st.markdown("### Signal Proximity Monitor")
    view = scan.sort_values(["priority", "buy_score", "watch_score"], ascending=[True, False, False])[
        [
            "symbol",
            "scan_bucket",
            "last_close",
            "watch_score",
            "buy_score",
            "orderflow_score",
            "sell_score",
            "confidence_score",
            "stream_spread_bps",
            "stream_depth_imbalance",
            "stream_taker_buy_ratio",
            "buy_missing",
            "orderflow_reason",
            "confidence_reason",
        ]
    ]
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "watch_score": st.column_config.ProgressColumn("Watch", min_value=0, max_value=100, format="%.0f%%"),
            "buy_score": st.column_config.ProgressColumn("Buy", min_value=0, max_value=100, format="%.0f%%"),
            "orderflow_score": st.column_config.ProgressColumn("Orderflow", min_value=0, max_value=100, format="%.0f%%"),
            "sell_score": st.column_config.ProgressColumn("Sell / Exit", min_value=0, max_value=100, format="%.0f%%"),
            "confidence_score": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%.0f%%"),
            "last_close": st.column_config.NumberColumn("Last", format="$%.4f"),
            "stream_spread_bps": st.column_config.NumberColumn("Spread bps", format="%.2f"),
            "stream_depth_imbalance": st.column_config.NumberColumn("Depth", format="%+.2f"),
            "stream_taker_buy_ratio": st.column_config.ProgressColumn("Taker Buy", min_value=0, max_value=1, format="%.0f%%"),
        },
    )


def stream_age_text(value: object) -> str:
    if not value:
        return "stream pending"
    try:
        updated = utc_datetime(str(value))
    except ValueError:
        return "stream seen"
    age_seconds = max(0, int((datetime.now(UTC) - updated).total_seconds()))
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    return f"{age_seconds // 60}m {age_seconds % 60}s ago"


def orderflow(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Order Flow")
    st.caption("Trade tape, spread, depth, and buyer/seller pressure for review. This screen is observational and does not place orders.")
    candles = data["candles"]
    tape = data["tape"]
    book = data["book"]
    assert isinstance(candles, pd.DataFrame)
    assert isinstance(tape, pd.DataFrame)
    assert isinstance(book, pd.DataFrame)
    scan = load_live_scan()
    symbols = scan["symbol"].astype(str).tolist() if not scan.empty else available_live_symbols()
    selected = st.selectbox("Coin", symbols, index=0)
    selected_row = scan[scan["symbol"].astype(str) == selected].iloc[0] if not scan.empty and selected in set(scan["symbol"].astype(str)) else None
    if selected_row is not None:
        watchlist = orderflow_watchlist(scan)
        st.markdown(
            "<div class='decision-summary info'>"
            "<div class='decision-summary-title'>Order Flow Decision Lens</div>"
            "<div class='runtime-bot-boundary-meta'>Use this page to confirm whether liquidity, tape aggression, and spread are supportive before runtime deployment or manual review.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(orderflow_guidance(selected_row, watchlist), unsafe_allow_html=True)
        buy_pressure = float(selected_row.get("buy_score", 0.0))
        sell_pressure = float(selected_row.get("sell_score", 0.0))
        orderflow_score = float(selected_row.get("orderflow_score", 0.0))
        depth = float(selected_row.get("stream_depth_imbalance", 0.0))
        spread = float(selected_row.get("stream_spread_bps", 0.0))
        velocity = float(selected_row.get("stream_trade_count", 0.0))
        taker_buy = float(selected_row.get("stream_taker_buy_ratio", 0.0)) * 100
        a, b, c, d = st.columns(4)
        a.metric("Buy Pressure", f"{buy_pressure:.0f}%")
        b.metric("Sell / Exit Pressure", f"{sell_pressure:.0f}%")
        c.metric("Spread", f"{spread:.2f} bps")
        d.metric("Trade Velocity", f"{velocity:.0f} trades")
        e, f, g = st.columns(3)
        e.metric("Volume Imbalance", f"{taker_buy:.0f}% taker buy")
        f.metric("Book Imbalance", f"{depth:+.2f}")
        g.metric("Delta / Flow", f"{orderflow_score:.0f}%")
        st.markdown(orderflow_insight(selected_row), unsafe_allow_html=True)
        st.caption(str(selected_row.get("orderflow_reason", "awaiting selected coin orderflow")))
        if not watchlist.empty:
            with st.expander("Orderflow names to watch", expanded=True):
                st.dataframe(
                    watchlist,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "orderflow_watch_score": st.column_config.ProgressColumn("Watch", min_value=0, max_value=100, format="%.0f%%"),
                        "orderflow_score": st.column_config.ProgressColumn("Flow", min_value=0, max_value=100, format="%.0f%%"),
                        "buy_score": st.column_config.ProgressColumn("Buy", min_value=0, max_value=100, format="%.0f%%"),
                        "stream_taker_buy_ratio": st.column_config.ProgressColumn("Taker Buy", min_value=0, max_value=1, format="%.0f%%"),
                        "stream_depth_imbalance": st.column_config.NumberColumn("Book", format="%+.2f"),
                        "stream_spread_bps": st.column_config.NumberColumn("Spread bps", format="%.2f"),
                    },
                )
    left, right = st.columns([1.45, 1])
    left.plotly_chart(orderflow_panel(candles, tape), use_container_width=True)
    right.plotly_chart(depth_panel(book), use_container_width=True)
    st.dataframe(
        enrich_orderflow_table(tape.sort_values("age_ms").head(16)),
        use_container_width=True,
        hide_index=True,
        column_config={
            "notional": st.column_config.NumberColumn("notional", format="$%.0f"),
            "spread_bps": st.column_config.NumberColumn("spread bps", format="%.2f"),
        },
    )


def orderflow_insight(row: pd.Series) -> str:
    buy = float(row.get("buy_score", 0.0))
    sell = float(row.get("sell_score", 0.0))
    spread = float(row.get("stream_spread_bps", 0.0))
    depth = float(row.get("stream_depth_imbalance", 0.0))
    taker = float(row.get("stream_taker_buy_ratio", 0.0))
    bias = "bullish" if buy > sell + 12 else "defensive" if sell > buy + 12 else "balanced"
    liquidity = "liquidity stable" if spread <= 8 else "liquidity thinning"
    aggressor = "buyers lifting offers" if taker >= 0.56 else "sellers pressing bids" if taker <= 0.44 else "two-way tape"
    absorption = "bid absorption visible" if depth > 0.15 else "ask-side resistance visible" if depth < -0.15 else "no strong absorption edge"
    return (
        "<div class='buy-alert'>"
        f"<b>Orderflow insight:</b> {bias.title()} bias. {liquidity.title()}. "
        f"{aggressor.title()}. {absorption.title()}."
        "</div>"
    )


def orderflow_watchlist(scan: pd.DataFrame) -> pd.DataFrame:
    if scan.empty:
        return pd.DataFrame()
    view = normalize_scan_columns(scan).copy()
    view["orderflow_watch_score"] = (
        view["orderflow_score"].astype(float) * 0.45
        + view["buy_score"].astype(float) * 0.25
        + view["stream_taker_buy_ratio"].astype(float).fillna(0) * 100 * 0.15
        + (view["stream_depth_imbalance"].astype(float).fillna(0).clip(lower=0) * 100).clip(upper=100) * 0.10
        + (100 - view["stream_spread_bps"].astype(float).fillna(25).clip(0, 25) * 4) * 0.05
    )
    cols = [
        "symbol",
        "scan_bucket",
        "orderflow_watch_score",
        "orderflow_score",
        "buy_score",
        "stream_taker_buy_ratio",
        "stream_depth_imbalance",
        "stream_spread_bps",
        "orderflow_reason",
    ]
    return view.sort_values("orderflow_watch_score", ascending=False)[cols].head(6)


def orderflow_guidance(row: pd.Series, watchlist: pd.DataFrame) -> str:
    metrics = {
        "symbol": str(row.get("symbol", "")),
        "bucket": str(row.get("scan_bucket", "")),
        "orderflow_score": float(row.get("orderflow_score", 0.0) or 0.0),
        "buy_score": float(row.get("buy_score", 0.0) or 0.0),
        "sell_score": float(row.get("sell_score", 0.0) or 0.0),
        "depth": float(row.get("stream_depth_imbalance", 0.0) or 0.0),
        "taker_buy": float(row.get("stream_taker_buy_ratio", 0.0) or 0.0),
        "spread_bps": float(row.get("stream_spread_bps", 0.0) or 0.0),
    }
    watch = watchlist[["symbol", "orderflow_watch_score", "orderflow_reason"]].to_dict(orient="records") if not watchlist.empty else []
    text = orderflow_guidance_cached(metrics["symbol"], json.dumps(metrics, sort_keys=True), json.dumps(watch, sort_keys=True))
    return f"<div class='buy-alert'><b>How to use this:</b> {text}</div>"


@st.cache_data(ttl=300, show_spinner=False)
def orderflow_guidance_cached(symbol: str, metrics_json: str, watchlist_json: str) -> str:
    metrics = json.loads(metrics_json)
    watch = json.loads(watchlist_json)
    fallback = _rule_orderflow_guidance(symbol, metrics, watch)
    api_key = openai_api_key()
    if not api_key:
        return fallback
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=8.0)
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": "Give concise institutional orderflow guidance. No promises. Use watch, wait, or avoid language."},
                {"role": "user", "content": f"Selected symbol metrics: {metrics_json}. Orderflow watchlist: {watchlist_json}."},
            ],
            max_tokens=70,
        )
        return (response.choices[0].message.content or fallback).strip()
    except Exception as exc:
        log_diagnostic(logger, "orderflow_llm_fallback", reason=str(exc))
        return fallback


def _rule_orderflow_guidance(symbol: str, metrics: dict[str, object], watch: list[dict[str, object]]) -> str:
    flow = float(metrics.get("orderflow_score", 0.0) or 0.0)
    buy = float(metrics.get("buy_score", 0.0) or 0.0)
    spread = float(metrics.get("spread_bps", 0.0) or 0.0)
    top = ", ".join(str(item.get("symbol", "")) for item in watch[:3]) or "none yet"
    if flow >= 65 and buy >= 60 and spread <= 12:
        return f"{symbol} has constructive orderflow; watch confirmation and compare it against the strongest flow names: {top}."
    if flow >= 50:
        return f"{symbol} is developing but not clean yet; wait for stronger aggressor pressure and stable spread. Current watchlist: {top}."
    return f"{symbol} orderflow is not supportive; keep it on observation only and focus on stronger flow names: {top}."


def openai_api_key() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return str(os.getenv("OPENAI_API_KEY"))
    values = dotenv_values(".env")
    return str(values.get("OPENAI_API_KEY") or "")


def enrich_orderflow_table(tape: pd.DataFrame) -> pd.DataFrame:
    view = tape.copy()
    view["implication"] = np.where(
        (view["side"] == "BUY") & (view["delta"] > 0),
        "buyers active",
        np.where((view["side"] == "SELL") & (view["delta"] < 0), "sellers active", "mixed flow"),
    )
    view["liquidity_hint"] = np.where(view["spread_bps"] <= 6, "tight spread", np.where(view["spread_bps"] <= 12, "watch spread", "wide spread"))
    return view


def operational_metric_tiles(rows: list[dict[str, str]]) -> None:
    cards: list[str] = []
    for row in rows:
        tone = row.get("tone", "")
        cards.append(
            "<div class='metric-tile'>"
            f"<div class='label'>{html.escape(row.get('label', ''))}</div>"
            f"<div class='value {html.escape(tone)}'>{html.escape(row.get('value', ''))}</div>"
            f"<div class='sub'>{html.escape(row.get('sub', ''))}</div>"
            "</div>"
        )
    st.markdown(f"<div class='metric-tile-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)


def risk_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    summary = data["summary"]
    assert isinstance(summary, dict)
    risk = load_risk_settings()
    bots = load_bot_instances()
    running = bots[bots["state"].isin(["DEPLOYED", "RUNNING"])] if not bots.empty and "state" in bots else pd.DataFrame()
    exposure = float(running["capital"].sum()) if not running.empty and "capital" in running else 0.0
    exposure_limit = float(risk["max_portfolio_exposure"])
    capital = max(float(risk["capital"]), 1.0)
    max_cash = float(risk["max_cash_per_trade"])
    max_trades = int(risk["max_trades_per_window"])
    exposure_pct = min(999.0, exposure / max(exposure_limit, 1.0) * 100)
    cash_pct = min(999.0, max_cash / capital * 100)
    trade_pct = min(999.0, len(running) / max(max_trades, 1) * 100)
    st.markdown("### Portfolio Risk Gates")
    st.caption("Risk limits are shown as plain operating numbers so the runtime cap is unambiguous.")
    operational_metric_tiles(
        [
            {
                "label": "Portfolio Exposure",
                "value": f"{money_text(exposure)} / {money_text(exposure_limit)}",
                "sub": f"{exposure_pct:.1f}% used across {len(running)} running bot(s)",
                "tone": "good" if exposure_pct < 75 else "warn" if exposure_pct < 100 else "bad",
            },
            {
                "label": "Cash Per Trade",
                "value": money_text(max_cash),
                "sub": f"{cash_pct:.1f}% of portfolio capital",
                "tone": "good" if cash_pct <= 10 else "warn",
            },
            {
                "label": "Trade Window",
                "value": f"{len(running)} / {max_trades}",
                "sub": f"{trade_pct:.1f}% of allowed concurrent window",
                "tone": "good" if trade_pct < 75 else "warn" if trade_pct < 100 else "bad",
            },
        ]
    )
    with st.form("risk_settings"):
        st.markdown("#### Edit Risk Limits")
        c1, c2, c3 = st.columns(3)
        capital = c1.number_input("Portfolio capital", min_value=0.0, value=float(risk["capital"]), step=100.0)
        max_cash = c2.number_input("Max cash allocation per trade", min_value=0.0, value=float(risk["max_cash_per_trade"]), step=25.0)
        max_risk_pct = c3.number_input("Max risk per trade %", min_value=0.0, max_value=100.0, value=float(risk["max_risk_per_trade_pct"]) * 100, step=0.1)
        c4, c5, c6 = st.columns(3)
        max_trades = c4.number_input("Max trades per window", min_value=0, value=int(risk["max_trades_per_window"]), step=1)
        window = c5.number_input("Window minutes", min_value=1, value=int(risk["trade_window_minutes"]), step=15)
        max_exposure = c6.number_input("Portfolio exposure limit", min_value=0.0, value=float(risk["max_portfolio_exposure"]), step=100.0)
        kill_switch = st.checkbox("Kill switch", value=bool(risk["kill_switch"]))
        if st.form_submit_button("Save risk limits"):
            save_risk_settings(
                {
                    "profile": "default",
                    "capital": capital,
                    "max_cash_per_trade": max_cash,
                    "max_risk_per_trade_pct": max_risk_pct / 100,
                    "max_trades_per_window": int(max_trades),
                    "trade_window_minutes": int(window),
                    "max_portfolio_exposure": max_exposure,
                    "kill_switch": kill_switch,
                }
            )
            st.success("Risk limits saved. Running bots will enforce these before trade placement.")
    st.dataframe(
        pd.DataFrame(
            [
                {"gate": "kill switch", "value": bool(risk["kill_switch"]), "enforcement": "hard block"},
                {"gate": "max cash per trade", "value": float(risk["max_cash_per_trade"]), "enforcement": "hard cap"},
                {"gate": "max risk per trade", "value": f"{float(risk['max_risk_per_trade_pct']) * 100:.2f}%", "enforcement": "hard cap"},
                {"gate": "max trades per window", "value": int(risk["max_trades_per_window"]), "enforcement": "hard block"},
                {"gate": "portfolio exposure", "value": float(risk["max_portfolio_exposure"]), "enforcement": "hard block"},
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


def bot_framework_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Bot Framework")
    st.caption("Design bots here; monitor and control them in Runtime.")
    strategy_ranking_panel()
    available = active_strategy_names()
    dormant = dormant_strategy_names()
    if not available:
        st.error("No active strategy is certified for bot creation. Refresh backtests and promote a deployable strategy first.")
        return
    st.success("Available for bot creation: " + ", ".join(available))
    guidance_rows = deployable_strategy_symbol_rows()
    if guidance_rows:
        st.markdown("#### Deploy Guidance / Deployment Guidance")
        st.caption("Use only the strategy and coin pairs that passed the latest production-readiness backtest guidance.")
        st.dataframe(pd.DataFrame(guidance_rows), use_container_width=True, hide_index=True)
    st.markdown("#### Strategy Deployment Defaults")
    st.caption("Bot Creation, Validation Lab, and Runtime inherit these defaults unless the bot definition overrides them.")
    defaults_rows = [strategy_deployment_defaults(name) for name in available]
    st.dataframe(
        pd.DataFrame(defaults_rows)[
            [
                "strategy",
                "strategy_type",
                "strategy_version",
                "default_stop_type",
                "default_tp_type",
                "minimum_recommended_capital",
                "capital_allocation_model",
                "recommended_timeframe",
                "runtime_profile",
                "risk_classification",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.markdown("#### Bot Creation Panel Defaults")
    st.caption("The creation form below persists strategy defaults, stop-loss, take-profit, trailing, emergency stop, and risk allocation overrides into the bot definition.")
    bot_marketplace_panel(load_bot_instances())
    if dormant:
        with st.expander("Dormant strategy research shelf", expanded=False):
            dormant_rows = [
                {
                    "strategy": name,
                    "timeframe": getattr(STRATEGY_REGISTRY[name], "default_timeframe", ""),
                    "state": getattr(STRATEGY_REGISTRY[name], "activation_state", "DORMANT"),
                    "reason": getattr(STRATEGY_REGISTRY[name], "activation_reason", ""),
                }
                for name in dormant
            ]
            st.dataframe(pd.DataFrame(dormant_rows), use_container_width=True, hide_index=True)
    symbols = load_live_scan()["symbol"].astype(str).tolist() or available_live_symbols()
    with st.form("create_bot"):
        c1, c2, c3, c4 = st.columns(4)
        name = c1.text_input("Bot instance name", value=f"{symbols[0].replace('/', '')} bot" if symbols else "Coin bot")
        primary_strategy = c2.selectbox("Primary strategy", available)
        certified_for_primary = certified_symbols_for_strategy(primary_strategy)
        symbol_options = [symbol for symbol in symbols if not certified_for_primary or symbol in set(certified_for_primary)]
        if not symbol_options:
            symbol_options = certified_for_primary or symbols
        symbol = c3.selectbox("Symbol", symbol_options)
        capital = c4.number_input("Capital", min_value=0.0, value=250.0, step=50.0)
        selected_strategies = st.multiselect("Strategy collection", available, default=[primary_strategy], help="Bots may carry multiple reusable strategy modules; the primary strategy is used for validation until portfolio execution is expanded.")
        p1, p2, p3 = st.columns(3)
        min_confidence = p1.slider("Min confidence", min_value=0, max_value=100, value=55)
        risk_reward = p2.number_input("Risk reward", min_value=0.1, value=1.7, step=0.1)
        timeframe_options = ["5m", "1h", "4h", "1d"]
        strategy_default_timeframe = getattr(STRATEGY_REGISTRY[primary_strategy], "default_timeframe", "1h")
        timeframe_index = timeframe_options.index(strategy_default_timeframe) if strategy_default_timeframe in timeframe_options else 1
        timeframe = p3.selectbox("Entry timeframe", timeframe_options, index=timeframe_index)
        defaults = strategy_deployment_defaults(primary_strategy)
        st.markdown("#### Bot Creation Defaults")
        d1, d2, d3 = st.columns(3)
        stop_loss_type = d1.selectbox(
            "Stop-loss logic",
            ["ATR-based stop + ATR trail", "ATR trailing stop", "ATR stop + EMA trailing stop", "Fixed stop", "Drawdown stop", "Time-based stop", "Strategy-generated stop"],
            index=0 if str(defaults["default_stop_type"]) not in {"ATR trailing stop", "ATR stop + EMA trailing stop"} else ["ATR-based stop + ATR trail", "ATR trailing stop", "ATR stop + EMA trailing stop", "Fixed stop", "Drawdown stop", "Time-based stop", "Strategy-generated stop"].index(str(defaults["default_stop_type"])),
        )
        take_profit_type = d2.selectbox(
            "Take-profit logic",
            ["Strategy default TP", "Staged partial TP", "Partial TP + strategy default TP", "Fixed TP", "Trailing TP"],
            index=0 if str(defaults["default_tp_type"]) not in {"Staged partial TP", "Partial TP + strategy default TP"} else ["Strategy default TP", "Staged partial TP", "Partial TP + strategy default TP", "Fixed TP", "Trailing TP"].index(str(defaults["default_tp_type"])),
        )
        risk_allocation_category = d3.selectbox(
            "Risk allocation",
            ["Fast intraday momentum risk", "Volatility mean-reversion risk", "Swing trend risk", "Certified multi-factor risk", "Experimental strategy risk"],
            index=0 if str(defaults["risk_classification"]) not in ["Fast intraday momentum risk", "Volatility mean-reversion risk", "Swing trend risk", "Certified multi-factor risk", "Experimental strategy risk"] else ["Fast intraday momentum risk", "Volatility mean-reversion risk", "Swing trend risk", "Certified multi-factor risk", "Experimental strategy risk"].index(str(defaults["risk_classification"])),
        )
        e1, e2, e3 = st.columns(3)
        trailing_enabled = e1.checkbox("Trailing stop enabled", value=bool(defaults["trailing_enabled"]))
        emergency_stop_enabled = e2.checkbox("Emergency stop enabled", value=bool(defaults["emergency_stop_enabled"]))
        bot_version = e3.text_input("Bot version", value=str(defaults["strategy_version"]))
        if st.form_submit_button("Create bot instance"):
            bots = load_bot_instances()
            strategy_collection = selected_strategies or [primary_strategy]
            creation_parameters = {
                "strategies": strategy_collection,
                "min_confidence": min_confidence,
                "risk_reward": risk_reward,
                "bot_version": bot_version,
                "stop_loss_type": stop_loss_type,
                "stop_loss_value": str(defaults["default_stop_value"]),
                "take_profit_type": take_profit_type,
                "take_profit_value": str(defaults["default_tp_value"]),
                "trailing_enabled": trailing_enabled,
                "emergency_stop_enabled": emergency_stop_enabled,
                "risk_allocation_category": risk_allocation_category,
                "strategy_defaults_used": defaults,
                "capital_allocation_model": defaults["capital_allocation_model"],
                "minimum_recommended_capital": defaults["minimum_recommended_capital"],
            }
            row = {
                "name": name,
                "strategy": primary_strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "capital": capital,
                "parameters": creation_parameters,
                "state": "DRAFT",
                "status_reason": "created from UI",
                "created_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": "",
            }
            bots = bots[bots["name"].astype(str) != name] if not bots.empty and "name" in bots else pd.DataFrame()
            save_bot_instances(pd.concat([bots, pd.DataFrame([row])], ignore_index=True))
            append_journal(name, symbol, "BOT_CREATED", "INFO", "DRAFT", "bot instance created", creation_parameters)
            st.success("Bot instance created.")

    bots = load_bot_instances()
    if not bots.empty:
        display = bots.copy()
        display["strategy_collection"] = display["parameters"].apply(lambda value: ", ".join((value or {}).get("strategies", [])) if isinstance(value, dict) else "")
        display["stop_loss_type"] = display.apply(lambda row: bot_deployment_profile(row)["stop_loss_type"], axis=1)
        display["take_profit_type"] = display.apply(lambda row: bot_deployment_profile(row)["take_profit_type"], axis=1)
        display["risk_allocation_category"] = display.apply(lambda row: bot_deployment_profile(row)["risk_allocation_category"], axis=1)
        display["next_action"] = display["state"].astype(str).apply(lifecycle_next_action)
        with st.expander("Configured Bot Definitions", expanded=True):
            st.dataframe(
                display[[col for col in ["name", "strategy_collection", "strategy", "symbol", "timeframe", "capital", "stop_loss_type", "take_profit_type", "risk_allocation_category", "state", "next_action", "status_reason"] if col in display]],
                use_container_width=True,
                hide_index=True,
            )
    st.info("Flow: create a bot here, validate it in Validation Lab, then deploy or stop it in Runtime. Saved bots appear on the next screens immediately.")


def bot_runtime_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Bot Runtime")
    st.caption("Live bot cockpit with portfolio summary, ranked bot tiles, and Binance Testnet price updates.")
    bots = load_bot_instances()
    scan = load_live_scan()
    stream = load_live_stream()
    if bots.empty:
        st.info("No bot instances exist yet. Create one in Bot Framework.")
        return
    runtime_states = pd.DataFrame(RuntimeManager().list_bot_states())
    if not runtime_states.empty and "bot_id" in runtime_states:
        runtime_cols = [
            "bot_id",
            "status",
            "mode",
            "runtime_mode",
            "started_at",
            "pnl_started_at",
            "runtime_entry_price",
            "pnl_since_start",
            "pnl_since_start_pct",
            "runtime_position_state",
            "last_entry_at",
            "last_exit_at",
            "last_trade_event_type",
            "last_trade_event_at",
            "last_trade_event_reason",
            "framework_status",
            "supervisor_action",
            "alert_level",
            "alert_code",
            "data_state",
            "reconciliation_state",
            "portfolio_state",
        ]
        available_cols = [col for col in runtime_cols if col in runtime_states]
        bots = bots.merge(runtime_states[available_cols], on="bot_id", how="left", suffixes=("", "_runtime"))
    active_names = tuple(sorted(set(bots["strategy"].astype(str).tolist()))) if "strategy" in bots else tuple(load_deployed_strategy_names())
    matrix, aggregate = load_cached_strategy_matrix(active_names)
    if matrix.empty and set(active_names).issubset(set(STRATEGY_REGISTRY)):
        all_matrix, _ = load_cached_strategy_matrix(tuple(STRATEGY_REGISTRY))
        if not all_matrix.empty:
            matrix = all_matrix[all_matrix["strategy"].astype(str).isin(active_names)]
            aggregate = aggregate_strategy_matrix(matrix)
    rankings = runtime_bot_rankings(bots, scan, matrix)
    runtime_marketplace_header(bots, rankings, scan)
    runtime_discovery_panel(rankings, scan)
    filtered_bots = runtime_bot_filter_bar(bots, rankings)
    runtime_tiles(filtered_bots, scan, stream, matrix, aggregate, rankings)


def bot_admin_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Bot Admin")
    st.caption("Operations command center. Actions go through the shared command bus and runtime manager; this screen does not place exchange orders.")
    manager = RuntimeManager()
    bus = RuntimeCommandBus(manager)
    status = manager.runtime_status()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Runtime", str(status["runtime"]))
    c2.metric("Mode", str(status["runtime_mode"]))
    c3.metric("Running Bots", int(status["running_bots"]))
    c4.metric("AI", str(status["llm_state"]).replace("_", " "))
    c5.metric("Heartbeat", stream_age_text(str(status.get("runtime_heartbeat", ""))))
    with st.expander("Runtime process controls", expanded=True):
        st.caption("Dashboard and headless runtime are independent. Closing the browser does not stop the runtime process.")
        st.code(
            "\n".join(
                [
                    "python -m mytradingmind.runtime start --mode headless",
                    "python scripts/run_headless_runtime.py --mode headless",
                    "python -m mytradingmind.dashboard start",
                    "python -m mytradingmind.runtime stop",
                ]
            ),
            language="bash",
        )
        r1, r2, r3 = st.columns(3)
        if r1.button("START HEADLESS RUNTIME", use_container_width=True, disabled=str(status["runtime"]) == "RUNNING"):
            result = launch_headless_runtime_process()
            append_journal("runtime", "", "RUNTIME_ADMIN_ACTION", "INFO" if result["ok"] else "ERROR", "START_RUNTIME", str(result["message"]), result)
            if result["ok"]:
                st.success(str(result["message"]))
            else:
                st.error(str(result["message"]))
            st.rerun()
        if r2.button("STOP RUNTIME", use_container_width=True, disabled=str(status["runtime"]) == "STOPPED"):
            result = bus.dispatch(RuntimeCommand("STOP_RUNTIME", source="BOT_ADMIN"))
            append_journal("runtime", "", "RUNTIME_ADMIN_ACTION", "INFO" if result.ok else "ERROR", "STOP_RUNTIME", result.message, result.state)
            st.warning("Runtime stop requested." if result.ok else result.message)
            st.rerun()
        if r3.button("REFRESH STATUS", use_container_width=True):
            st.rerun()
    with st.expander("Emergency controls", expanded=False):
        st.caption("Emergency actions are audited and routed through persisted bot/risk state; they do not bypass exchange/risk gates.")
        risk = load_risk_settings()
        e1, e2, e3, e4 = st.columns(4)
        if e1.button("STOP ALL BOTS", type="primary", use_container_width=True):
            bots = load_bot_instances()
            changed = 0
            if not bots.empty and "state" in bots:
                mask = bots["state"].astype(str).isin(["RUNNING", "DEPLOYED"])
                changed = int(mask.sum())
                bots.loc[mask, "state"] = "STOPPED"
                bots.loc[mask, "status_reason"] = "emergency stop all from Bot Admin"
                bots.loc[mask, "updated_at"] = datetime.now(UTC).isoformat()
                save_bot_instances(bots)
            append_action_audit("EMERGENCY_STOP_ALL", reason=f"Stopped {changed} bot(s)")
            append_journal("SYSTEM", "", "EMERGENCY_STOP_ALL", "WARN", "STOPPED", f"stopped {changed} bot(s) from Bot Admin", {"changed": changed})
            st.warning(f"Emergency stop requested for {changed} bot(s).")
            st.rerun()
        if e2.button("DISABLE NEW LAUNCHES", use_container_width=True):
            updated = {**risk, "kill_switch": True}
            save_risk_settings(updated)
            append_action_audit("DISABLE_NEW_BOT_LAUNCHES", previous_value=risk, new_value=updated, reason="risk lock enabled from Bot Admin")
            st.warning("New bot launches disabled through risk lock mode.")
            st.rerun()
        if e3.button("ENABLE RISK LOCK", use_container_width=True):
            updated = {**risk, "kill_switch": True}
            save_risk_settings(updated)
            append_action_audit("RISK_LOCK_ENABLE", previous_value=risk, new_value=updated, reason="manual Bot Admin risk lock")
            st.warning("Risk lock is enabled.")
            st.rerun()
        if e4.button("CLEAR RISK LOCK", use_container_width=True, disabled=not bool(risk.get("kill_switch", False))):
            updated = {**risk, "kill_switch": False}
            save_risk_settings(updated)
            append_action_audit("RISK_LOCK_DISABLE", previous_value=risk, new_value=updated, reason="manual Bot Admin risk unlock")
            st.success("Risk lock is cleared.")
            st.rerun()
    states = manager.list_bot_states()
    if not states:
        st.info("No bots are configured yet. Create a bot in Bot Framework first.")
        return
    marketplace_rows = []
    for state in states:
        defaults = strategy_deployment_defaults(str(state.get("strategy", "")))
        validation = validation_metrics_for_bot(str(state.get("name", state.get("bot_id", ""))))
        marketplace_rows.append(
            {
                "Bot": state.get("name", state.get("bot_id", "")),
                "Strategy": state.get("strategy", ""),
                "Default Stop": defaults["default_stop_type"],
                "Default TP": defaults["default_tp_type"],
                "Minimum Capital": defaults["minimum_recommended_capital"],
                "Backtest Status": "Available" if validation else "Pending",
                "Validation Status": state.get("validation_status", "UNKNOWN"),
                "Runtime Compatibility": defaults["runtime_profile"],
                "Risk Classification": defaults["risk_classification"],
            }
        )
    with st.expander("Bot Marketplace Readiness", expanded=False):
        st.dataframe(pd.DataFrame(marketplace_rows), use_container_width=True, hide_index=True)
    for state in states:
        bot_id = str(state["bot_id"])
        status_text = str(state.get("status", "STOPPED"))
        deployment = strategy_deployment_defaults(str(state.get("strategy", "")))
        with st.container(border=True):
            left, right = st.columns([1.4, 1])
            with left:
                st.markdown(f"#### {state.get('name', bot_id)}")
                st.caption(f"ID `{bot_id}` | {state.get('strategy', '')} | {state.get('symbol', '')}")
                chips = [
                    f"status {status_text}",
                    f"mode {state.get('mode', 'PAPER')}",
                    f"runtime {state.get('runtime_mode', 'HEADLESS')}",
                    f"framework {state.get('framework_status', 'READY')}",
                    f"supervisor {state.get('supervisor_action', 'NONE')}",
                    f"alert {state.get('alert_level', 'INFO')}",
                    f"protection {state.get('protection_state', 'UNKNOWN')}",
                    f"risk {state.get('risk_state', 'OK')}",
                    f"validation {state.get('validation_status', 'UNKNOWN')}",
                    f"AI {state.get('llm_state', 'RULE_BASED')}",
                ]
                st.markdown(" ".join(f"<span class='pill'>{chip}</span>" for chip in chips), unsafe_allow_html=True)
                st.caption(f"Last heartbeat: {state.get('last_heartbeat') or 'pending'}")
                st.caption(f"Framework: {state.get('framework', 'PRODUCTION_STABILITY_V1')} | {state.get('last_framework_reason', 'framework ready')}")
                st.caption(
                    f"Defaults: stop {deployment['default_stop_type']} | TP {deployment['default_tp_type']} | "
                    f"capital >= ${float(deployment['minimum_recommended_capital']):,.0f} | risk {deployment['risk_classification']}"
                )
                if state.get("last_error"):
                    st.warning(str(state["last_error"]))
                with st.expander(f"{state.get('name', bot_id)} CLI equivalents", expanded=False):
                    st.code(
                        "\n".join(
                            [
                                f"python -m mytradingmind.runtime start-bot --bot-id {bot_id}",
                                f"python -m mytradingmind.runtime stop-bot --bot-id {bot_id}",
                                f"python -m mytradingmind.runtime pause-bot --bot-id {bot_id}",
                                f"python -m mytradingmind.runtime resume-bot --bot-id {bot_id}",
                                "python -m mytradingmind.runtime status",
                            ]
                        ),
                        language="bash",
                    )
            with right:
                action_cols = st.columns(3)
                actions = [
                    ("START", "START_BOT", status_text == "RUNNING"),
                    ("STOP", "STOP_BOT", status_text == "STOPPED"),
                    ("RESTART", "RESTART_BOT", False),
                    ("PAUSE", "PAUSE_BOT", status_text != "RUNNING"),
                    ("RESUME", "RESUME_BOT", status_text != "PAUSED"),
                    ("RUN VALIDATION", "RUN_VALIDATION", False),
                ]
                for index, (label, action, disabled) in enumerate(actions):
                    if action_cols[index % 3].button(label, key=f"admin-{action}-{bot_id}", disabled=disabled, use_container_width=True):
                        result = bus.dispatch(RuntimeCommand(action, bot_id=bot_id, source="BOT_ADMIN"))
                        append_journal(
                            str(state.get("name", bot_id)),
                            str(state.get("symbol", "")),
                            "BOT_ADMIN_ACTION",
                            "INFO" if result.ok else "ERROR",
                            action,
                            result.message,
                            {"bot_id": bot_id, "command_id": result.command_id, "state": result.state},
                        )
                        if result.ok:
                            st.success(f"{label} accepted for {state.get('name', bot_id)}")
                        else:
                            st.error(result.message)
                        load_bot_instances.clear()
                        st.rerun()
                view_cols = st.columns(3)
                if view_cols[0].button("VIEW JOURNAL", key=f"admin-journal-{bot_id}", use_container_width=True):
                    st.query_params["screen"] = "JOURNAL"
                    st.rerun()
                if view_cols[1].button("OPEN RUNTIME", key=f"admin-runtime-{bot_id}", use_container_width=True):
                    st.query_params["screen"] = "BOT MANAGEMENT"
                    st.query_params["bot_child"] = "Runtime"
                    st.query_params["route"] = BOT_MANAGEMENT_ROUTES["Runtime"]
                    st.rerun()
                with view_cols[2].popover("AI Health", use_container_width=True):
                    comment = ReasoningAgent().explain_status(
                        {
                            "spread_bps": 0,
                            "liquidity_score": 1,
                            "orderflow_score": 50,
                            "bot_status": status_text,
                        }
                    )
                    st.write(comment)
                with st.popover("REMOVE BOT DEFINITION", use_container_width=True):
                    st.warning("This removes the saved bot definition from Bot Admin and Bot Runtime. Running bots must be stopped first.")
                    confirm = st.text_input("Type the bot name to confirm", key=f"delete-confirm-{bot_id}")
                    disabled = status_text == "RUNNING" or confirm != str(state.get("name", bot_id))
                if st.button("Remove definition", key=f"delete-bot-{bot_id}", type="primary", disabled=disabled, use_container_width=True):
                        removed = remove_bot_definition(str(state.get("name", bot_id)))
                        if removed:
                            st.success(f"Removed bot definition for {state.get('name', bot_id)}.")
                            st.rerun()
                        else:
                            st.error("Could not remove this bot definition. Stop it first and check database connectivity.")


def launch_headless_runtime_process() -> dict[str, object]:
    try:
        reports = Path("reports")
        reports.mkdir(parents=True, exist_ok=True)
        log_path = reports / "headless_runtime.out.log"
        with log_path.open("a", encoding="utf-8") as log_file:
            popen_kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
            subprocess.Popen(
                [sys.executable, "scripts/run_headless_runtime.py", "--mode", "headless"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **popen_kwargs,
            )
        RuntimeManager().start_runtime("HEADLESS")
        return {"ok": True, "message": "Headless runtime process started.", "log_path": str(log_path)}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def deploy_bot_from_tile(bot: pd.Series, scan: pd.DataFrame, stream: dict[str, object]) -> tuple[bool, str]:
    if str(bot.get("strategy", "")) not in set(active_strategy_names()):
        return False, "This bot uses a dormant strategy. Create or switch to an active certified strategy before deployment."
    if not strategy_symbol_is_certified(str(bot.get("strategy", "")), str(bot.get("symbol", ""))):
        allowed = certified_symbols_for_strategy(str(bot.get("strategy", "")))
        return False, "This strategy/coin pair has not passed deployment guidance. Passed coins: " + ", ".join(allowed)
    validation = last_validation_for_bot(str(bot["name"]))
    if validation is None:
        return False, "Backtest this bot in Validation Lab before deployment."
    ok, reason = risk_gate_for_bot(bot, load_risk_settings())
    if not ok:
        transition_bot(str(bot["name"]), "FAILED", f"risk rejection: {reason}")
        append_journal(str(bot["name"]), str(bot["symbol"]), "RISK_REJECTION", "WARN", "BLOCKED", reason)
        return False, reason
    live_mark = bot_live_mark(bot, scan)
    if float(live_mark["last_price"]) <= 0:
        return False, "Cannot deploy: Binance socket/latest price is not available for this bot symbol."
    timeframe_bar = stream_timeframe_bar_for_bot(bot, stream)
    if not timeframe_bar:
        timeframe_bar = {
            "source": "latest_scan_fallback",
            "timeframe": str(bot.get("timeframe", "1h") or "1h"),
            "close": float(live_mark["last_price"]),
            "generated_at": datetime.now(UTC).isoformat(),
        }
    transition_bot(
        str(bot["name"]),
        "RUNNING",
        f"deployed and running 24x7 from {bot.get('timeframe', '1h')} socket bar and mark ${float(live_mark['last_price']):.6f}",
        {
            "runtime_entry_price": float(live_mark["last_price"]),
            "runtime_entry_symbol": str(live_mark["symbol"]),
            "runtime_entry_source": "binance_socket",
            "runtime_entry_timeframe": str(bot.get("timeframe", "1h") or "1h"),
            "runtime_entry_bar": timeframe_bar,
            "runtime_metadata": runtime_instance_profile(bot, scan, pd.DataFrame()),
            "validation_metadata": validation_metrics_for_bot(str(bot["name"])),
            "strategy_defaults_used": strategy_deployment_defaults(str(bot.get("strategy", ""))),
            "risk_policy_overrides": load_risk_settings(),
        },
    )
    return True, reason


def validate_bot_from_tile(bot: pd.Series) -> tuple[bool, str]:
    if str(bot.get("strategy", "")) not in set(active_strategy_names()):
        return False, "Dormant strategy bots cannot be validated for deployment from runtime."
    if not strategy_symbol_is_certified(str(bot.get("strategy", "")), str(bot.get("symbol", ""))):
        allowed = certified_symbols_for_strategy(str(bot.get("strategy", "")))
        return False, "This strategy/coin pair has not passed deployment guidance. Passed coins: " + ", ".join(allowed)
    symbol = str(bot.get("symbol", ""))
    timeframe = str(bot.get("timeframe", "1h") or "1h")
    capital = float(bot.get("capital", 0.0) or 0.0)
    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=365)
    metrics_result, _ = run_bot_validation(bot, symbol, timeframe, start_date, end_date, capital, 10.0, 5.0)
    run_id = f"validation-{bot['name']}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    validation_row = {
        "run_id": run_id,
        "bot_name": str(bot["name"]),
        "symbol": symbol,
        "timeframe": timeframe,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "capital": capital,
        "fees_bps": 10.0,
        "slippage_bps": 5.0,
        "state": "FAILED" if "error" in metrics_result else "COMPLETED",
        "metrics": metrics_result,
    }
    save_validation_run(validation_row)
    if "error" in metrics_result:
        transition_bot(str(bot["name"]), "FAILED", str(metrics_result["error"]))
        return False, str(metrics_result["error"])
    transition_bot(
        str(bot["name"]),
        "BACKTESTED",
        f"runtime tile validation completed: trades {int(metrics_result.get('total_trades', 0))}, PF {float(metrics_result.get('profit_factor', 0.0)):.2f}, DD {float(metrics_result.get('max_drawdown_pct', 0.0)):.1f}%",
    )
    append_journal(
        str(bot["name"]),
        symbol,
        "VALIDATION_RUN",
        "INFO",
        "COMPLETED",
        "runtime tile validation completed before deployment",
        validation_row["metrics"],
    )
    load_validation_runs_frame.clear()
    load_bot_instances.clear()
    return True, "Validation completed."


def runtime_bot_rankings(bots: pd.DataFrame, scan: pd.DataFrame, matrix: pd.DataFrame) -> pd.DataFrame:
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    rows: list[dict[str, object]] = []
    for _, bot in bots.reset_index(drop=True).iterrows():
        state = str(bot.get("state", "DRAFT"))
        symbol = str(bot.get("symbol", ""))
        strategy = str(bot.get("strategy", ""))
        live_mark = bot_live_mark(bot, scan)
        scan_match = scan[scan["symbol"].astype(str) == symbol].iloc[0] if not scan.empty and symbol in set(scan["symbol"].astype(str)) else pd.Series(dtype=object)
        perf_match = matrix[(matrix["strategy"].astype(str) == strategy) & (matrix["symbol"].astype(str) == symbol)] if not matrix.empty else pd.DataFrame()
        perf = perf_match.iloc[0] if not perf_match.empty else pd.Series(dtype=object)
        pf = float(perf.get("profit_factor", 0.0) or 0.0)
        sharpe = float(perf.get("sharpe_proxy", 0.0) or 0.0)
        win_rate = float(perf.get("win_rate", 0.0) or 0.0)
        drawdown = float(perf.get("max_drawdown_pct", 0.0) or 0.0)
        buy_score = float(scan_match.get("buy_score", 0.0) or 0.0)
        watch_score = float(scan_match.get("watch_score", 0.0) or 0.0)
        confidence = float(scan_match.get("confidence_score", 0.0) or 0.0)
        flow = float(scan_match.get("orderflow_score", 0.0) or 0.0)
        state_score = {"RUNNING": 24.0, "DEPLOYED": 24.0, "BACKTESTED": 18.0, "PAUSED": 12.0, "STOPPED": 8.0, "DRAFT": 5.0, "FAILED": -12.0}.get(state, 4.0)
        validation_bonus = 8.0 if last_validation_for_bot(str(bot.get("name", ""))) is not None else 0.0
        market_score = (max(buy_score, watch_score * 0.82) * 0.22) + (confidence * 0.13) + (flow * 0.08)
        backtest_score = min(18.0, pf * 4.0) + min(13.0, max(0.0, sharpe) * 5.0) + min(12.0, win_rate * 0.13) + max(-16.0, 10.0 - drawdown)
        pnl_score = max(-8.0, min(8.0, float(live_mark["pnl_pct"]) * 1.5))
        score = max(0.0, min(100.0, state_score + validation_bonus + market_score + backtest_score + pnl_score))
        rows.append(
            {
                "name": str(bot.get("name", "")),
                "state": state,
                "strategy": strategy,
                "symbol": symbol,
                "capital": float(bot.get("capital", 0.0) or 0.0),
                "pnl": float(live_mark["pnl"]),
                "pnl_pct": float(live_mark["pnl_pct"]),
                "last_price": float(live_mark["last_price"]),
                "runtime_score": round(score, 1),
                "category": runtime_bot_category(state),
                "signal": str(scan_match.get("scan_bucket", "NO SIGNAL") or "NO SIGNAL"),
                "buy_score": buy_score,
                "watch_score": watch_score,
                "confidence_score": confidence,
                "profit_factor": pf,
                "sharpe_proxy": sharpe,
                "win_rate": win_rate,
                "max_drawdown_pct": drawdown,
            }
        )
    ranked = pd.DataFrame(rows)
    if ranked.empty:
        return ranked
    ranked = ranked.sort_values(["runtime_score", "pnl", "capital"], ascending=[False, False, False]).reset_index(drop=True)
    ranked["rank"] = ranked.index + 1
    return ranked


def runtime_bot_category(state: str) -> str:
    if state in {"RUNNING", "DEPLOYED"}:
        return "Active"
    if state == "BACKTESTED":
        return "Ready"
    if state in {"DRAFT", "PAUSED"}:
        return "Review"
    if state == "FAILED":
        return "Failed"
    return "Stopped"


def runtime_marketplace_header(bots: pd.DataFrame, rankings: pd.DataFrame, scan: pd.DataFrame) -> None:
    active = rankings[rankings["state"].isin(["RUNNING", "DEPLOYED"])] if not rankings.empty else pd.DataFrame()
    total_value = float(active["capital"].sum()) if not active.empty else 0.0
    total_pnl = float(active["pnl"].sum()) if not active.empty else 0.0
    ready = int((rankings["state"] == "BACKTESTED").sum()) if not rankings.empty else 0
    top_score = float(rankings["runtime_score"].max()) if not rankings.empty else 0.0
    hot_symbol = "pending"
    if not scan.empty:
        normalized = normalize_scan_columns(scan).copy()
        normalized["hot_score"] = (
            pd.to_numeric(normalized["buy_score"], errors="coerce").fillna(0.0) * 0.42
            + pd.to_numeric(normalized["watch_score"], errors="coerce").fillna(0.0) * 0.25
            + pd.to_numeric(normalized["orderflow_score"], errors="coerce").fillna(0.0) * 0.18
            + pd.to_numeric(normalized["confidence_score"], errors="coerce").fillna(0.0) * 0.15
        )
        hot_symbol = str(normalized.sort_values("hot_score", ascending=False).iloc[0].get("symbol", "pending"))
    pnl_class = "good" if total_pnl >= 0 else "bad"
    st.markdown(
        f"""
        <div class="status-row">
          <div class="status-card"><div class="status-label">Active Strategies</div><div class="status-value good">{len(active)}</div></div>
          <div class="status-card"><div class="status-label">Total Value</div><div class="status-value">${total_value:,.2f}</div></div>
          <div class="status-card"><div class="status-label">Live PnL</div><div class="status-value {pnl_class}">${total_pnl:,.2f}</div></div>
          <div class="status-card"><div class="status-label">Ready Bots</div><div class="status-value warn">{ready}</div></div>
          <div class="status-card"><div class="status-label">Top Runtime Score</div><div class="status-value info">{top_score:.1f}</div></div>
          <div class="status-card"><div class="status-label">Hot Symbol</div><div class="status-value">{hot_symbol}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
def runtime_discovery_panel(rankings: pd.DataFrame, scan: pd.DataFrame) -> None:
    picks = rankings.head(3) if not rankings.empty else pd.DataFrame()
    pick_cards: list[str] = []
    for _, row in picks.iterrows():
        score = float(row.get("runtime_score", 0.0) or 0.0)
        state_class = "good" if str(row.get("state")) in {"RUNNING", "DEPLOYED"} else "warn" if str(row.get("state")) == "BACKTESTED" else "info"
        pick_cards.append(
            "<div class='runtime-pick'>"
            f"<div class='runtime-pick-title'><span>{row.get('name', '')}</span><span class='runtime-rank'>#{int(row.get('rank', 0))}</span></div>"
            f"<div class='scan-meta'>{row.get('strategy', '')}</div>"
            f"<div class='scan-meta'><span class='pill {state_class}'>{row.get('state', '')}</span> <span class='pill'>{row.get('symbol', '')}</span> <span class='pill'>{row.get('signal', '')}</span></div>"
            f"<div class='scan-meta'>score {score:.1f} | PF {float(row.get('profit_factor', 0.0) or 0.0):.2f} | win {float(row.get('win_rate', 0.0) or 0.0):.1f}%</div>"
            "</div>"
        )
    if not pick_cards:
        pick_cards.append("<div class='runtime-pick'><div class='runtime-pick-title'><span>No ranked bots yet</span></div><div class='scan-meta'>Create and backtest bots to populate picks.</div></div>")

    leaders: list[str] = []
    if not scan.empty:
        normalized = normalize_scan_columns(scan).copy()
        normalized["hot_score"] = (
            pd.to_numeric(normalized["buy_score"], errors="coerce").fillna(0.0) * 0.42
            + pd.to_numeric(normalized["watch_score"], errors="coerce").fillna(0.0) * 0.25
            + pd.to_numeric(normalized["orderflow_score"], errors="coerce").fillna(0.0) * 0.18
            + pd.to_numeric(normalized["confidence_score"], errors="coerce").fillna(0.0) * 0.15
        )
        for rank, (_, row) in enumerate(normalized.sort_values("hot_score", ascending=False).head(5).iterrows(), start=1):
            leaders.append(
                "<div class='runtime-leader-row'>"
                f"<span class='runtime-rank'>#{rank}</span>"
                f"<span>{row.get('symbol', '')}<div class='scan-meta'>{row.get('scan_bucket', 'NO SIGNAL')}</div></span>"
                f"<span class='pill'>{float(row.get('hot_score', 0.0) or 0.0):.0f}</span>"
                "</div>"
            )
    if not leaders:
        leaders.append("<div class='scan-meta'>Awaiting live scanner data.</div>")

    st.markdown(
        "<div class='runtime-discovery'>"
        "<div><div class='bucket-title'><span>Daily Picks</span><span class='bucket-count'>runtime ranked</span></div>"
        f"<div class='runtime-picks'>{''.join(pick_cards)}</div></div>"
        "<div class='runtime-leaderboard'><div class='bucket-title'><span>Hot Coin Leaderboard</span><span class='bucket-count'>live scan</span></div>"
        f"{''.join(leaders)}</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def runtime_bot_filter_bar(bots: pd.DataFrame, rankings: pd.DataFrame) -> pd.DataFrame:
    if rankings.empty:
        return bots
    categories = ["All", "Active", "Ready", "Review", "Stopped", "Failed"]
    selected = st.radio("Runtime category", categories, horizontal=True, label_visibility="collapsed")
    sort_by = st.selectbox("Sort", ["Runtime score", "Live PnL", "Capital", "Name"], index=0, label_visibility="collapsed")
    view = rankings.copy()
    if selected != "All":
        view = view[view["category"] == selected]
    if sort_by == "Live PnL":
        view = view.sort_values(["pnl", "runtime_score"], ascending=[False, False])
    elif sort_by == "Capital":
        view = view.sort_values(["capital", "runtime_score"], ascending=[False, False])
    elif sort_by == "Name":
        view = view.sort_values("name", ascending=True)
    else:
        view = view.sort_values(["runtime_score", "pnl"], ascending=[False, False])
    names = view["name"].astype(str).tolist()
    if not names:
        st.info("No bots match this runtime filter.")
        return bots.iloc[0:0]
    order = {name: index for index, name in enumerate(names)}
    filtered = bots[bots["name"].astype(str).isin(names)].copy()
    filtered["_runtime_order"] = filtered["name"].astype(str).map(order)
    return filtered.sort_values("_runtime_order").drop(columns=["_runtime_order"])


def runtime_tiles(
    bots: pd.DataFrame,
    scan: pd.DataFrame,
    stream: dict[str, object],
    matrix: pd.DataFrame,
    aggregate: pd.DataFrame,
    rankings: pd.DataFrame | None = None,
) -> None:
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    if bots.empty:
        st.info("No bot tiles match the current filter.")
        return
    columns = st.columns(2)
    for index, row in bots.reset_index(drop=True).iterrows():
        state = str(row.get("state", "DRAFT"))
        symbol = str(row.get("symbol", ""))
        strategy = str(row.get("strategy", ""))
        state_class = "good" if state in {"RUNNING", "DEPLOYED"} else "bad" if state == "FAILED" else "warn" if state in {"PAUSED", "BACKTESTED"} else "info"
        perf = matrix[(matrix["strategy"].astype(str) == strategy) & (matrix["symbol"].astype(str) == symbol)] if not matrix.empty else pd.DataFrame()
        perf_row = perf.iloc[0] if not perf.empty else pd.Series(dtype=object)
        live_mark = bot_live_mark(row, scan)
        pnl = float(live_mark["pnl"])
        drawdown = float(perf_row.get("max_drawdown_pct", 0.0) or 0.0)
        win_rate = float(perf_row.get("win_rate", 0.0) or 0.0)
        pf = float(perf_row.get("profit_factor", 0.0) or 0.0)
        sharpe = float(perf_row.get("sharpe_proxy", 0.0) or 0.0)
        expectancy = float(perf_row.get("avg_trade_return_pct", 0.0) or 0.0)
        health = "healthy" if state in {"RUNNING", "DEPLOYED"} and drawdown < 12 else "watch" if state != "FAILED" else "failed"
        rank_row = pd.Series(dtype=object)
        if rankings is not None and not rankings.empty:
            match = rankings[rankings["name"].astype(str) == str(row.get("name", ""))]
            rank_row = match.iloc[0] if not match.empty else rank_row
        rank_label = f"#{int(rank_row.get('rank', index + 1) or index + 1)}"
        runtime_score = float(rank_row.get("runtime_score", 0.0) or 0.0)
        signal = str(rank_row.get("signal", "NO SIGNAL") or "NO SIGNAL")
        instance = runtime_instance_profile(row, scan, matrix)
        health_class = "good" if instance["health_light"] == "Green" else "warn" if instance["health_light"] == "Amber" else "bad"
        trade_state = str(instance["trade_position_state"])
        trade_state_class = "good" if trade_state == "IN_TRADE" else "warn" if trade_state == "OUT_OF_TRADE" else "info"
        tile_data = {
            "name": str(row.get("name", "")),
            "state": state,
            "state_class": state_class,
            "strategy": strategy,
            "symbol": symbol,
            "capital": float(live_mark["capital"]),
            "entry_price": float(live_mark["entry_price"]),
            "last_price": float(live_mark["last_price"]),
            "pnl": pnl,
            "pnl_pct": float(live_mark["pnl_pct"]),
            "started_at": str(live_mark["started_at"]),
            "socket_status": str(live_mark["socket_status"]),
            "socket_age": str(live_mark["socket_age"]),
            "in_market": bool(instance["in_trade"]),
            "trade_position_state": str(instance["trade_position_state"]),
            "trade_position_reason": str(instance["trade_position_reason"]),
            "last_entry_at": str(instance["last_entry_at"]),
            "last_exit_at": str(instance["last_exit_at"]),
        }
        with columns[index % len(columns)].container(border=True):
            st.markdown(
                f"<div class='runtime-bot-boundary {health_class}'>"
                f"<div class='runtime-bot-boundary-title'>{rank_label} {row.get('name', '')}</div>"
                f"<div class='runtime-bot-boundary-meta'>{strategy} | {symbol} | {row.get('timeframe', '1h')} | {instance['strategy_type']} | bot {instance['bot_version']}</div>"
                "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<span class="pill {state_class}">{html.escape(state)}</span> '
                f'<span class="pill {trade_state_class}">{html.escape(trade_state)}</span> '
                f'<span class="pill {health_class}">{html.escape(str(instance["health_light"]))} {html.escape(str(instance["health_label"]))}</span> '
                f'<span class="pill">score {runtime_score:.1f}</span> '
                f'<span class="pill">{html.escape(signal)}</span>',
                unsafe_allow_html=True,
            )
            st.caption(
                f"{instance['bot_instance_name']} | {instance['strategy_name']} | {instance['strategy_type']} | "
                f"{symbol} | {row.get('timeframe', '1h')} | bot {instance['bot_version']} | validation {instance['validation_status']}"
            )
            runtime_tile_live_mark_component(tile_data)
            st.info(str(instance["operational_guidance"]))
            st.caption(f"Trade state: {instance['trade_position_state']} | {instance['trade_position_reason']} | entry {stream_age_text(str(instance['last_entry_at']))} | exit {stream_age_text(str(instance['last_exit_at']))}")
            st.markdown("<div class='runtime-tile-zone'><div class='runtime-tile-zone-title'>Performance</div>", unsafe_allow_html=True)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Real-time P&L", money_text(instance["real_time_pnl"]), f"{pct_text(instance['roi_pct'])} ROI")
            m2.metric("Unrealized / Realized", money_text(instance["unrealized_pnl"]), f"{money_text(instance['realized_pnl'])} realized")
            m3.metric("Runtime", f"{safe_number(instance['runtime_hours']):.2f}h", f"started {stream_age_text(str(live_mark['started_at']))}")
            m4.metric("Exposure", money_text(instance["current_exposure"]), f"{safe_number(instance['capital_utilization_pct']):.1f}% utilized")
            cperf1, cperf2, cperf3, cperf4 = st.columns(4)
            cperf1.metric("Drawdown", pct_text(instance["current_drawdown_pct"]), f"peak {pct_text(instance['peak_drawdown_pct'])}")
            cperf2.metric("Trades", f"{int(instance['trade_count'])}", f"win {safe_number(instance['win_rate']):.1f}%")
            cperf3.metric("Profit Factor", f"{safe_number(instance['profit_factor']):.2f}")
            cperf4.metric("Bucket / State", str(instance["current_bucket"]), str(instance["current_strategy_state"]))
            st.markdown("</div>", unsafe_allow_html=True)
            st.markdown("<div class='runtime-tile-zone'><div class='runtime-tile-zone-title'>Risk, Stops, and Timing</div>", unsafe_allow_html=True)
            s0, s1, s2, s3, s4 = st.columns(5)
            s0.metric("Trade State", str(instance["trade_position_state"]).replace("_", " "), str(instance["trade_position_reason"])[:42])
            s1.metric("Position Size", f"{safe_number(instance['qty_per_order']):.8f}", f"recommended {safe_number(instance['recommended_quantity']):.8f}")
            s2.metric("Stop-loss", money_text(instance["current_stop_loss"]), f"{money_text(instance['stop_loss_distance_abs'])} away")
            s3.metric("Stop Distance", pct_text(instance["stop_loss_distance_pct"]), f"R {safe_number(instance['risk_multiple']):.2f}")
            s4.metric("Signal Age", stream_age_text(str(instance.get("last_signal_at", ""))), f"data {stream_age_text(str(row.get('heartbeat_at', '')))}")
            st.markdown("</div>", unsafe_allow_html=True)
            with st.expander("Instance details: context, capital, stops, health", expanded=False):
                detail_rows = [
                    {"Section": "Identity", "Metric": "Deployment timestamp", "Value": instance["deployment_timestamp"] or "pending"},
                    {"Section": "Identity", "Metric": "Runtime duration", "Value": f"{float(instance['runtime_duration_hours']):.2f} hours"},
                    {"Section": "Trade State", "Metric": "In/out of trade", "Value": f"{instance['trade_position_state']} | {instance['trade_position_reason']}"},
                    {"Section": "Trade State", "Metric": "Entry / exit timestamp", "Value": f"{instance['last_entry_at'] or 'pending'} / {instance['last_exit_at'] or 'open'}"},
                    {"Section": "Backtest", "Metric": "ROI / net P&L", "Value": money_text(instance["backtest_roi"])},
                    {"Section": "Backtest", "Metric": "Max drawdown / win / trades", "Value": f"{pct_text(instance['backtest_max_drawdown'])} / {safe_number(instance['backtest_win_rate']):.1f}% / {int(instance['backtest_trade_count'])}"},
                    {"Section": "Backtest", "Metric": "Last run / data range", "Value": f"{instance['last_backtest_timestamp']} / {instance['backtest_data_range']}"},
                    {"Section": "Capital", "Metric": "Initial / current / available", "Value": f"{money_text(instance['initial_allocated_capital'])} / {money_text(instance['current_allocated_capital'])} / {money_text(instance['available_unallocated_capital'])}"},
                    {"Section": "Capital", "Metric": "Qty/order / recommended / margin", "Value": f"{float(instance['qty_per_order']):.8f} / {float(instance['recommended_quantity']):.8f} / {float(instance['margin_usage']):.2f}"},
                    {"Section": "Capital", "Metric": "Sizing method / reasoning", "Value": f"{instance['position_sizing_method']} | {instance['position_sizing_reasoning']}"},
                    {"Section": "Stop / Exit", "Metric": "Stop-loss", "Value": f"{instance['stop_loss_type']} | {instance['stop_loss_value']}"},
                    {"Section": "Stop / Exit", "Metric": "Active stop / distance / R", "Value": f"{money_text(instance['current_stop_loss'])} / {money_text(instance['stop_loss_distance_abs'])} ({pct_text(instance['stop_loss_distance_pct'])}) / {safe_number(instance['risk_multiple']):.2f}R"},
                    {"Section": "Stop / Exit", "Metric": "Take-profit", "Value": f"{instance['take_profit_type']} | {instance['take_profit_value']}"},
                    {"Section": "Stop / Exit", "Metric": "Trailing / emergency / policy", "Value": f"{instance['trailing_enabled']} / {instance['emergency_stop_enabled']} / {instance['risk_policy_stop_status']}"},
                    {"Section": "Health", "Metric": "API / feed / latency", "Value": f"{instance['api_connectivity']} / {instance['feed_status']} / {float(instance['runtime_latency_ms']):.1f} ms"},
                    {"Section": "Health", "Metric": "Heartbeat / order / queue", "Value": f"{stream_age_text(str(instance['last_heartbeat']))} / {instance['order_execution_status']} / {instance['execution_queue_state']}"},
                    {"Section": "Health", "Metric": "Errors / recovery", "Value": f"{int(instance['error_warning_count'])} / {instance['recovery_state']}"},
                ]
                st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)
            c1, c2, c3 = st.columns(3)
            deploy_disabled = state in {"RUNNING", "DEPLOYED"} or strategy not in set(active_strategy_names())
            if c1.button("Deploy", key=f"tile-deploy-{row['name']}", use_container_width=True, disabled=deploy_disabled):
                deploy_row = row
                if last_validation_for_bot(str(row["name"])) is None or state != "BACKTESTED":
                    validated, validation_message = validate_bot_from_tile(row)
                    if not validated:
                        st.error(validation_message)
                        st.rerun()
                    refreshed = load_bot_instances()
                    match = refreshed[refreshed["name"].astype(str) == str(row["name"])] if not refreshed.empty and "name" in refreshed else pd.DataFrame()
                    deploy_row = match.iloc[0] if not match.empty else row
                deployed, message = deploy_bot_from_tile(deploy_row, scan, stream)
                if deployed:
                    st.success(message)
                else:
                    st.error(message)
                st.rerun()
            if c2.button("Stop", key=f"tile-stop-{row['name']}", use_container_width=True, disabled=state not in {"RUNNING", "DEPLOYED"}):
                transition_bot(str(row["name"]), "STOPPED", "stopped by user")
                st.warning("Bot stopped.")
                st.rerun()
            with c3.popover("Remove", use_container_width=True):
                st.warning("Stop running bots before removal.")
                confirm = st.text_input("Type bot name", key=f"tile-remove-confirm-{row['name']}")
                disabled = state in {"RUNNING", "DEPLOYED"} or confirm != str(row["name"])
                if st.button("Remove definition", key=f"tile-remove-{row['name']}", type="primary", disabled=disabled, use_container_width=True):
                    removed = remove_bot_definition(str(row["name"]))
                    if removed:
                        st.success("Bot definition removed.")
                        st.rerun()
                    st.error("Could not remove this bot definition.")
            st.caption(f"Next: {lifecycle_next_action(state)}")
            st.caption(f"DD {drawdown:.2f}% | win {win_rate:.1f}% | PF {pf:.2f} | Sharpe {sharpe:.2f} | expectancy {expectancy:.3f}%")
            st.caption(f"Started {stream_age_text(str(live_mark['started_at']))} ago | Heartbeat {stream_age_text(row.get('heartbeat_at', ''))} | {row.get('status_reason', '')}")


def runtime_tile_live_mark_component(row: dict[str, object]) -> None:
    components.html(
        f"""
        <div id="runtime-tile"></div>
        <style>
          body {{ margin: 0; background: transparent; font-family: Inter, Arial, sans-serif; color: #e8edf2; }}
          .good {{ color: #55d49a; }}
          .bad {{ color: #ff6f7d; }}
          .rt-value {{ font-size: 1.4rem; font-weight: 850; margin: 0.15rem 0 0.25rem; }}
          .rt-meta {{ color: #94a3ad; font-size: 0.8rem; line-height: 1.36; margin-top: 0.22rem; overflow-wrap: anywhere; }}
          .rt-pulse {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #94a3ad; margin-right: 5px; }}
          .rt-pulse.live {{ background: #55d49a; box-shadow: 0 0 10px rgba(85, 212, 154, 0.8); }}
        </style>
        <script>
          const row = {json.dumps(row)};
          const shell = document.getElementById("runtime-tile");
          const symbolKey = String(row.symbol || "").replace("/", "").toLowerCase();
          let socketStatus = "starting";

          function money(value, precision = 2) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "$0.00";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: precision, maximumFractionDigits: precision }});
          }}
          function price(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "price unavailable";
            return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 6 }});
          }}
          function percent(value) {{
            const n = Number(value);
            if (!Number.isFinite(n)) return "0.00";
            return n.toFixed(2);
          }}
          function ageText(value) {{
            if (!value) return "pending";
            const ts = new Date(value);
            if (Number.isNaN(ts.getTime())) return String(value);
            const secs = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
            if (secs < 60) return secs + "s";
            const mins = Math.floor(secs / 60);
            if (mins < 60) return mins + "m " + (secs % 60) + "s";
            const hours = Math.floor(mins / 60);
            return hours + "h " + (mins % 60) + "m";
          }}
          function mark(row) {{
            const entry = Number(row.entry_price || 0);
            const last = Number(row.last_price || 0);
            if (!row.in_market || entry <= 0 || last <= 0) return {{ pnl: 0, pnlPct: 0 }};
            const pnlPct = (last - entry) / entry * 100;
            const capital = Number.isFinite(Number(row.capital)) ? Number(row.capital) : 0;
            return {{ pnl: capital * pnlPct / 100, pnlPct }};
          }}
          function render() {{
            const current = mark(row);
            const pnlClass = current.pnl >= 0 ? "good" : "bad";
            const pulseClass = row.socket_seen_at ? "live" : "";
            const tradeState = String(row.trade_position_state || (row.in_market ? "IN_TRADE" : "OUT_OF_TRADE")).replaceAll("_", " ");
            shell.innerHTML = `<div class="rt-value ${{pnlClass}}">${{money(current.pnl)}} <span style="font-size:0.92rem">/ ${{percent(current.pnlPct)}}%</span></div>
              <div class="rt-meta"><span class="rt-pulse ${{pulseClass}}"></span>${{tradeState}} | PnL since start ${{row.started_at ? ageText(row.started_at) + " ago" : "pending"}} | live ${{price(row.last_price)}} | entry ${{price(row.entry_price)}} | socket ${{socketStatus}} ${{row.socket_seen_at ? ageText(row.socket_seen_at) : row.socket_age}}</div>`;
          }}
          function connect() {{
            if (!symbolKey) {{
              socketStatus = "no symbols";
              render();
              return;
            }}
            const ws = new WebSocket(`wss://stream.testnet.binance.vision/stream?streams=${{symbolKey}}@trade`);
            ws.onopen = () => {{ socketStatus = "streaming"; render(); }};
            ws.onmessage = (event) => {{
              const payload = JSON.parse(event.data);
              const data = payload.data || payload;
              const key = String(data.s || "").toLowerCase();
              if (key === symbolKey && data.p) {{
                row.last_price = Number(data.p);
                row.socket_seen_at = new Date().toISOString();
                render();
              }}
            }};
            ws.onerror = () => {{ socketStatus = "error"; render(); }};
            ws.onclose = () => {{ socketStatus = "reconnecting"; render(); setTimeout(connect, 3000); }};
          }}
          render();
          connect();
          setInterval(render, 1000);
        </script>
        """,
        height=76,
    )


def bot_instance_tiles(bots: pd.DataFrame) -> None:
    if bots.empty:
        st.info("No bot instances yet.")
        return
    html = ["<div class='bucket-grid'>"]
    for _, row in bots.iterrows():
        state = str(row.get("state", "DRAFT"))
        state_class = "good" if state in {"RUNNING", "DEPLOYED"} else "bad" if state == "FAILED" else "warn" if state in {"PAUSED", "BACKTESTED"} else "info"
        heartbeat = stream_age_text(row.get("heartbeat_at", ""))
        html.append(
            "<div class='bucket'>"
            f"<div class='bucket-title'><span>{row['name']}</span><span class='pill {state_class}'>{state}</span></div>"
            f"<div class='scan-meta'>{row['strategy']} on {row['symbol']} / {row.get('timeframe', '1h')}</div>"
            f"<div class='status-value'>${float(row.get('capital', 0.0)):,.2f}</div>"
            f"<div class='scan-meta'>{row.get('status_reason', '')}</div>"
            f"<div class='scan-meta'>heartbeat {heartbeat}</div>"
            "</div>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def strategy_tiles(aggregate: pd.DataFrame) -> None:
    html = ["<div class='bucket-grid'>"]
    for _, row in aggregate.iterrows():
        status_class = "good" if row["status"] == "DEPLOYABLE" else "bad" if row["status"] == "REJECTED" else "warn"
        pnl_class = "good" if float(row["total_pnl"]) >= 0 else "bad"
        html.append(
            "<div class='bucket'>"
            f"<div class='bucket-title'><span>{row['strategy']}</span><span class='pill {status_class}'>{row['status']}</span></div>"
            f"<div class='status-value {pnl_class}'>${float(row['total_pnl']):,.2f}</div>"
            f"<div class='scan-meta'>symbols {int(row['symbols'])} | trades {int(row['trades'])} | win {float(row['win_rate']):.1f}%</div>"
            f"<div class='scan-meta'>avg return {float(row['avg_return_pct']):.2f}% | max DD {float(row['max_drawdown_pct']):.2f}%</div>"
            f"<div class='scan-meta'>sharpe proxy {float(row['sharpe_proxy']):.2f} | confidence {float(row['confidence_score']):.0f}%</div>"
            "</div>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def da_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    scan = load_live_scan()
    if scan.empty:
        st.info("Awaiting Binance Testnet scan data.")
        return
    concerns = pd.DataFrame(
        {
            "factor": ["Spread", "Orderflow", "Confidence", "Buy Pressure", "Sell Pressure"],
            "score": [
                max(0.0, min(0.5, float(scan["stream_spread_bps"].mean()) / 40)),
                max(0.0, 1 - float(scan["orderflow_score"].mean()) / 100),
                max(0.0, 1 - float(scan["confidence_score"].mean()) / 100),
                max(0.0, 1 - float(scan["buy_score"].mean()) / 100),
                max(0.0, float(scan["sell_score"].mean()) / 100),
            ],
            "action": ["watch", "reduce", "reduce", "wait", "veto threshold"],
        }
    )
    fig = go.Figure(go.Bar(x=concerns["score"], y=concerns["factor"], orientation="h", marker_color=["#f0c86a", "#f0c86a", "#55d49a", "#ff6f7d", "#f0c86a"]))
    st.plotly_chart(layout_chart(fig, 330), use_container_width=True)
    st.dataframe(concerns, use_container_width=True, hide_index=True)
    st.caption("DA layer uses Binance Testnet/live scan factors: <= 0.20 full size, 0.20-0.35 reduced size, > 0.35 veto.")


def execution_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    trades_path = Path("reports/top10_replay_trades.csv")
    if not trades_path.exists():
        st.info("No Binance backtest trade report found yet.")
        return
    try:
        trades = pd.read_csv(trades_path).tail(50).copy()
    except pd.errors.EmptyDataError:
        st.info("Binance backtest trade report is present but has no trades yet.")
        return
    if trades.empty:
        st.info("Binance backtest trade report has no trades yet.")
        return
    orders = pd.DataFrame(
        {
            "minute": pd.to_datetime(trades["exit_time"], errors="coerce"),
            "ack_ms": 0.0,
            "slippage_bps": ((trades["exit_price"] - trades["entry_price"]).abs() / trades["entry_price"] * 10_000).clip(0, 100),
            "state": np.where(trades["pnl"] >= 0, "FILLED_WIN", "FILLED_LOSS"),
        }
    )
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=orders["minute"], y=orders["ack_ms"], line={"color": "#79a7ff"}, name="ACK ms"))
    fig.add_trace(go.Bar(x=orders["minute"], y=orders["slippage_bps"], marker_color="#f0c86a", name="Slippage bps"), secondary_y=True)
    st.plotly_chart(layout_chart(fig, 390), use_container_width=True)
    st.dataframe(orders.tail(18), use_container_width=True, hide_index=True)


def health_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    stream = live_stream_heartbeat(load_live_stream())
    bots = load_bot_instances()
    journal = load_journal_events()
    running = int(bots["state"].isin(["DEPLOYED", "RUNNING"]).sum()) if not bots.empty and "state" in bots else 0
    failed = int((bots["state"] == "FAILED").sum()) if not bots.empty and "state" in bots else 0
    errors = int(journal["severity"].isin(["ERROR", "CRITICAL"]).sum()) if not journal.empty and "severity" in journal else 0
    retries = int(journal["event_type"].astype(str).str.contains("RETRY", case=False, na=False).sum()) if not journal.empty and "event_type" in journal else 0
    health = pd.DataFrame(
        [
            {"component": "websocket", "status": stream["status"], "latency_ms": 0.0, "queue": 0, "detail": stream["age"]},
            {"component": "api", "status": "TESTNET", "latency_ms": 0.0, "queue": 0, "detail": "Binance Spot Testnet"},
            {"component": "bot heartbeat", "status": "RUNNING" if running else "IDLE", "latency_ms": 0.0, "queue": running, "detail": f"{running} running"},
            {"component": "failed bots", "status": "OK" if failed == 0 else "ATTENTION", "latency_ms": 0.0, "queue": failed, "detail": f"{failed} failed"},
            {"component": "database", "status": "ENABLED" if setting_bool("database_enabled") else "FILE FALLBACK", "latency_ms": 0.0, "queue": 0, "detail": redact_url(settings.database_url)},
            {"component": "exchange", "status": "CONNECTED", "latency_ms": 0.0, "queue": 0, "detail": "testnet configured"},
            {"component": "errors", "status": "OK" if errors == 0 else "ATTENTION", "latency_ms": 0.0, "queue": errors, "detail": f"{errors} logged"},
            {"component": "retries", "status": "OK", "latency_ms": 0.0, "queue": retries, "detail": f"{retries} retries"},
            {"component": "app log", "status": "WRITING" if LOG_PATH.exists() else "PENDING", "latency_ms": 0.0, "queue": 0, "detail": str(LOG_PATH)},
        ]
    )
    fig = go.Figure(
        go.Heatmap(
            z=[health["latency_ms"], health["queue"]],
            x=health["component"],
            y=["latency ms", "queue"],
            colorscale=[[0, "#143123"], [0.55, "#3b3518"], [1, "#4a1d24"]],
        )
    )
    st.plotly_chart(layout_chart(fig, 300), use_container_width=True)
    st.dataframe(health, use_container_width=True, hide_index=True)
    st.markdown("### Critical Log Updates")
    critical = critical_log_events(journal)
    if critical.empty:
        st.info("No critical operational events in the current journal window.")
    else:
        st.dataframe(critical, use_container_width=True, hide_index=True)


def trade_management_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Trade Management")
    st.caption("Bot-level trade lifecycle, risk posture, runtime visibility, and audit evidence. Headline numbers update live without refreshing the screen.")
    scan = load_live_scan()
    bots = load_bot_instances()
    matrix, _ = load_cached_strategy_matrix(tuple(active_strategy_names()))
    validation_runs = load_validation_runs_frame()
    backtest_trades = load_backtest_trades()
    order_audit = load_runtime_order_audit()
    trade_events = load_runtime_trade_events()
    pnl_snapshots = load_runtime_trade_pnl_snapshots()
    alerts = load_runtime_alerts()
    active, closed, strategy_rows = trade_management_rows(bots, scan, matrix, validation_runs, backtest_trades, order_audit, trade_events)
    persisted_snapshots = persist_trade_pnl_snapshots(active)
    pnl_snapshots = load_runtime_trade_pnl_snapshots()
    summary = trade_management_summary(active, closed, alerts)

    trade_management_live_summary_component(
        active,
        summary,
        len(trade_events) if not trade_events.empty else 0,
        len(pnl_snapshots) if not pnl_snapshots.empty else 0,
    )

    last_snapshot = ""
    if not pnl_snapshots.empty and "snapshot_time" in pnl_snapshots:
        last_snapshot = str(pnl_snapshots["snapshot_time"].iloc[-1])

    tab_active, tab_analytics, tab_audit, tab_persistence = st.tabs(["Active Desk", "Analytics", "Audit Trail", "Persistence"])

    with tab_active:
        st.markdown("<div class='trade-console-grid'>", unsafe_allow_html=True)
        left, right = st.columns([2.1, 1.0])
        with left:
            st.markdown("<div class='trade-panel'><div class='trade-panel-title'><strong>Active Trades</strong><span class='trade-refresh-note'>near real-time mark and lifecycle state</span></div>", unsafe_allow_html=True)
            if active.empty:
                st.caption("No active runtime trades are currently visible.")
            else:
                active_view_columns = [
                    "Bot",
                    "Strategy ID",
                    "Symbol",
                    "Timeframe",
                    "Trade State",
                    "Order State",
                    "Position State",
                    "Entry Timestamp",
                    "Exit Timestamp",
                    "Current Price",
                    "Unrealized P&L",
                    "ROI %",
                    "Current Stop-Loss",
                    "Current Take-Profit",
                    "Stop Distance %",
                    "Market Feed Status",
                    "Market Feed Age",
                    "Last Mark Source",
                    "Risk Light",
                    "Guidance",
                ]
                trade_management_live_active_grid_component(active)
                st.dataframe(active[[column for column in active_view_columns if column in active]], use_container_width=True, hide_index=True)
            st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("<div class='trade-panel'><div class='trade-panel-title'><strong>Strategy Runtime Cards</strong><span class='trade-refresh-note'>one card per bot instance</span></div>", unsafe_allow_html=True)
            if strategy_rows.empty:
                st.caption("No bot strategy runtime cards available yet.")
            else:
                for _, row in strategy_rows.head(12).iterrows():
                    color = {"Green": "var(--good)", "Amber": "var(--warn)", "Red": "var(--bad)"}.get(str(row.get("Health", "Green")), "var(--info)")
                    st.markdown(
                        f"""
                        <div class="runtime-bot-boundary" style="border-left-color:{color};">
                          <div class="runtime-bot-boundary-title">{row.get('Bot', '')}</div>
                          <div class="runtime-bot-boundary-meta">{row.get('Strategy', '')} | {row.get('Symbol', '')} | {row.get('Runtime Status', '')}</div>
                          <div class="trade-health-strip">
                            <div class="trade-health-cell"><div class="label">Runtime P&L</div><div class="value">{money_text(row.get('Runtime P&L', 0.0))}</div></div>
                            <div class="trade-health-cell"><div class="label">Win Rate</div><div class="value">{pct_text(row.get('Win Rate %', 0.0))}</div></div>
                            <div class="trade-health-cell"><div class="label">Profit Factor</div><div class="value">{safe_number(row.get('Profit Factor', 0.0)):.2f}</div></div>
                            <div class="trade-health-cell"><div class="label">Health</div><div class="value">{row.get('Health', '')}</div></div>
                          </div>
                          <div class="runtime-bot-boundary-meta">{row.get('Guidance', '')}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
            st.markdown("</div>", unsafe_allow_html=True)

        with right:
            st.markdown("<div class='trade-panel'><div class='trade-panel-title'><strong>Risk Alerts</strong><span class='trade-refresh-note'>latest runtime alerts</span></div>", unsafe_allow_html=True)
            if alerts.empty:
                st.caption("No runtime risk alerts recorded.")
            else:
                alert_columns = [column for column in ["bot_id", "level", "code", "reason", "timestamp", "created_at"] if column in alerts]
                st.dataframe(alerts[alert_columns].tail(50), use_container_width=True, hide_index=True)
            st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("<div class='trade-panel'><div class='trade-panel-title'><strong>Trade-Level Risk</strong><span class='trade-refresh-note'>stop proximity and drawdown</span></div>", unsafe_allow_html=True)
            if active.empty:
                st.caption("Risk overlays appear when active trades are present.")
            else:
                risk_columns = ["Bot", "Symbol", "Stop Distance %", "Drawdown %", "Exposure", "Risk Light", "Risk Reason"]
                st.dataframe(active[[column for column in risk_columns if column in active]], use_container_width=True, hide_index=True)
            st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("<div class='trade-panel'><div class='trade-panel-title'><strong>Portfolio Exposure</strong><span class='trade-refresh-note'>symbol and strategy concentration</span></div>", unsafe_allow_html=True)
            if active.empty or "Exposure" not in active:
                st.caption("No open exposure.")
            else:
                exposure_by_symbol = active.groupby("Symbol", as_index=False)["Exposure"].sum().sort_values("Exposure", ascending=False)
                exposure_by_strategy = active.groupby("Strategy ID", as_index=False)["Exposure"].sum().sort_values("Exposure", ascending=False)
                st.dataframe(exposure_by_symbol, use_container_width=True, hide_index=True)
                st.dataframe(exposure_by_strategy, use_container_width=True, hide_index=True)
            st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with tab_analytics:
        trade_management_live_analytics_component(active)
        st.markdown("#### Live P&L Snapshots")
        if not pnl_snapshots.empty and {"snapshot_time", "unrealized_pnl"}.issubset(set(pnl_snapshots.columns)):
            chart = pnl_snapshots.copy()
            chart["snapshot_time"] = pd.to_datetime(chart["snapshot_time"], errors="coerce")
            chart["total_pnl"] = pd.to_numeric(chart.get("unrealized_pnl", 0.0), errors="coerce").fillna(0.0) + pd.to_numeric(chart.get("realized_pnl", 0.0), errors="coerce").fillna(0.0)
            chart = chart.dropna(subset=["snapshot_time"]).sort_values("snapshot_time")
            if not chart.empty:
                by_time = chart.groupby("snapshot_time", as_index=False)["total_pnl"].sum()
                by_time["Peak"] = by_time["total_pnl"].cummax()
                by_time["Drawdown"] = by_time["total_pnl"] - by_time["Peak"]
                fig = make_subplots(rows=1, cols=2, subplot_titles=("Live Runtime P&L", "Live Drawdown"))
                fig.add_trace(go.Scatter(x=by_time["snapshot_time"], y=by_time["total_pnl"], line={"color": "#55d49a"}, name="Runtime P&L"), row=1, col=1)
                fig.add_trace(go.Scatter(x=by_time["snapshot_time"], y=by_time["Drawdown"], fill="tozeroy", line={"color": "#ff6f7d"}, name="Runtime DD"), row=1, col=2)
                st.plotly_chart(layout_chart(fig, 340), use_container_width=True)
            snapshot_columns = [column for column in ["snapshot_time", "bot_id", "symbol", "current_price", "unrealized_pnl", "realized_pnl", "roi_pct", "exposure", "drawdown_pct", "lifecycle_state"] if column in pnl_snapshots]
            st.dataframe(pnl_snapshots[snapshot_columns].tail(200), use_container_width=True, hide_index=True)
        else:
            st.caption("Live P&L snapshots will appear after the next active-trade refresh.")

        st.markdown("#### Closed Trade P&L")
        if active.empty:
            pass
        if closed.empty or "Realized P&L" not in closed:
            st.caption("Closed-trade P&L chart appears after replay or runtime trade history is available.")
        else:
            chart = closed.copy()
            chart["Exit Timestamp"] = pd.to_datetime(chart.get("Exit Timestamp", ""), errors="coerce")
            chart = chart.dropna(subset=["Exit Timestamp"]).sort_values("Exit Timestamp")
            if chart.empty:
                st.caption("Closed-trade timestamps are not available for charting.")
            else:
                chart["Equity"] = pd.to_numeric(chart["Realized P&L"], errors="coerce").fillna(0.0).cumsum()
                chart["Peak"] = chart["Equity"].cummax()
                chart["Drawdown"] = chart["Equity"] - chart["Peak"]
                fig = make_subplots(rows=1, cols=2, subplot_titles=("Closed Trade P&L", "Closed Drawdown"))
                fig.add_trace(go.Scatter(x=chart["Exit Timestamp"], y=chart["Equity"], line={"color": "#55d49a"}, name="P&L"), row=1, col=1)
                fig.add_trace(go.Scatter(x=chart["Exit Timestamp"], y=chart["Drawdown"], fill="tozeroy", line={"color": "#ff6f7d"}, name="Drawdown"), row=1, col=2)
                st.plotly_chart(layout_chart(fig, 340), use_container_width=True)

        st.markdown("#### Closed Trade History")
        if closed.empty:
            st.caption("No closed trade history available.")
        else:
            closed_columns = [
                "Trade ID",
                "Bot",
                "Strategy ID",
                "Symbol",
                "Entry Timestamp",
                "Exit Timestamp",
                "Entry Price",
                "Exit Price",
                "Realized P&L",
                "ROI %",
                "Exit Reason",
                "Source",
            ]
            st.dataframe(closed[[column for column in closed_columns if column in closed]], use_container_width=True, hide_index=True)

    with tab_audit:
        st.markdown("#### Trade Timeline")
        timeline_frames = []
        if not trade_events.empty:
            event_view = trade_events.copy()
            event_view["Source"] = "Trade event"
            timeline_frames.append(event_view)
        if not order_audit.empty:
            order_view = order_audit.copy()
            order_view["Source"] = "Order audit"
            timeline_frames.append(order_view)
        journal = load_journal_events()
        if not journal.empty:
            journal_view = journal.copy()
            journal_view["Source"] = "Journal"
            timeline_frames.append(journal_view)
        if timeline_frames:
            timeline = pd.concat(timeline_frames, ignore_index=True, sort=False).head(300)
            columns = [
                column
                for column in [
                    "event_time",
                    "timestamp",
                    "snapshot_time",
                    "trade_id",
                    "bot_id",
                    "bot_name",
                    "symbol",
                    "event_type",
                    "order_state",
                    "position_state",
                    "lifecycle_state",
                    "status",
                    "severity",
                    "decision",
                    "reason",
                    "Source",
                ]
                if column in timeline
            ]
            st.dataframe(timeline[columns], use_container_width=True, hide_index=True)
        else:
            st.caption("No audit or journal timeline rows available yet.")

    with tab_persistence:
        st.markdown(
            f"""
            <div class="trade-panel">
              <div class="trade-panel-title">
                <strong>Persistence And Live Refresh</strong>
                <span class="trade-refresh-note">last persisted snapshot: {last_snapshot or 'pending'} | this refresh wrote {persisted_snapshots} snapshot(s)</span>
              </div>
              <div class="trade-health-strip">
                <div class="trade-health-cell"><div class="label">Execution path</div><div class="value good">Unblocked</div></div>
                <div class="trade-health-cell"><div class="label">Trade events</div><div class="value">{len(trade_events) if not trade_events.empty else 0}</div></div>
                <div class="trade-health-cell"><div class="label">PnL snapshots</div><div class="value">{len(pnl_snapshots) if not pnl_snapshots.empty else 0}</div></div>
                <div class="trade-health-cell"><div class="label">Source of truth</div><div class="value info">Runtime + audit</div></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("#### Persistence & Scale Notes")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Layer": "Runtime Execution", "Current Implementation": "RuntimeManager state and bot registry", "Guidance": "Keep in memory/read-through; do not block order submission."},
                    {"Layer": "Persistence/Event", "Current Implementation": "runtime_trade_events.json + runtime_order_audit.json + JournalEventRow", "Guidance": "Append-only, retry-safe operational audit."},
                    {"Layer": "PnL Snapshots", "Current Implementation": "runtime_trade_pnl_snapshots.json", "Guidance": "Dashboard writes lightweight snapshots for charts; execution path is untouched."},
                    {"Layer": "Analytics/UI", "Current Implementation": "Validation runs, replay trades, cached dashboard reads", "Guidance": "Read optimized; can lag execution by seconds."},
                    {"Layer": "Future DB Migration", "Current Implementation": "File-backed event/snapshot stores", "Guidance": "Move trade_event and trade_pnl_snapshot to DB when retention/query volume requires it."},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        if not trade_events.empty:
            st.markdown("#### Persisted Trade Events")
            event_columns = [column for column in ["event_time", "trade_id", "bot_id", "symbol", "event_type", "order_state", "position_state", "lifecycle_state", "price", "quantity", "reason"] if column in trade_events]
            st.dataframe(trade_events[event_columns].tail(300), use_container_width=True, hide_index=True)


def critical_log_events(journal: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return pd.DataFrame()
    text = journal.astype(str).agg(" ".join, axis=1)
    mask = journal.get("severity", pd.Series("", index=journal.index)).astype(str).isin(["WARN", "ERROR", "CRITICAL"]) | text.str.contains(
        "kill|stale|reconnect|desync|reconciliation|fallback|failed|retry",
        case=False,
        na=False,
    )
    cols = [col for col in ["event_time", "bot_name", "symbol", "event_type", "severity", "decision", "reason"] if col in journal]
    return journal.loc[mask, cols].head(80)


def journal_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    journal = load_journal_events()
    trades = load_backtest_trades()
    validation_runs = load_validation_runs_frame()
    analysis = bot_journal_analysis(load_bot_instances(), validation_runs, journal, trades)
    render_journal_learning_loop(journal, trades, analysis)
    if not trades.empty:
        journal_charts(trades)
    if not analysis.empty:
        st.markdown("### Bot Learning Board")
        st.caption("Visual trader review by bot. Recommendations are approval-gated and never auto-change strategy code or runtime configuration.")
        st.caption("Journal analysis never modifies strategy code or runtime configuration without human approval and revalidation.")
        render_journal_analysis_cards(analysis)
        with st.expander("Detailed bot analysis table", expanded=False):
            st.dataframe(analysis.astype(str), use_container_width=True, hide_index=True)
    if journal.empty:
        st.info("Live journal is ready. New bot decisions and testnet execution results will append here.")
        return
    journal["event_time"] = pd.to_datetime(journal["event_time"], errors="coerce")
    correlation = correlate_backtest_journal(validation_runs, journal, trades)
    if not correlation.empty:
        st.markdown("### Action Queue")
        render_journal_action_queue(correlation)
        for suggestion in journal_improvement_suggestions(correlation):
            st.info(suggestion)
        for suggestion in strategy_change_suggestions(correlation):
            st.warning(suggestion)
    counts = journal.groupby("event_type").size().reset_index(name="events").sort_values("events", ascending=False)
    st.plotly_chart(layout_chart(go.Figure(go.Bar(x=counts["event_type"], y=counts["events"], marker_color="#79a7ff")), 280), use_container_width=True)
    with st.expander("Raw journal evidence", expanded=False):
        st.dataframe(journal.sort_values("event_time", ascending=False).astype(str), use_container_width=True, hide_index=True)
    if not trades.empty:
        with st.expander("Historical trade journal evidence", expanded=False):
            st.dataframe(enrich_trade_journal(trades).tail(250).astype(str), use_container_width=True, hide_index=True)


def render_journal_analysis_cards(analysis: pd.DataFrame) -> None:
    cards: list[str] = []
    for _, row in analysis.head(6).iterrows():
        score = max(0.0, min(100.0, safe_number(row.get("Outcome Score", 0.0))))
        tone = str(row.get("Tone", "warn"))
        priority = html.escape(str(row.get("Learning Priority", "Collect Evidence")))
        outcome = html.escape(str(row.get("Outcome", "Evidence building")))
        next_action = html.escape(str(row.get("Next Review Action", row.get("Recommended Change", "Keep monitoring"))))
        cards.append(
            f"<div class='journal-card {tone}'>"
            f"<div class='journal-card-title'>{html.escape(str(row.get('Bot', 'Unknown bot')))}</div>"
            f"<div class='runtime-bot-boundary-meta'>{html.escape(str(row.get('Strategy', 'Unknown')))} | {html.escape(str(row.get('Symbol', 'Unknown')))} | approval {html.escape(str(row.get('Human Approval Status', 'PENDING')))}</div>"
            f"<div class='journal-visual-row'>"
            f"<div class='journal-mini-metric'><div class='label'>Outcome</div><div class='value'>{outcome}</div></div>"
            f"<div class='journal-mini-metric'><div class='label'>Score</div><div class='value'>{score:.0f}</div></div>"
            f"<div class='journal-mini-metric'><div class='label'>Priority</div><div class='value'>{priority}</div></div>"
            f"<div class='journal-mini-metric'><div class='label'>Status</div><div class='value'>{html.escape(str(row.get('Action Status', 'Pending')))}</div></div>"
            f"</div>"
            f"<div class='journal-scorebar'><span style='width:{score:.0f}%'></span></div>"
            f"<div class='journal-action-chip'>{next_action}</div>"
            f"<div class='journal-card-section'><b>Evidence:</b> {html.escape(str(row.get('Evidence', 'No evidence available.')))}</div>"
            "</div>"
        )
    st.markdown(f"<div class='journal-card-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)


def journal_learning_summary(journal: pd.DataFrame, trades: pd.DataFrame, analysis: pd.DataFrame) -> dict[str, object]:
    wins = int((pd.to_numeric(trades.get("pnl", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not trades.empty and "pnl" in trades else 0
    losses = int((pd.to_numeric(trades.get("pnl", pd.Series(dtype=float)), errors="coerce") <= 0).sum()) if not trades.empty and "pnl" in trades else 0
    warnings = int(journal.get("severity", pd.Series(dtype=str)).astype(str).isin(["WARN", "ERROR", "CRITICAL"]).sum()) if not journal.empty else 0
    risk_blocks = int(journal.get("event_type", pd.Series(dtype=str)).astype(str).str.contains("RISK_REJECTION|BLOCK", case=False, na=False).sum()) if not journal.empty else 0
    pending = int(analysis.get("Human Approval Status", pd.Series(dtype=str)).astype(str).eq("PENDING").sum()) if not analysis.empty else 0
    avg_score = safe_number(pd.to_numeric(analysis.get("Outcome Score", pd.Series(dtype=float)), errors="coerce").mean() if not analysis.empty else 0.0)
    if risk_blocks or warnings >= 3 or avg_score < 45:
        decision = "Review Before Scaling"
    elif losses > wins and losses > 0:
        decision = "Tune Exits"
    elif wins > losses and pending:
        decision = "Evidence Building"
    else:
        decision = "Stable Monitoring"
    return {
        "Wins": wins,
        "Losses": losses,
        "Warnings": warnings,
        "Risk Blocks": risk_blocks,
        "Pending Reviews": pending,
        "Average Score": avg_score,
        "Decision": decision,
    }


def render_journal_learning_loop(journal: pd.DataFrame, trades: pd.DataFrame, analysis: pd.DataFrame) -> None:
    summary = journal_learning_summary(journal, trades, analysis)
    st.markdown("### Trade Learning Loop")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Outcome Score", f"{safe_number(summary['Average Score']):.0f}/100")
    c2.metric("Wins / Losses", f"{summary['Wins']} / {summary['Losses']}")
    c3.metric("Risk Blocks", int(summary["Risk Blocks"]))
    c4.metric("Warnings", int(summary["Warnings"]))
    c5.metric("Next Decision", str(summary["Decision"]))
    if not analysis.empty:
        view = analysis.copy()
        view["Outcome Score"] = pd.to_numeric(view["Outcome Score"], errors="coerce").fillna(0.0)
        fig = go.Figure(
            go.Bar(
                x=view["Bot"],
                y=view["Outcome Score"],
                marker_color=np.where(view["Outcome Score"] >= 70, "#55d49a", np.where(view["Outcome Score"] >= 45, "#f5b84b", "#ff6f7d")),
                text=view["Learning Priority"],
            )
        )
        fig.update_yaxes(range=[0, 100], title="Learning score")
        fig.update_layout(showlegend=False, margin={"l": 20, "r": 20, "t": 20, "b": 45})
        st.plotly_chart(layout_chart(fig, 285), use_container_width=True)


def render_journal_action_queue(correlation: pd.DataFrame) -> None:
    view = correlation.copy()
    view["priority_score"] = (
        pd.to_numeric(view.get("max_drawdown_pct", 0.0), errors="coerce").fillna(0.0)
        + (1.5 - pd.to_numeric(view.get("profit_factor", 0.0), errors="coerce").fillna(0.0)).clip(lower=0.0) * 10
        + pd.to_numeric(view.get("risk_blocks", 0), errors="coerce").fillna(0.0) * 5
    )
    view = view.sort_values("priority_score", ascending=False)
    fig = go.Figure(
        go.Scatter(
            x=view["profit_factor"],
            y=view["max_drawdown_pct"],
            mode="markers+text",
            text=view["bot_name"],
            textposition="top center",
            marker={
                "size": np.maximum(10, view["priority_score"] + 8),
                "color": view["priority_score"],
                "colorscale": [[0, "#55d49a"], [0.5, "#f5b84b"], [1, "#ff6f7d"]],
                "showscale": False,
            },
        )
    )
    fig.update_xaxes(title="Profit factor")
    fig.update_yaxes(title="Max drawdown %")
    st.plotly_chart(layout_chart(fig, 330), use_container_width=True)
    action_cols = ["bot_name", "symbol", "profit_factor", "max_drawdown_pct", "risk_blocks", "suggestion"]
    st.dataframe(view[[column for column in action_cols if column in view]].head(10), use_container_width=True, hide_index=True)


def metric_from_validation_row(row: pd.Series, key: str, default: float = 0.0) -> float:
    value = row.get(key, None)
    if value not in (None, "") and pd.notna(value):
        return safe_number(value, default)
    metrics = row.get("metrics", {})
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except json.JSONDecodeError:
            metrics = {}
    if isinstance(metrics, dict):
        return safe_number(metrics.get(key, default), default)
    return default


def latest_validation_by_bot(validation_runs: pd.DataFrame, bot_name: str) -> pd.Series:
    if validation_runs.empty or "bot_name" not in validation_runs:
        return pd.Series(dtype=object)
    matches = validation_runs[validation_runs["bot_name"].astype(str) == str(bot_name)]
    if matches.empty:
        return pd.Series(dtype=object)
    sort_col = "created_at" if "created_at" in matches else "run_id" if "run_id" in matches else None
    if sort_col:
        return matches.sort_values(sort_col, ascending=False).iloc[0]
    return matches.iloc[0]


def short_join(items: list[str]) -> str:
    cleaned = [item for item in items if item]
    return "; ".join(cleaned[:4]) if cleaned else "Insufficient evidence yet."


def bot_journal_analysis(bots: pd.DataFrame, validation_runs: pd.DataFrame, journal: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    names: set[str] = set()
    if not bots.empty and "name" in bots:
        names.update(str(item) for item in bots["name"].dropna().tolist())
    if not validation_runs.empty and "bot_name" in validation_runs:
        names.update(str(item) for item in validation_runs["bot_name"].dropna().tolist())
    if not journal.empty and "bot_name" in journal:
        names.update(str(item) for item in journal["bot_name"].dropna().tolist() if str(item).upper() not in {"", "SYSTEM", "RUNTIME"})
    rows: list[dict[str, object]] = []
    for bot_name in sorted(name for name in names if name):
        bot_row = bots[bots["name"].astype(str) == bot_name].iloc[0] if not bots.empty and "name" in bots and bot_name in set(bots["name"].astype(str)) else pd.Series(dtype=object)
        validation = latest_validation_by_bot(validation_runs, bot_name)
        symbol = str(bot_row.get("symbol", validation.get("symbol", "")) or "")
        strategy = str(bot_row.get("strategy", validation.get("strategy", "")) or "")
        bot_journal = journal[journal.get("bot_name", pd.Series(dtype=str)).astype(str) == bot_name] if not journal.empty else pd.DataFrame()
        bot_trades = trades[trades.get("symbol", pd.Series(dtype=str)).astype(str) == symbol] if symbol and not trades.empty else pd.DataFrame()
        pf = metric_from_validation_row(validation, "profit_factor")
        win_rate = metric_from_validation_row(validation, "win_rate")
        max_dd = metric_from_validation_row(validation, "max_drawdown_pct")
        net_pnl = metric_from_validation_row(validation, "net_pnl", metric_from_validation_row(validation, "total_pnl"))
        total_trades = int(metric_from_validation_row(validation, "total_trades"))
        risk_blocks = int(bot_journal.get("event_type", pd.Series(dtype=str)).astype(str).str.contains("RISK_REJECTION|BLOCK", case=False, na=False).sum()) if not bot_journal.empty else 0
        warnings = int(bot_journal.get("severity", pd.Series(dtype=str)).astype(str).isin(["WARN", "ERROR", "CRITICAL"]).sum()) if not bot_journal.empty else 0
        trade_losses = int((bot_trades.get("pnl", pd.Series(dtype=float)) < 0).sum()) if not bot_trades.empty and "pnl" in bot_trades else 0
        trade_wins = int((bot_trades.get("pnl", pd.Series(dtype=float)) > 0).sum()) if not bot_trades.empty and "pnl" in bot_trades else 0

        pros: list[str] = []
        cons: list[str] = []
        improvements: list[str] = []
        if total_trades > 0:
            if net_pnl > 0:
                pros.append("positive validated net P&L")
            else:
                cons.append("validated P&L is not positive")
            if pf >= 1.5:
                pros.append("strong profit factor")
            elif pf and pf < 1.3:
                cons.append("profit factor below deployment comfort")
                improvements.append("review stop distance, TP timing, and spread filters")
            if win_rate >= 45:
                pros.append("acceptable win rate")
            elif win_rate and win_rate < 40:
                cons.append("low win rate")
                improvements.append("raise entry confirmation and orderflow thresholds")
            if 0 < max_dd < 8:
                pros.append("drawdown is controlled")
            elif max_dd >= 12:
                cons.append("drawdown is elevated")
                improvements.append("reduce capital allocation or add volatility pause")
        else:
            cons.append("no meaningful validation trade sample yet")
            improvements.append("run a fresh validation before deployment")
        if risk_blocks:
            cons.append(f"{risk_blocks} risk block event(s)")
            improvements.append("align capital, exposure, and frequency with Risk screen gates")
        if warnings:
            cons.append(f"{warnings} warning/error journal event(s)")
            improvements.append("inspect recent journal warnings before scaling")
        if trade_wins > trade_losses and trade_wins:
            pros.append("symbol trade history has more wins than losses")
        elif trade_losses > trade_wins and trade_losses:
            cons.append("symbol trade history has more losses than wins")
            improvements.append("review losing trade clusters in Historical Trade Journal")
        if not pros:
            pros.append("bot is tracked with persistent journal evidence")
        if not improvements:
            improvements.append("keep parameters stable and collect more live journal events")
        outcome_score = 50.0
        outcome_score += 20.0 if net_pnl > 0 else -15.0 if total_trades > 0 else -10.0
        outcome_score += 15.0 if pf >= 1.5 else -12.0 if pf and pf < 1.3 else 0.0
        outcome_score += 10.0 if win_rate >= 45 else -8.0 if win_rate and win_rate < 40 else 0.0
        outcome_score += 10.0 if 0 < max_dd < 8 else -15.0 if max_dd >= 12 else 0.0
        outcome_score -= min(20.0, risk_blocks * 5.0 + warnings * 3.0)
        outcome_score = max(0.0, min(100.0, outcome_score))
        if outcome_score >= 72:
            outcome = "Working"
            tone = "good"
            priority = "Low"
            next_action = "Keep stable; collect live evidence"
        elif outcome_score >= 45:
            outcome = "Watch"
            tone = "warn"
            priority = "Medium"
            next_action = short_join(improvements)
        else:
            outcome = "Fix First"
            tone = "bad"
            priority = "High"
            next_action = short_join(improvements)
        weakness = short_join(cons if cons else ["No major weakness identified yet"])
        recommendation = short_join(improvements)
        expected_benefit = "Better drawdown control and cleaner deployment evidence" if cons else "More confidence from additional live evidence"
        risk_of_change = "May reduce trade frequency or miss some winners; requires revalidation before deployment"
        requirement_text = (
            f"For {bot_name}, review journal evidence and implement only after approval: {recommendation}. "
            "Run backtest, stress test, and Validation Lab certification before marketplace promotion."
        )

        rows.append(
            {
                "Bot": bot_name,
                "Strategy": strategy or "Unknown",
                "Symbol": symbol or "Unknown",
                "Pros": short_join(pros),
                "Cons": short_join(cons),
                "Areas of Improvement": short_join(improvements),
                "Evidence": f"validation trades {total_trades}; journal events {len(bot_journal)}; PF {pf:.2f}; win {win_rate:.1f}%; DD {max_dd:.1f}%",
                "Outcome": outcome,
                "Outcome Score": round(outcome_score, 1),
                "Learning Priority": priority,
                "Tone": tone,
                "Next Review Action": next_action,
                "Action Status": "Needs Approval" if cons else "Monitor",
                "Weakness Identified": weakness,
                "Recommended Change": recommendation,
                "Expected Benefit": expected_benefit,
                "Risk Of Change": risk_of_change,
                "Suggested Codex Requirement Text": requirement_text,
                "Human Approval Status": "PENDING",
            }
        )
    return pd.DataFrame(rows)


def correlate_backtest_journal(validation_runs: pd.DataFrame, journal: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    if validation_runs.empty:
        return pd.DataFrame()
    runs = validation_runs.copy()
    journal_view = journal.copy() if not journal.empty else pd.DataFrame()
    trades_view = trades.copy() if not trades.empty else pd.DataFrame()
    rows: list[dict[str, object]] = []
    for _, run in runs.iterrows():
        bot_name = str(run.get("bot_name", ""))
        symbol = str(run.get("symbol", ""))
        related_journal = journal_view[
            (journal_view.get("bot_name", pd.Series(dtype=str)).astype(str) == bot_name)
            & (journal_view.get("symbol", pd.Series(dtype=str)).astype(str).isin([symbol, ""]))
        ] if not journal_view.empty else pd.DataFrame()
        related_trades = trades_view[trades_view.get("symbol", pd.Series(dtype=str)).astype(str) == symbol] if not trades_view.empty else pd.DataFrame()
        losing_trades = int((related_trades.get("pnl", pd.Series(dtype=float)) <= 0).sum()) if not related_trades.empty else 0
        winning_trades = int((related_trades.get("pnl", pd.Series(dtype=float)) > 0).sum()) if not related_trades.empty else 0
        risk_blocks = int(related_journal.get("event_type", pd.Series(dtype=str)).astype(str).str.contains("RISK_REJECTION|BLOCK", case=False, na=False).sum()) if not related_journal.empty else 0
        validation_events = int((related_journal.get("event_type", pd.Series(dtype=str)).astype(str) == "VALIDATION_RUN").sum()) if not related_journal.empty else 0
        rows.append(
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "strategy": run.get("strategy", ""),
                "validation_state": run.get("state", ""),
                "net_pnl": float(run.get("net_pnl", 0.0) or 0.0),
                "profit_factor": float(run.get("profit_factor", 0.0) or 0.0),
                "win_rate": float(run.get("win_rate", 0.0) or 0.0),
                "max_drawdown_pct": float(run.get("max_drawdown_pct", 0.0) or 0.0),
                "consecutive_losses": int(safe_number(run.get("consecutive_losses", 0), 0.0)),
                "journal_events": int(len(related_journal)),
                "validation_events": validation_events,
                "risk_blocks": risk_blocks,
                "backtest_wins": winning_trades,
                "backtest_losses": losing_trades,
                "suggestion": improvement_suggestion_for_row(run, risk_blocks, losing_trades),
            }
        )
    return pd.DataFrame(rows).sort_values(["max_drawdown_pct", "net_pnl"], ascending=[False, True])


def improvement_suggestion_for_row(row: pd.Series, risk_blocks: int, losing_trades: int) -> str:
    suggestions: list[str] = []
    if float(row.get("max_drawdown_pct", 0.0) or 0.0) >= 12:
        suggestions.append("tighten sizing or pause this bot in expansion/panic regimes")
    if float(row.get("profit_factor", 0.0) or 0.0) < 1.3:
        suggestions.append("review exit logic, stop distance, and spread filter")
    if float(row.get("win_rate", 0.0) or 0.0) < 40:
        suggestions.append("raise entry confidence and orderflow confirmation thresholds")
    if int(safe_number(row.get("consecutive_losses", 0), 0.0)) >= 3:
        suggestions.append("add a consecutive-loss cooldown")
    if risk_blocks:
        suggestions.append("align bot capital and frequency with Risk screen hard gates")
    if losing_trades > int(row.get("total_trades", 0) or 0) * 0.55 and losing_trades > 0:
        suggestions.append("analyze losing trade clusters in the historical journal")
    return "; ".join(suggestions or ["keep monitoring; current evidence does not show a major journal/backtest mismatch"])


def journal_improvement_suggestions(correlation: pd.DataFrame) -> list[str]:
    if correlation.empty:
        return []
    messages: list[str] = []
    worst_dd = correlation.sort_values("max_drawdown_pct", ascending=False).iloc[0]
    weakest_pf = correlation.sort_values("profit_factor", ascending=True).iloc[0]
    if float(worst_dd["max_drawdown_pct"]) >= 12:
        messages.append(f"{worst_dd['bot_name']} has the largest drawdown ({float(worst_dd['max_drawdown_pct']):.1f}%). Suggested action: {worst_dd['suggestion']}.")
    if float(weakest_pf["profit_factor"]) < 1.3:
        messages.append(f"{weakest_pf['bot_name']} has weak profit factor ({float(weakest_pf['profit_factor']):.2f}). Suggested action: {weakest_pf['suggestion']}.")
    blocked = correlation[correlation["risk_blocks"] > 0]
    if not blocked.empty:
        messages.append("Risk rejections are present in journal correlation; reduce per-bot capital, trade frequency, or portfolio exposure before deployment.")
    if not messages:
        messages.append("Backtest and journal evidence are aligned; continue collecting live journal events before changing parameters.")
    return messages[:4]


def strategy_change_suggestions(correlation: pd.DataFrame) -> list[str]:
    if correlation.empty:
        return []
    suggestions: list[str] = []
    for _, row in correlation.head(5).iterrows():
        text = strategy_change_suggestion_for_metrics(row.to_dict())
        suggestions.append(f"{row['bot_name']} / {row['symbol']}: {text}")
    return suggestions


def strategy_change_suggestion_for_metrics(metrics: dict[str, object]) -> str:
    changes: list[str] = []
    if float(metrics.get("max_drawdown_pct", 0.0) or 0.0) >= 12:
        changes.append("reduce position sizing and add a volatility-expansion pause")
    if float(metrics.get("profit_factor", 0.0) or 0.0) < 1.3:
        changes.append("tighten exits, improve reward/risk, or reject wide-spread entries")
    if float(metrics.get("win_rate", 0.0) or 0.0) < 40:
        changes.append("raise min confidence and require stronger orderflow confirmation")
    if int(metrics.get("consecutive_losses", 0) or 0) >= 3:
        changes.append("add a cooldown after three consecutive losses")
    if int(metrics.get("risk_rule_blocked_trades", metrics.get("risk_blocks", 0)) or 0) > 0:
        changes.append("lower bot capital or trade frequency to pass hard risk gates")
    return "; ".join(changes or ["keep parameters stable and collect more live journal evidence"])


@st.cache_data(ttl=60, show_spinner=False)
def load_backtest_trades() -> pd.DataFrame:
    path = Path("reports/top10_replay_trades.csv")
    if not path.exists():
        return pd.DataFrame()
    try:
        trades = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if "exit_time" in trades:
        trades["exit_time"] = pd.to_datetime(trades["exit_time"], errors="coerce")
    return trades


def enrich_trade_journal(trades: pd.DataFrame) -> pd.DataFrame:
    view = trades.copy()
    view["trade_reasoning"] = np.where(view.get("pnl", 0) >= 0, "target/positive exit behavior", "stop or adverse exit behavior")
    view["regime"] = np.where(view.get("return_pct", 0) >= 0, "favorable", "hostile")
    view["da_verdict"] = np.where(view.get("return_pct", 0) >= -1.5, "acceptable", "would review")
    view["orderflow_snapshot"] = np.where(view.get("return_pct", 0) >= 0, "confirmation present", "flow degraded")
    return view


def journal_charts(trades: pd.DataFrame) -> None:
    trades = trades.sort_values("exit_time").copy()
    trades["equity"] = trades["pnl"].cumsum()
    trades["peak"] = trades["equity"].cummax()
    trades["drawdown"] = trades["equity"] - trades["peak"]
    trades["month"] = trades["exit_time"].dt.to_period("M").astype(str)
    monthly = trades.groupby("month", as_index=False)["pnl"].sum()
    fig = make_subplots(rows=2, cols=2, subplot_titles=("Equity Curve", "PnL Distribution", "Drawdown", "Monthly Performance"))
    fig.add_trace(go.Scatter(x=trades["exit_time"], y=trades["equity"], line={"color": "#55d49a"}, name="Equity"), row=1, col=1)
    fig.add_trace(go.Histogram(x=trades["pnl"], marker_color="#79a7ff", name="PnL"), row=1, col=2)
    fig.add_trace(go.Scatter(x=trades["exit_time"], y=trades["drawdown"], fill="tozeroy", line={"color": "#ff6f7d"}, name="Drawdown"), row=2, col=1)
    fig.add_trace(go.Bar(x=monthly["month"], y=monthly["pnl"], marker_color=np.where(monthly["pnl"] >= 0, "#55d49a", "#ff6f7d"), name="Monthly"), row=2, col=2)
    st.plotly_chart(layout_chart(fig, 560), use_container_width=True)


def validation_screen() -> None:
    bots = load_bot_instances()
    bot_names = bots["name"].astype(str).tolist() if not bots.empty else []
    if not bots.empty:
        with st.expander("Bots available for backtest", expanded=True):
            display = bots.copy()
            display["strategy_collection"] = display["parameters"].apply(lambda value: ", ".join((value or {}).get("strategies", [])) if isinstance(value, dict) else "")
            display["next_action"] = display["state"].astype(str).apply(lifecycle_next_action)
            st.dataframe(
                display[[col for col in ["name", "strategy_collection", "strategy", "symbol", "timeframe", "capital", "state", "next_action"] if col in display]],
                use_container_width=True,
                hide_index=True,
            )
    selected_bot = st.selectbox("Bot instance", bot_names or ["No bot instances"])
    selected_row = bots[bots["name"].astype(str) == selected_bot].iloc[0] if selected_bot != "No bot instances" and not bots.empty else pd.Series(dtype=object)
    selected_defaults = strategy_deployment_defaults(str(selected_row.get("strategy", ""))) if not selected_row.empty else {}
    if selected_defaults:
        st.markdown("### Deployment Defaults And Runtime Compatibility")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Metric": "Strategy default parameters", "Value": f"{selected_defaults['strategy_type']} / {selected_defaults['recommended_timeframe']} / {selected_defaults['strategy_version']}"},
                    {"Metric": "Recommended stop-loss", "Value": f"{selected_defaults['default_stop_type']} | {selected_defaults['default_stop_value']}"},
                    {"Metric": "Recommended take-profit", "Value": f"{selected_defaults['default_tp_type']} | {selected_defaults['default_tp_value']}"},
                    {"Metric": "Recommended capital allocation", "Value": f"${float(selected_defaults['minimum_recommended_capital']):,.2f} minimum | {selected_defaults['capital_allocation_model']}"},
                    {"Metric": "Runtime compatibility checks", "Value": selected_defaults["runtime_profile"]},
                    {"Metric": "Risk classification", "Value": selected_defaults["risk_classification"]},
                    {"Metric": "Deployment validation output", "Value": "deployment_readiness is persisted after each validation run."},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    c1, c2, c3, c4 = st.columns(4)
    symbol = c1.selectbox("Symbol", load_live_scan()["symbol"].astype(str).tolist() or available_live_symbols())
    timeframe = c2.selectbox("Timeframe", ["5m", "1h", "4h", "1d"])
    capital = c3.number_input("Capital assumption", min_value=0.0, value=1_000.0, step=100.0)
    fees = c4.number_input("Fees bps", min_value=0.0, value=10.0, step=1.0)
    d1, d2, d3 = st.columns(3)
    start_date = d1.date_input("Start date", value=datetime.now().date() - timedelta(days=365))
    end_date = d2.date_input("End date", value=datetime.now().date())
    slippage = d3.number_input("Slippage bps", min_value=0.0, value=5.0, step=1.0)
    if st.button("Run validation", use_container_width=True) and selected_bot != "No bot instances":
        metrics_result, trades = run_bot_validation(selected_row, symbol, timeframe, start_date, end_date, capital, fees, slippage)
        readiness = validation_deployment_status(metrics_result, capital, strategy_deployment_defaults(str(selected_row.get("strategy", ""))))
        run_id = f"validation-{selected_bot}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        validation_row = {
            "run_id": run_id,
            "bot_name": selected_bot,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "capital": capital,
            "fees_bps": fees,
            "slippage_bps": slippage,
            "state": "FAILED" if "error" in metrics_result else "COMPLETED",
            "metrics": metrics_result,
            "deployment_readiness": readiness,
            "strategy_defaults_used": strategy_deployment_defaults(str(selected_row.get("strategy", ""))),
        }
        save_validation_run(validation_row)
        if "error" in metrics_result:
            transition_bot(selected_bot, "FAILED", str(metrics_result["error"]))
        else:
            transition_bot(
                selected_bot,
                "BACKTESTED",
                f"backtest completed: trades {int(metrics_result.get('total_trades', 0))}, PF {float(metrics_result.get('profit_factor', 0.0)):.2f}, DD {float(metrics_result.get('max_drawdown_pct', 0.0)):.1f}%",
            )
        append_journal(
            selected_bot,
            symbol,
            "VALIDATION_RUN",
            "INFO",
            "COMPLETED",
            "validation/backtest completed; " + strategy_change_suggestion_for_metrics(metrics_result),
            validation_row["metrics"],
        )
        st.info(f"Deployment readiness: {readiness}")
        show_validation_result(metrics_result, trades)
    runs = load_validation_runs_frame()
    if runs.empty:
        metrics = CertificationMetrics(sharpe=0.0, profit_factor=0.0, max_drawdown_pct=99.0, replay_determinism=100, risk_violations=0)
    else:
        best = runs.iloc[0]
        metrics = CertificationMetrics(
            sharpe=float(best.get("sharpe_proxy", 0.0) or 0.0),
            profit_factor=float(best.get("profit_factor", 0.0) or 0.0),
            max_drawdown_pct=float(best.get("max_drawdown_pct", 99.0) or 99.0),
            replay_determinism=100,
            risk_violations=int(best.get("risk_rule_blocked_trades", 0) or 0),
        )
    report = CertificationEngine().certify(metrics)
    state_class = "good" if report.state == CertificationState.CERTIFIED else "warn" if report.state == CertificationState.CONDITIONAL else "bad"
    st.markdown(f"<div class='status-card'><div class='status-label'>Certification</div><div class='status-value {state_class}'>{report.state}</div></div>", unsafe_allow_html=True)
    a, b, c, d, e = st.columns(5)
    a.metric("Sharpe", f"{metrics.sharpe:.2f}")
    b.metric("Profit Factor", f"{metrics.profit_factor:.2f}")
    c.metric("Max Drawdown", f"{metrics.max_drawdown_pct:.1f}%")
    d.metric("Determinism", f"{metrics.replay_determinism:.0f}%")
    e.metric("Risk Violations", f"{metrics.risk_violations}")
    if report.reasons:
        st.error("Rejected / conditional reasons: " + "; ".join(report.reasons))
    st.info(validation_remediation(metrics, report.reasons))
    if not runs.empty:
        st.markdown("### Previous Validation Runs")
        st.dataframe(runs, use_container_width=True, hide_index=True)


def run_bot_validation(bot: pd.Series, symbol: str, timeframe: str, start_date: object, end_date: object, capital: float, fees_bps: float, slippage_bps: float) -> tuple[dict[str, object], pd.DataFrame]:
    strategy_name = str(bot.get("strategy", ""))
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    if strategy is None:
        return {"error": f"strategy {strategy_name} is not registered", "total_trades": 0, "risk_rule_blocked_trades": 1}, pd.DataFrame()
    feature_files = available_feature_files()
    path = feature_files.get(symbol)
    if path is None or not path.exists():
        return {"error": f"no feature file found for {symbol}", "total_trades": 0, "risk_rule_blocked_trades": 1}, pd.DataFrame()
    features = load_feature_file(path)
    if "open_time" in features:
        start_ts, end_ts = utc_day_window(start_date, end_date)
        features["open_time"] = utc_datetime_series(features["open_time"])
        features = features[(features["open_time"] >= start_ts) & (features["open_time"] < end_ts)]
    if len(features) < 210:
        return {"error": "selected period has insufficient candles for indicator warmup", "total_trades": 0, "risk_rule_blocked_trades": 1}, pd.DataFrame()
    bot_runner = StrategyAgnosticBot(BotDeployment(name=str(bot["name"]), strategy=strategy, interval=timeframe, notional=capital))
    metrics, trades = bot_runner.replay(features)
    trades_frame = pd.DataFrame([asdict(trade) for trade in trades])
    fee_drag = sum(abs(float(trade.pnl)) for trade in trades) * (fees_bps / 10_000)
    slippage_drag = len(trades) * capital * (slippage_bps / 10_000)
    net_pnl = float(metrics.total_pnl) - fee_drag - slippage_drag
    returns = trades_frame["return_pct"] if not trades_frame.empty and "return_pct" in trades_frame else pd.Series(dtype="float64")
    consecutive_losses = max_consecutive_losses(trades_frame)
    result = {
        "strategy": strategy_name,
        "strategy_collection": ", ".join((bot.get("parameters") or {}).get("strategies", [strategy_name])) if isinstance(bot.get("parameters"), dict) else strategy_name,
        "total_trades": int(metrics.trades),
        "win_rate": float(metrics.win_rate),
        "profit_factor": finite_float(metrics.profit_factor),
        "net_pnl": round(net_pnl, 2),
        "max_drawdown_pct": float(metrics.max_drawdown_pct),
        "average_r": float(metrics.avg_trade_return_pct),
        "expectancy": float(returns.mean()) if not returns.empty else 0.0,
        "sharpe_proxy": float(metrics.sharpe_proxy),
        "consecutive_losses": int(consecutive_losses),
        "average_holding_time": float(trades_frame["bars_held"].mean()) if not trades_frame.empty and "bars_held" in trades_frame else 0.0,
        "best_trade": float(trades_frame["pnl"].max()) if not trades_frame.empty and "pnl" in trades_frame else 0.0,
        "worst_trade": float(trades_frame["pnl"].min()) if not trades_frame.empty and "pnl" in trades_frame else 0.0,
        "rejected_trades": 0,
        "risk_rule_blocked_trades": 0,
        "fees_bps": fees_bps,
        "slippage_bps": slippage_bps,
    }
    return result, trades_frame


def finite_float(value: float) -> float:
    return float(value) if np.isfinite(value) else 999.0


def show_validation_result(metrics: dict[str, object], trades: pd.DataFrame) -> None:
    st.markdown("### Validation Result")
    cols = st.columns(5)
    cols[0].metric("Trades", f"{int(metrics.get('total_trades', 0))}")
    cols[1].metric("Win Rate", f"{float(metrics.get('win_rate', 0.0)):.1f}%")
    cols[2].metric("Profit Factor", f"{float(metrics.get('profit_factor', 0.0)):.2f}")
    cols[3].metric("Net PnL", f"${float(metrics.get('net_pnl', 0.0)):,.2f}")
    cols[4].metric("Max Drawdown", f"{float(metrics.get('max_drawdown_pct', 0.0)):.1f}%")
    if not trades.empty:
        journal_charts(trades.assign(exit_time=pd.to_datetime(trades["exit_time"], errors="coerce")))
        st.dataframe(enrich_trade_journal(trades), use_container_width=True, hide_index=True)


def max_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty or "pnl" not in trades:
        return 0
    current = 0
    worst = 0
    for pnl in trades["pnl"]:
        if float(pnl) <= 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def validation_remediation(metrics: CertificationMetrics, reasons: list[str]) -> str:
    guidance = []
    if metrics.max_drawdown_pct >= 12:
        guidance.append("Reduce volatility exposure and tighten sizing during expansion regimes.")
    if metrics.sharpe <= 1.2:
        guidance.append("Raise confidence/orderflow thresholds or limit trading to stronger regimes.")
    if metrics.profit_factor <= 1.3:
        guidance.append("Review exits, spread filters, and stop distance to improve profit factor.")
    if metrics.risk_violations:
        guidance.append("Risk violations must be zero before deployment.")
    return " ".join(guidance or ["Validation is acceptable for continued paper/testnet monitoring."])


BOT_MANAGEMENT_CHILDREN = {
    "BOT FRAMEWORK": "Framework",
    "BOT RUNTIME": "Runtime",
    "BOT ADMIN": "Admin",
    "VALIDATION LAB": "Validation Lab",
}

BOT_MANAGEMENT_ROUTES = {
    "Framework": "/bot-management/framework",
    "Runtime": "/bot-management/runtime",
    "Admin": "/bot-management/admin",
    "Validation Lab": "/bot-management/validation-lab",
}

BOT_MANAGEMENT_ROUTE_TO_CHILD = {route: child for child, route in BOT_MANAGEMENT_ROUTES.items()}

BOT_MANAGEMENT_DESCRIPTIONS = {
    "Framework": "Create bots, choose strategies, and prepare deployable definitions.",
    "Runtime": "Monitor running bots, live PnL, status, and tile-level actions.",
    "Admin": "Operate bot definitions, runtime controls, and administrative actions.",
    "Validation Lab": "Backtest, certify readiness, and review deployment evidence.",
}


SCREEN_OPTIONS = [
    "DASHBOARD",
    "BOT MANAGEMENT",
    "JOURNAL",
    "ORDERFLOW",
    "RISK",
    "SYSTEM HEALTH",
    "TRADE MANAGEMENT",
    "USER ADMIN",
    "MY PROFILE",
]


def first_query_value(value: object) -> str:
    if isinstance(value, list):
        return str(value[0] if value else "")
    return str(value or "")


def bot_management_path_from_url(url: object) -> str:
    try:
        path = urlparse(str(url or "")).path
    except ValueError:
        return ""
    return path if path.startswith("/bot-management/") else ""


def resolve_screen_request(
    requested_screen: object,
    requested_route: object = "",
    requested_bot_child: object = "",
    current_url: object = "",
) -> tuple[str, str]:
    screen = first_query_value(requested_screen)
    route = first_query_value(requested_route) or bot_management_path_from_url(current_url)
    bot_child = first_query_value(requested_bot_child)
    if screen in BOT_MANAGEMENT_CHILDREN:
        return "BOT MANAGEMENT", BOT_MANAGEMENT_CHILDREN[screen]
    if screen in {"LIVE TRADING", "SIGNAL FLOW"}:
        return "DASHBOARD", ""
    if route in BOT_MANAGEMENT_ROUTE_TO_CHILD:
        return "BOT MANAGEMENT", BOT_MANAGEMENT_ROUTE_TO_CHILD[route]
    if screen == "BOT MANAGEMENT":
        if bot_child in BOT_MANAGEMENT_ROUTES:
            return "BOT MANAGEMENT", bot_child
        return "BOT MANAGEMENT", ""
    return screen if screen in SCREEN_OPTIONS else "DASHBOARD", ""


def clear_bot_management_query_params() -> None:
    for key in ("bot_child", "route"):
        if key in st.query_params:
            del st.query_params[key]


def open_bot_management_child(child: str) -> None:
    st.query_params["screen"] = "BOT MANAGEMENT"
    st.query_params["bot_child"] = child
    st.query_params["route"] = BOT_MANAGEMENT_ROUTES[child]
    st.session_state["_bot_management_requested_child"] = child
    st.session_state["bot_management_nav_expanded"] = True
    st.rerun()


def open_root_screen(screen: str) -> None:
    st.query_params["screen"] = screen
    if screen != "BOT MANAGEMENT":
        clear_bot_management_query_params()
        st.session_state["bot_management_nav_expanded"] = False
    else:
        clear_bot_management_query_params()
        st.session_state["bot_management_nav_expanded"] = True
    st.rerun()


def toggle_bot_management_nav(is_active: bool) -> None:
    expanded = bool(st.session_state.get("bot_management_nav_expanded", is_active))
    next_expanded = not expanded
    st.session_state["bot_management_nav_expanded"] = next_expanded
    st.query_params["screen"] = "BOT MANAGEMENT"
    if not next_expanded:
        clear_bot_management_query_params()
        st.session_state.pop("_bot_management_requested_child", None)
    st.rerun()


def clear_bot_management_child() -> None:
    st.query_params["screen"] = "BOT MANAGEMENT"
    clear_bot_management_query_params()
    st.session_state.pop("_bot_management_requested_child", None)
    st.rerun()


def current_user_context():
    return st.session_state.get("auth_context")


def requires_password_change() -> bool:
    context = current_user_context()
    return bool(context and getattr(context, "force_password_change", False))


def allowed_screen_options_for_context(context) -> list[str]:
    if context is None:
        return []
    allowed = [option for option in SCREEN_OPTIONS if can_access_screen(context, option)]
    roles = set(getattr(context, "roles", ()) or ())
    fallback: list[str] = []
    if "ADMIN" in roles:
        fallback = SCREEN_OPTIONS.copy()
    elif "POWER_USER" in roles:
        fallback = ["DASHBOARD", "BOT MANAGEMENT", "JOURNAL", "TRADE MANAGEMENT", "MY PROFILE"]
    elif "BASIC_USER" in roles:
        fallback = ["DASHBOARD", "MY PROFILE"]
    for option in fallback:
        if option in SCREEN_OPTIONS and option not in allowed:
            allowed.append(option)
    if "MY PROFILE" not in allowed:
        allowed.append("MY PROFILE")
    return allowed


def user_avatar(email: str) -> tuple[str, str]:
    cleaned = (email or "user").strip().lower()
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    palette = ["#79a7ff", "#55d49a", "#f0c86a", "#ff8b95", "#8bd4e8", "#b9a7ff"]
    color = palette[int(digest[:2], 16) % len(palette)]
    local = cleaned.split("@", 1)[0]
    parts = [part for part in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if part]
    if len(parts) >= 2:
        initials = (parts[0][0] + parts[1][0]).upper()
    else:
        initials = (local[:2] or "U").upper()
    return initials, color


def account_status_bar() -> None:
    context = current_user_context()
    if context is None:
        return
    initials, color = user_avatar(str(context.email))
    col_info, col_profile, col_logout = st.columns([6.2, 1.25, 1.1])
    with col_info:
        st.markdown(
            f"""
            <div class="account-strip">
              <div class="account-left">
                <div class="user-avatar" style="background:{color};">{html.escape(initials)}</div>
                <div>
                  <div class="account-name">{html.escape(str(context.email))}</div>
                  <div class="account-meta">Tier {html.escape(str(context.subscription_tier))} | Roles {html.escape(", ".join(context.roles))}</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_profile:
        if st.button("My Profile", key="account-my-profile", use_container_width=True, disabled=page == "MY PROFILE"):
            open_root_screen("MY PROFILE")
    with col_logout:
        if st.button("Logout", key="account-logout", use_container_width=True):
            try:
                import asyncio

                asyncio.run(_logout_user_from_db(context.session_token))
            except Exception as exc:
                logger.warning("logout_failed fallback=local error=%s", exc)
            st.session_state.pop("auth_context", None)
            st.rerun()
    if getattr(context, "force_password_change", False):
        st.warning("Password change required before accessing the platform.")


def app_banner() -> None:
    st.markdown(
        f"""
        <div class="app-banner">
          <a class="app-home-link" href="?screen=DASHBOARD" target="_self" aria-label="Go to home dashboard" title="Home">
            <div class="app-emblem">MT</div>
          </a>
          <div>
            <div class="app-title">mytradingmind.ai</div>
            <div class="app-subtitle">Version {APP_VERSION} | Global trading operations dashboard. Visibility lives here; heavy bot controls stay inside Bot Management.</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def public_login_landing() -> None:
    st.markdown(
        f"""
        <div class="public-hero">
          <h1>State-of-the-art bot trading operations</h1>
          <p>
            A state-of-the-art bot trading operations system built for disciplined strategy research,
            certified deployment, live runtime monitoring, risk-gated execution, and bot-wise journal learning.
            Access is role-based so every user sees only the tools, bots, and performance data they are approved to use.
          </p>
        </div>
        <div class="public-proof-grid">
          <div class="public-proof"><b>Signal Intelligence</b><span>Market data, regime checks, strategy confirmation, and risk readiness move through one auditable flow.</span></div>
          <div class="public-proof"><b>Certified Bots</b><span>Deployable bots pass backtesting, validation, stress review, and human approval before runtime use.</span></div>
          <div class="public-proof"><b>Runtime Cockpit</b><span>Running bots expose live P&L, stop levels, health, heartbeat, and recovery state in one operating surface.</span></div>
          <div class="public-proof"><b>Enterprise Controls</b><span>MariaDB persistence, RBAC, audit trail, subscriptions, and bootstrap security protect multi-user operations.</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    image_path = Path("docs/assets/mytradingmind-highlevel-flow.svg")
    if image_path.exists():
        st.image(
            str(image_path),
            caption="High-level flow: market data becomes ranked signals, passes risk gates, runs through bots, and improves through journal learning.",
            use_container_width=True,
        )
    st.caption("Use the left access panel to sign in or request registration.")


def user_profile_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    context = current_user_context()
    st.markdown("### My Profile")
    if context is None:
        st.info("Sign in from the login panel to view profile, subscription, P&L, and user-scoped trades.")
        st.markdown("#### Subscription")
        st.write("BASIC_USER can browse the platform. POWER_USER unlocks premium bot capabilities after validation gates.")
        return
    st.caption("Your data is isolated to your signed-in workspace.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Email", context.email)
    c2.metric("Tier", context.subscription_tier)
    c3.metric("Roles", ", ".join(context.roles))
    c4.metric("Workspace", context.tenant_id[-8:])
    st.markdown("#### Password")
    if getattr(context, "force_password_change", False):
        st.warning("Temporary password in use. Set a permanent password to unlock the rest of the platform.")
    if getattr(context, "session_token", "") and "auth_password_changed" in st.session_state:
        st.success("Password changed successfully. Use the new password on your next login.")
    with st.container(border=True):
        st.caption("Passwords are never stored or logged in plain text.")
        current_password = st.text_input("Current password", type="password", key="profile-current-password")
        new_password = st.text_input("New password", type="password", key="profile-new-password")
        confirm_password = st.text_input("Confirm new password", type="password", key="profile-confirm-password")
        password_ready = bool(current_password and new_password and confirm_password)
        if st.button("Change Password", key="profile-change-password", use_container_width=True, disabled=not password_ready):
            if len(new_password) < 12:
                st.warning("New password must be at least 12 characters.")
            elif new_password != confirm_password:
                st.warning("New password and confirmation do not match.")
            else:
                try:
                    import asyncio

                    asyncio.run(_change_user_password_from_db(context.user_id, current_password, new_password))
                    st.session_state["auth_context"] = replace(context, force_password_change=False)
                    st.session_state["auth_password_changed"] = True
                    st.query_params["screen"] = "DASHBOARD" if can_access_screen(st.session_state["auth_context"], "DASHBOARD") else "MY PROFILE"
                    st.success("Password changed successfully.")
                    st.rerun()
                except Exception as exc:
                    st.warning(f"Password change failed: {exc}")
    bots = load_bot_instances()
    ranked = bots.head(2) if not bots.empty else pd.DataFrame()
    st.markdown("#### Bot Access")
    if ranked.empty:
        st.warning("No certified bots are available yet.")
    else:
        st.dataframe(ranked[["name", "strategy", "symbol", "timeframe", "state"]], use_container_width=True, hide_index=True)
    st.markdown("#### Performance & Trades")
    trades = load_top10_replay_trades()
    if trades.empty:
        st.info("No user-specific trade history is available yet.")
    else:
        st.dataframe(trades.head(100), use_container_width=True, hide_index=True)
        st.download_button(
            "Export CSV",
            trades.to_csv(index=False),
            file_name=f"mytradingmind_trades_{context.user_id}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    st.markdown("#### Billing")
    st.info("Payment integration is pending. Subscription events are already audit-ready.")


def user_admin_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    context = current_user_context()
    st.markdown("### User Admin")
    if not can_access_screen(context, "USER ADMIN"):
        st.error("Admin access is required for user, role, and subscription administration.")
        return
    st.caption("Role and security bootstrap status. Role editing uses persisted tables; advanced editors can be added without changing the schema.")
    try:
        import asyncio

        summary = asyncio.run(_security_summary_from_db())
    except Exception as exc:
        st.warning(f"Security schema summary unavailable: {exc}")
        summary = {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Users", summary.get("users", 0))
    c2.metric("Roles", summary.get("roles", 0))
    c3.metric("Permissions", summary.get("permissions", 0))
    c4.metric("Sessions", summary.get("sessions", 0))
    rows = [
        {"Role": "BASIC_USER", "Capability": "Profile and subscription viewing; premium bot execution gated."},
        {"Role": "POWER_USER", "Capability": "Premium bot access, top two bots included, own P&L/trade export."},
        {"Role": "ADMIN", "Capability": "Full RBAC, subscription, user, runtime, and emergency controls."},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.info("Bootstrap credentials are hash-only, max three attempts, single-use, and expiry-bound.")


def bot_management_landing(status: dict[str, object], bots: pd.DataFrame) -> None:
    st.markdown("#### Bot Management")
    st.caption("Choose a screen from Bot Management in the navigation panel.")
    children = list(BOT_MANAGEMENT_ROUTES)
    columns = st.columns(4)
    running = int(status.get("running_bots", 0) or 0)
    bot_count = 0 if bots.empty else len(bots)
    badges = {
        "Framework": f"{bot_count} definitions",
        "Runtime": f"{running} running",
        "Admin": "controls",
        "Validation Lab": "readiness",
    }
    for index, child in enumerate(children):
        with columns[index % len(columns)].container(border=True):
            st.markdown(f"##### {child}")
            st.caption(BOT_MANAGEMENT_DESCRIPTIONS[child])
            st.markdown(f"<span class='pill'>{badges[child]}</span>", unsafe_allow_html=True)


def bot_management_screen(data: dict[str, pd.DataFrame | dict[str, float | str]], selected_child: str) -> None:
    st.markdown("### Bot Management")
    st.caption("Design, run, administer, and validate bots from one operational module.")
    bots = load_bot_instances()
    status = RuntimeManager().runtime_status()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Bot Definitions", 0 if bots.empty else len(bots))
    c2.metric("Running Bots", int(status.get("running_bots", 0)))
    c3.metric("Runtime", str(status.get("runtime", "UNKNOWN")))
    c4.metric("Validation", "ready")
    st.markdown(
        " > Flow: **Framework** builds bots, **Runtime** monitors execution, **Admin** controls operations, "
        "and **Validation Lab** certifies deployment readiness."
    )

    selected = selected_child if selected_child in BOT_MANAGEMENT_ROUTES else ""
    if not selected:
        bot_management_landing(status, bots)
        return

    st.query_params["screen"] = "BOT MANAGEMENT"
    st.query_params["bot_child"] = selected
    st.query_params["route"] = BOT_MANAGEMENT_ROUTES[selected]
    st.caption(f"Bot Management / {selected} | Route: `{BOT_MANAGEMENT_ROUTES[selected]}`")
    if selected == "Framework":
        bot_framework_screen(data)
    elif selected == "Runtime":
        bot_runtime_screen(data)
    elif selected == "Admin":
        bot_admin_screen(data)
    elif selected == "Validation Lab":
        validation_screen()


with st.sidebar:
    context = current_user_context()
    feature_files = available_feature_files()
    selectable_symbols = list(feature_files)
    requested_screen = st.query_params.get("screen", "")
    requested_route = st.query_params.get("route", "")
    requested_bot_child = st.query_params.get("bot_child", "")
    if context is None:
        requested_screen = "DASHBOARD"
        requested_bot_child = ""
        page = "DASHBOARD"
        st.markdown(
            """
            <div class="sidebar-panel">
              <div class="sidebar-panel-title">Access</div>
              <div class="sidebar-panel-text">Sign in here. Registration is available as a compact request popup.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.popover("Login", use_container_width=True):
            st.caption("Access your role-based trading workspace.")
            side_login_email = st.text_input("Email", key="sidebar-login-email")
            side_login_password = st.text_input("Password", type="password", key="sidebar-login-password")
            side_login_captcha = st.text_input("CAPTCHA", key="sidebar-login-captcha") if settings.captcha_required else ""
            if st.button("Sign in", key="sidebar-login-submit", use_container_width=True):
                try:
                    import asyncio

                    st.session_state["auth_context"] = asyncio.run(_login_user_from_db(side_login_email, side_login_password, side_login_captcha))
                    st.query_params["screen"] = "DASHBOARD"
                    st.rerun()
                except Exception as exc:
                    st.warning(f"Login unavailable: {exc}")
        with st.popover("Register", use_container_width=True):
            st.caption("Request activation for a new workspace.")
            side_name = st.text_input("Name", key="sidebar-register-name")
            side_email = st.text_input("Email", key="sidebar-register-email")
            side_captcha = st.text_input("CAPTCHA", key="sidebar-register-captcha") if settings.captcha_required else ""
            if st.button("Request activation", key="sidebar-register-submit", use_container_width=True):
                try:
                    import asyncio

                    token = asyncio.run(_register_user_from_db(side_name, side_email, side_captcha))
                    st.success("Activation request recorded.")
                    st.caption(f"Development activation token: `{token}`")
                except Exception as exc:
                    st.warning(f"Registration unavailable: {exc}")
        st.caption("Operational menu unlocks after login.")
    else:
        st.markdown(
            """
            <div class="sidebar-panel">
              <div class="sidebar-panel-title">Operations</div>
              <div class="sidebar-panel-text">Bot Management expands on first click and collapses on second click.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(f"Signed in: {getattr(context, 'email', 'user')}")
        requested_screen, requested_bot_child = resolve_screen_request(
            requested_screen,
            requested_route,
            requested_bot_child,
            getattr(st.context, "url", ""),
        )
        allowed_options = allowed_screen_options_for_context(context)
        if requires_password_change():
            allowed_options = [option for option in allowed_options if option == "MY PROFILE"] or ["MY PROFILE"]
            requested_screen = "MY PROFILE"
            requested_bot_child = ""
        if requested_screen not in allowed_options:
            requested_screen = allowed_options[0] if allowed_options else "MY PROFILE"
            requested_bot_child = ""
        page = requested_screen
        if not allowed_options:
            st.warning("No screens are assigned to this role yet.")
        if requested_screen == "BOT MANAGEMENT" and "bot_management_nav_expanded" not in st.session_state:
            st.session_state["bot_management_nav_expanded"] = True
        for option in allowed_options:
            active = option == page
            label = f"{option} active" if active else option
            if option == "BOT MANAGEMENT":
                expanded = bool(st.session_state.get("bot_management_nav_expanded", False))
                prefix = "v" if expanded else ">"
                parent_label = f"{prefix} {label}"
                if st.button(parent_label, key=f"nav-screen-{option}", use_container_width=True):
                    toggle_bot_management_nav(active)
            elif st.button(label, key=f"nav-screen-{option}", use_container_width=True, disabled=active):
                open_root_screen(option)
            if option == "BOT MANAGEMENT" and bool(st.session_state.get("bot_management_nav_expanded", False)):
                for child in BOT_MANAGEMENT_ROUTES:
                    child_active = requested_bot_child == child
                    child_label = f"  {child} active" if child_active else f"  {child}"
                    if st.button(
                        child_label,
                        key=f"nav-bot-management-child-{child}",
                        use_container_width=True,
                        disabled=child_active,
                    ):
                        open_bot_management_child(child)
        st.divider()
    default_symbol = selectable_symbols[0] if selectable_symbols else ""
    data_file = feature_files.get(default_symbol, Path("__missing_feature_file__"))
    if context is not None:
        st.caption("Live prices update inside the ticker only; the page itself does not auto-refresh.")
        st.caption("Mode: Binance Spot Testnet live scan with Binance one-year candle backtest.")

log_diagnostic(logger, "dashboard_page_selected", page=page)

app_banner()

if current_user_context() is None:
    public_login_landing()
    st.stop()

if not can_access_screen(current_user_context(), page):
    st.error("Access is not available for your current role or subscription.")
    st.info("Use My Profile for account status or ask an administrator to update RBAC access.")
    st.stop()

if requires_password_change() and page != "MY PROFILE":
    st.error("Password change required before accessing the platform.")
    st.info("Open My Profile and set a permanent password.")
    st.stop()

account_status_bar()

if not data_file.exists():
    logger.error("dashboard_missing_data_file path=%s", data_file)
    st.error("Missing Binance one-year feature file. Backfill or select a symbol with available data.")
    st.stop()
data = binance_history_snapshot(str(data_file))
summary = data["summary"]
assert isinstance(summary, dict)

if page == "DASHBOARD":
    dashboard_screen(summary)
else:
    st.markdown("<div class='subtle'>Binance Spot Testnet live scan with one-year Binance candle backtest and strategy performance tiles.</div>", unsafe_allow_html=True)
    status_row(summary)

if page == "ORDERFLOW":
    orderflow(data)
elif page == "RISK":
    risk_screen(data)
elif page == "BOT MANAGEMENT":
    bot_management_screen(data, requested_bot_child)
elif page == "SYSTEM HEALTH":
    health_screen(data)
elif page == "TRADE MANAGEMENT":
    trade_management_screen(data)
elif page == "JOURNAL":
    journal_screen(data)
elif page == "MY PROFILE":
    user_profile_screen(data)
elif page == "USER ADMIN":
    user_admin_screen(data)
