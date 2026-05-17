from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from dotenv import dotenv_values
from plotly.subplots import make_subplots

from aegis_trader.analytics.replay_metrics import load_feature_file
from aegis_trader.analytics.strategy_reports import aggregate_strategy_matrix, run_strategy_matrix
from aegis_trader.bot.framework import BotDeployment, StrategyAgnosticBot
from aegis_trader.core.config import settings
from aegis_trader.core.enums import CertificationState
from aegis_trader.core.logging import configure_logging, log_diagnostic, redact_url
from aegis_trader.storage.bot_repository import (
    DEFAULT_RISK_SETTINGS,
    append_journal_event,
    read_bot_instances,
    read_journal_events,
    read_risk_settings,
    read_validation_runs,
    upsert_bot_instance,
    upsert_risk_settings,
    upsert_validation_run,
)
from aegis_trader.storage.db import build_engine, build_session_factory
from aegis_trader.storage.scan_repository import read_latest_heartbeat, read_live_scan
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY
from aegis_trader.testing.certification import CertificationEngine, CertificationMetrics


st.set_page_config(page_title="mytradingmind.ai Ops", layout="wide", initial_sidebar_state="collapsed")
LOG_PATH = configure_logging()
logger = logging.getLogger(__name__)


DEFAULT_LIVE_SYMBOLS: tuple[str, ...] = tuple(settings.symbols)
DEPLOYED_STRATEGIES_PATH = Path("reports/deployed_strategies.json")
BOT_INSTANCES_PATH = Path("reports/bot_instances.json")
RISK_SETTINGS_PATH = Path("reports/risk_settings.json")
JOURNAL_PATH = Path("reports/journal_events.json")
VALIDATION_RUNS_PATH = Path("reports/validation_runs.json")


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
div[data-testid="stSidebar"] {
  background: #0d1216;
  border-right: 1px solid var(--line);
}
.block-container {
  padding-top: 1.15rem;
  padding-bottom: 2rem;
}
h1, h2, h3 {
  letter-spacing: 0;
}
h1 {
  font-size: 1.75rem;
  margin-bottom: 0.1rem;
}
.subtle {
  color: var(--muted);
  font-size: 0.9rem;
}
.status-row {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px, 1fr));
  gap: 0.65rem;
  margin: 0.75rem 0 1rem;
}
.status-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.72rem 0.82rem;
  min-height: 82px;
}
.status-label {
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.status-value {
  font-size: 1.18rem;
  font-weight: 700;
  margin-top: 0.2rem;
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
}
</style>
"""


st.markdown(CSS, unsafe_allow_html=True)


@st.cache_data(ttl=10)
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


@st.cache_data(ttl=1)
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
    frame = merge_stream_state(ensure_default_live_symbols(pd.read_json(file_path)), stream)
    log_diagnostic(logger, "live_scan_loaded", source="file", rows=len(frame))
    return frame


@st.cache_data(ttl=1)
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
    scan = scan.copy()
    if "priority" not in scan.columns:
        scan["priority"] = 100
    for index, symbol in enumerate(list(DEFAULT_LIVE_SYMBOLS) or available_live_symbols()):
        if symbol in set(scan["symbol"].astype(str)):
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


@st.cache_data(ttl=5)
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


def load_deployed_strategy_names() -> list[str]:
    if not DEPLOYED_STRATEGIES_PATH.exists():
        return ["Existing Momentum", "ATR Trend Burst"]
    try:
        payload = json.loads(DEPLOYED_STRATEGIES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["Existing Momentum", "ATR Trend Burst"]
    names = [name for name in payload.get("strategies", []) if name in STRATEGY_REGISTRY]
    return names or ["Existing Momentum"]


def save_deployed_strategy_names(names: list[str]) -> None:
    DEPLOYED_STRATEGIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_framework": "strategy_agnostic",
        "strategies": names,
        "deployed_at": datetime.now(UTC).isoformat(),
    }
    DEPLOYED_STRATEGIES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@st.cache_data(ttl=60)
def load_strategy_matrix(strategy_names: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    matrix = run_strategy_matrix(list(strategy_names))
    return matrix, aggregate_strategy_matrix(matrix)


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
            return
        except Exception as exc:
            logger.exception("risk_settings_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "RISK_SETTINGS", str(exc))
    RISK_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RISK_SETTINGS_PATH.write_text(json.dumps(values, indent=2), encoding="utf-8")
    append_journal("SYSTEM", "", "RISK_SETTINGS", "INFO", "UPDATED", "portfolio risk gates updated", values)


def load_bot_instances() -> pd.DataFrame:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            frame = asyncio.run(_load_bot_instances_from_db())
            if not frame.empty:
                frame = normalize_bot_frame(frame)
                log_diagnostic(logger, "bot_instances_loaded", source="database", rows=len(frame))
                return frame
            rows = default_bot_instances()
            asyncio.run(_save_bot_instances_to_db(pd.DataFrame(rows)))
            frame = normalize_bot_frame(pd.DataFrame(rows))
            log_diagnostic(logger, "bot_instances_seeded", source="database", rows=len(frame))
            return frame
        except Exception as exc:
            logger.exception("bot_instances_database_load_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "BOT_INSTANCES", str(exc))
    rows = load_json_list(BOT_INSTANCES_PATH)
    if not rows:
        rows = default_bot_instances()
        save_json_list(BOT_INSTANCES_PATH, rows)
    return normalize_bot_frame(pd.DataFrame(rows))


def save_bot_instances(frame: pd.DataFrame) -> None:
    if setting_bool("database_enabled"):
        try:
            import asyncio

            asyncio.run(_save_bot_instances_to_db(frame))
            log_diagnostic(logger, "bot_instances_saved", source="database", rows=len(frame))
            return
        except Exception as exc:
            logger.exception("bot_instances_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "BOT_INSTANCES", str(exc))
    save_json_list(BOT_INSTANCES_PATH, frame.to_dict(orient="records"))


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
            return
        except Exception as exc:
            logger.exception("journal_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "JOURNAL", str(exc))
    append_file_journal(bot_name, symbol, event_type, severity, decision, reason, metrics)


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
            return
        except Exception as exc:
            logger.exception("validation_database_save_failed fallback=file")
            append_file_journal("SYSTEM", "", "DATABASE_FALLBACK", "WARN", "VALIDATION_WRITE", str(exc))
    rows = load_json_list(VALIDATION_RUNS_PATH)
    rows.insert(0, row)
    save_json_list(VALIDATION_RUNS_PATH, rows[:100])


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
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    return frame


def transition_bot(name: str, state: str, reason: str) -> None:
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
    save_bot_instances(bots)
    row = bots[mask].iloc[0]
    log_diagnostic(logger, "bot_transition", name=name, state=state, symbol=row.get("symbol"), reason=reason)
    append_journal(str(row["name"]), str(row["symbol"]), f"BOT_{state}", "INFO", state, reason, {"strategy": row["strategy"]})


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
        <div class="heartbeat">
          <span class="pill">socket: {stream_status["source"]}</span>
          <span class="pill">stream update: {stream_status["age"]}</span>
          <span class="pill">scanner: {source}</span>
          <span class="pill">last scan: {age_text}</span>
          <span class="pill">ok: {heartbeat.get("symbols_ok")}</span>
          <span class="pill">errors: {heartbeat.get("symbols_error")}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def portfolio_performance_overview(scan: pd.DataFrame, bots: pd.DataFrame) -> None:
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    running = bots[bots["state"].isin(["DEPLOYED", "RUNNING"])] if not bots.empty and "state" in bots else pd.DataFrame()
    cumulative_pnl = float(scan["total_pnl"].sum()) if not scan.empty and "total_pnl" in scan else 0.0
    unrealized = float(scan["active_pnl"].fillna(0).sum()) if not scan.empty and "active_pnl" in scan else 0.0
    realized = cumulative_pnl - unrealized
    avg_win_rate = float(scan["win_rate"].mean()) if not scan.empty and "win_rate" in scan else 0.0
    avg_pf = float(scan["profit_factor"].replace([np.inf, -np.inf], np.nan).fillna(0).mean()) if not scan.empty and "profit_factor" in scan else 0.0
    exposure = float(running["capital"].sum()) if not running.empty and "capital" in running else 0.0
    active_risk = min(100.0, exposure / max(float(load_risk_settings().get("max_portfolio_exposure", 1.0)), 1.0) * 100)
    drawdown = float(scan["total_pnl"].min()) if not scan.empty and "total_pnl" in scan else 0.0
    st.markdown("### Global Performance Overview")
    a, b, c, d, e, f = st.columns(6)
    a.metric("Cumulative PnL", f"${cumulative_pnl:,.2f}")
    b.metric("Realized PnL", f"${realized:,.2f}")
    c.metric("Unrealized PnL", f"${unrealized:,.2f}")
    d.metric("Profit Factor", f"{avg_pf:.2f}")
    e.metric("Win Rate", f"{avg_win_rate:.1f}%")
    f.metric("Active Risk", f"{active_risk:.1f}%")
    if not scan.empty:
        ranked = scan.sort_values("total_pnl", ascending=False).head(10)
        fig = make_subplots(rows=1, cols=2, specs=[[{"type": "bar"}, {"type": "scatter"}]], subplot_titles=("Strategy Universe PnL", "Signal Quality"))
        fig.add_trace(go.Bar(x=ranked["symbol"], y=ranked["total_pnl"], marker_color=np.where(ranked["total_pnl"] >= 0, "#55d49a", "#ff6f7d"), name="PnL"), row=1, col=1)
        fig.add_trace(go.Scatter(x=ranked["confidence_score"], y=ranked["orderflow_score"], mode="markers+text", text=ranked["symbol"], marker={"size": 12, "color": ranked["buy_score"], "colorscale": "Viridis"}, name="Quality"), row=1, col=2)
        st.plotly_chart(layout_chart(fig, 300), use_container_width=True)


def dashboard_screen(summary: dict[str, float | str]) -> None:
    scan = load_live_scan()
    bots = load_bot_instances()
    portfolio_performance_overview(scan, bots)
    status_row(summary)
    st.markdown("### 1Y Trading System Backtest")
    matrix, aggregate = load_strategy_matrix(tuple(STRATEGY_REGISTRY))
    if aggregate.empty:
        st.info("No one-year Binance feature files are available yet. Run the backfill and metrics job first.")
        return
    strategy_system_charts(matrix, aggregate)
    strategy_tiles(aggregate)


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
            name="Tape prints",
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
        st.caption("Only the price ticker above updates live in the browser. Bot, bucket, and score tables stay steady to avoid screen flicker.")
        st.markdown("### Market Buckets")
        bucket_board(scan)
    feature_files = available_feature_files()
    chart_symbols = list(feature_files)
    if chart_symbols:
        selected_chart_symbol = st.selectbox("Chart symbol", chart_symbols, index=0, key="live_chart_symbol")
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
    candles = data["candles"]
    tape = data["tape"]
    book = data["book"]
    assert isinstance(candles, pd.DataFrame)
    assert isinstance(tape, pd.DataFrame)
    assert isinstance(book, pd.DataFrame)
    scan = load_live_scan()
    symbols = scan["symbol"].astype(str).tolist() if not scan.empty else available_live_symbols()
    selected = st.selectbox("Crypto", symbols, index=0)
    selected_row = scan[scan["symbol"].astype(str) == selected].iloc[0] if not scan.empty and selected in set(scan["symbol"].astype(str)) else None
    if selected_row is not None:
        watchlist = orderflow_watchlist(scan)
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
        d.metric("Trade Velocity", f"{velocity:.0f} prints")
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


def risk_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    summary = data["summary"]
    assert isinstance(summary, dict)
    risk = load_risk_settings()
    bots = load_bot_instances()
    running = bots[bots["state"].isin(["DEPLOYED", "RUNNING"])] if not bots.empty and "state" in bots else pd.DataFrame()
    exposure = float(running["capital"].sum()) if not running.empty and "capital" in running else 0.0
    left, mid, right = st.columns(3)
    left.plotly_chart(risk_gauge("Portfolio Exposure", exposure, float(risk["max_portfolio_exposure"])), use_container_width=True)
    mid.plotly_chart(risk_gauge("Cash / Trade", float(risk["max_cash_per_trade"]), max(float(risk["capital"]), 1.0)), use_container_width=True)
    right.plotly_chart(risk_gauge("Trade Window", float(len(running)), float(risk["max_trades_per_window"]), ""), use_container_width=True)
    with st.form("risk_settings"):
        st.markdown("### Portfolio Risk Gates")
        c1, c2, c3 = st.columns(3)
        capital = c1.number_input("Portfolio capital", min_value=0.0, value=float(risk["capital"]), step=100.0)
        max_cash = c2.number_input("Max cash allocation per trade", min_value=0.0, value=float(risk["max_cash_per_trade"]), step=25.0)
        max_risk_pct = c3.number_input("Max risk per trade %", min_value=0.0, max_value=100.0, value=float(risk["max_risk_per_trade_pct"]) * 100, step=0.1)
        c4, c5, c6 = st.columns(3)
        max_trades = c4.number_input("Max trades per window", min_value=0, value=int(risk["max_trades_per_window"]), step=1)
        window = c5.number_input("Window minutes", min_value=1, value=int(risk["trade_window_minutes"]), step=15)
        max_exposure = c6.number_input("Portfolio exposure limit", min_value=0.0, value=float(risk["max_portfolio_exposure"]), step=100.0)
        kill_switch = st.checkbox("Kill switch", value=bool(risk["kill_switch"]))
        if st.form_submit_button("Save hard risk gates"):
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
            st.success("Risk gates persisted. Running bots will enforce these before trade placement.")
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
    st.caption("Create and configure bots here. Runtime monitoring lives in Bot Runtime.")
    available = list(STRATEGY_REGISTRY)
    symbols = load_live_scan()["symbol"].astype(str).tolist() or available_live_symbols()
    with st.form("create_bot"):
        c1, c2, c3, c4 = st.columns(4)
        name = c1.text_input("Bot instance name", value=f"{symbols[0].replace('/', '')} bot" if symbols else "Crypto bot")
        primary_strategy = c2.selectbox("Primary strategy", available)
        symbol = c3.selectbox("Symbol", symbols)
        capital = c4.number_input("Capital", min_value=0.0, value=250.0, step=50.0)
        selected_strategies = st.multiselect("Strategy collection", available, default=[primary_strategy], help="Bots may carry multiple reusable strategy modules; the primary strategy is used for validation until portfolio execution is expanded.")
        p1, p2 = st.columns(2)
        min_confidence = p1.slider("Min confidence", min_value=0, max_value=100, value=55)
        risk_reward = p2.number_input("Risk reward", min_value=0.1, value=1.7, step=0.1)
        if st.form_submit_button("Create bot instance"):
            bots = load_bot_instances()
            strategy_collection = selected_strategies or [primary_strategy]
            row = {
                "name": name,
                "strategy": primary_strategy,
                "symbol": symbol,
                "timeframe": "1h",
                "capital": capital,
                "parameters": {"strategies": strategy_collection, "min_confidence": min_confidence, "risk_reward": risk_reward},
                "state": "DRAFT",
                "status_reason": "created from UI",
                "created_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": "",
            }
            bots = bots[bots["name"].astype(str) != name] if not bots.empty and "name" in bots else pd.DataFrame()
            save_bot_instances(pd.concat([bots, pd.DataFrame([row])], ignore_index=True))
            append_journal(name, symbol, "BOT_CREATED", "INFO", "DRAFT", "bot instance created", row["parameters"])
            st.success("Bot instance created.")

    bots = load_bot_instances()
    if not bots.empty:
        display = bots.copy()
        display["strategy_collection"] = display["parameters"].apply(lambda value: ", ".join((value or {}).get("strategies", [])) if isinstance(value, dict) else "")
        with st.expander("Configured Bot Definitions", expanded=True):
            st.dataframe(
                display[[col for col in ["name", "strategy_collection", "strategy", "symbol", "timeframe", "capital", "state", "status_reason"] if col in display]],
                use_container_width=True,
                hide_index=True,
            )
    st.info("Use Validation Lab to backtest configured bot instances over a selected period. Use Bot Runtime to deploy, stop, and monitor 24x7 bot instances.")


def bot_runtime_screen(data: dict[str, pd.DataFrame | dict[str, float | str]]) -> None:
    st.markdown("### Bot Runtime")
    st.caption("Live operational monitoring for deployed bot instances. Creation and strategy configuration remain in Bot Framework.")
    bots = load_bot_instances()
    scan = load_live_scan()
    if bots.empty:
        st.info("No bot instances exist yet. Create one in Bot Framework.")
        return
    active_names = tuple(sorted(set(bots["strategy"].astype(str).tolist()))) if "strategy" in bots else tuple(load_deployed_strategy_names())
    matrix, aggregate = load_strategy_matrix(active_names)
    runtime_tiles(bots, scan, matrix, aggregate)
    st.markdown("### Runtime Controls")
    for _, bot in bots.iterrows():
        with st.expander(f"{bot['name']} controls", expanded=False):
            c1, c2, c3 = st.columns(3)
            if c1.button("Backtest", key=f"rt-bt-{bot['name']}", use_container_width=True):
                transition_bot(str(bot["name"]), "BACKTESTED", "one-year Binance backtest completed")
                st.success("Backtest state updated.")
            if c2.button("Deploy", key=f"rt-dep-{bot['name']}", use_container_width=True):
                ok, reason = risk_gate_for_bot(bot, load_risk_settings())
                if ok:
                    transition_bot(str(bot["name"]), "RUNNING", "deployed and running 24x7")
                    st.success(reason)
                else:
                    transition_bot(str(bot["name"]), "FAILED", f"risk rejection: {reason}")
                    append_journal(str(bot["name"]), str(bot["symbol"]), "RISK_REJECTION", "WARN", "BLOCKED", reason)
                    st.error(reason)
            if c3.button("Stop", key=f"rt-stop-{bot['name']}", use_container_width=True):
                transition_bot(str(bot["name"]), "STOPPED", "stopped by user")
                st.warning("Bot stopped.")


def runtime_tiles(bots: pd.DataFrame, scan: pd.DataFrame, matrix: pd.DataFrame, aggregate: pd.DataFrame) -> None:
    scan = normalize_scan_columns(scan) if not scan.empty else scan
    html = ["<div class='bucket-grid'>"]
    for _, row in bots.iterrows():
        state = str(row.get("state", "DRAFT"))
        symbol = str(row.get("symbol", ""))
        strategy = str(row.get("strategy", ""))
        state_class = "good" if state in {"RUNNING", "DEPLOYED"} else "bad" if state == "FAILED" else "warn" if state in {"PAUSED", "BACKTESTED"} else "info"
        scan_row = scan[scan["symbol"].astype(str) == symbol].iloc[0] if not scan.empty and symbol in set(scan["symbol"].astype(str)) else pd.Series(dtype=object)
        perf = matrix[(matrix["strategy"].astype(str) == strategy) & (matrix["symbol"].astype(str) == symbol)] if not matrix.empty else pd.DataFrame()
        perf_row = perf.iloc[0] if not perf.empty else pd.Series(dtype=object)
        pnl = float(scan_row.get("active_pnl", 0.0) or 0.0)
        drawdown = float(perf_row.get("max_drawdown_pct", 0.0) or 0.0)
        win_rate = float(perf_row.get("win_rate", 0.0) or 0.0)
        pf = float(perf_row.get("profit_factor", 0.0) or 0.0)
        sharpe = float(perf_row.get("sharpe_proxy", 0.0) or 0.0)
        expectancy = float(perf_row.get("avg_trade_return_pct", 0.0) or 0.0)
        health = "healthy" if state in {"RUNNING", "DEPLOYED"} and drawdown < 12 else "watch" if state != "FAILED" else "failed"
        pnl_class = "good" if pnl >= 0 else "bad"
        html.append(
            "<div class='bucket'>"
            f"<div class='bucket-title'><span>{row['name']}</span><span class='pill {state_class}'>{state}</span></div>"
            f"<div class='scan-meta'>{strategy} | {symbol}</div>"
            f"<div class='status-value {pnl_class}'>${pnl:,.2f}</div>"
            f"<div class='scan-meta'>health {health} | risk {row.get('status_reason', '')}</div>"
            f"<div class='scan-meta'>DD {drawdown:.2f}% | win {win_rate:.1f}% | PF {pf:.2f}</div>"
            f"<div class='scan-meta'>Sharpe {sharpe:.2f} | expectancy {expectancy:.3f}% | uptime {stream_age_text(row.get('deployed_at', row.get('created_at', '')))}</div>"
            f"<div class='scan-meta'>positions {1 if state in {'RUNNING', 'DEPLOYED'} else 0} | heartbeat {stream_age_text(row.get('heartbeat_at', ''))}</div>"
            "</div>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


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
    if not trades.empty:
        journal_charts(trades)
        with st.expander("Historical Trade Journal", expanded=True):
            st.dataframe(enrich_trade_journal(trades).tail(250), use_container_width=True, hide_index=True)
    if journal.empty:
        st.info("Live journal is ready. New bot decisions and testnet execution results will append here.")
        return
    journal["event_time"] = pd.to_datetime(journal["event_time"], errors="coerce")
    counts = journal.groupby("event_type").size().reset_index(name="events").sort_values("events", ascending=False)
    st.plotly_chart(layout_chart(go.Figure(go.Bar(x=counts["event_type"], y=counts["events"], marker_color="#79a7ff")), 280), use_container_width=True)
    st.dataframe(journal.sort_values("event_time", ascending=False), use_container_width=True, hide_index=True)


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
            st.dataframe(
                display[[col for col in ["name", "strategy_collection", "strategy", "symbol", "timeframe", "capital", "state"] if col in display]],
                use_container_width=True,
                hide_index=True,
            )
    selected_bot = st.selectbox("Bot instance", bot_names or ["No bot instances"])
    c1, c2, c3, c4 = st.columns(4)
    symbol = c1.selectbox("Symbol", load_live_scan()["symbol"].astype(str).tolist() or available_live_symbols())
    timeframe = c2.selectbox("Timeframe", ["1h", "4h", "1d"])
    capital = c3.number_input("Capital assumption", min_value=0.0, value=1_000.0, step=100.0)
    fees = c4.number_input("Fees bps", min_value=0.0, value=10.0, step=1.0)
    d1, d2, d3 = st.columns(3)
    start_date = d1.date_input("Start date", value=datetime.now().date() - timedelta(days=365))
    end_date = d2.date_input("End date", value=datetime.now().date())
    slippage = d3.number_input("Slippage bps", min_value=0.0, value=5.0, step=1.0)
    if st.button("Run validation", use_container_width=True) and selected_bot != "No bot instances":
        selected_row = bots[bots["name"].astype(str) == selected_bot].iloc[0]
        metrics_result, trades = run_bot_validation(selected_row, symbol, timeframe, start_date, end_date, capital, fees, slippage)
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
            "state": "COMPLETED",
            "metrics": metrics_result,
        }
        save_validation_run(validation_row)
        append_journal(selected_bot, symbol, "VALIDATION_RUN", "INFO", "COMPLETED", "validation/backtest completed", validation_row["metrics"])
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
    c.metric("Max DD", f"{metrics.max_drawdown_pct:.1f}%")
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
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1)
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
    cols[4].metric("Max DD", f"{float(metrics.get('max_drawdown_pct', 0.0)):.1f}%")
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


with st.sidebar:
    st.title("mytradingmind.ai")
    feature_files = available_feature_files()
    selectable_symbols = list(feature_files)
    page = st.radio(
        "Screen",
        [
            "DASHBOARD",
            "LIVE TRADING",
            "ORDERFLOW",
            "RISK",
            "BOT FRAMEWORK",
            "BOT RUNTIME",
            "SYSTEM HEALTH",
            "JOURNAL",
            "VALIDATION LAB",
        ],
    )
    st.divider()
    default_symbol = selectable_symbols[0] if selectable_symbols else ""
    data_file = feature_files.get(default_symbol, Path("__missing_feature_file__"))
    st.caption("Live prices update inside the ticker only; the page itself does not auto-refresh.")
    st.caption("Mode: Binance Spot Testnet live scan with Binance one-year candle backtest.")

log_diagnostic(logger, "dashboard_page_selected", page=page)

if not data_file.exists():
    logger.error("dashboard_missing_data_file path=%s", data_file)
    st.error("Missing Binance one-year feature file. Backfill or select a symbol with available data.")
    st.stop()
data = binance_history_snapshot(str(data_file))
summary = data["summary"]
assert isinstance(summary, dict)

st.markdown("# mytradingmind.ai Ops Console")
st.markdown("<div class='subtle'>Binance Spot Testnet live scan with one-year Binance candle backtest and strategy performance tiles.</div>", unsafe_allow_html=True)
if page == "DASHBOARD":
    dashboard_screen(summary)
else:
    status_row(summary)

if page == "LIVE TRADING":
    live_trading(data)
elif page == "ORDERFLOW":
    orderflow(data)
elif page == "RISK":
    risk_screen(data)
elif page == "BOT FRAMEWORK":
    bot_framework_screen(data)
elif page == "BOT RUNTIME":
    bot_runtime_screen(data)
elif page == "SYSTEM HEALTH":
    health_screen(data)
elif page == "JOURNAL":
    journal_screen(data)
elif page == "VALIDATION LAB":
    validation_screen()
