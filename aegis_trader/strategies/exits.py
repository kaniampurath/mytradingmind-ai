from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


DAILY_EXIT_PRIORITY: tuple[str, ...] = ("HARD_STOP", "ATR_TRAIL", "TREND_BREAK", "TIME_STOP")


@dataclass(frozen=True)
class DailyExitConfig:
    hard_stop_atr_mult: float = 2.0
    atr_trail_mult: float = 2.0
    trend_column: str = "ema20"
    time_stop_bars: int = 25
    time_stop_min_pnl_pct: float = 3.0


@dataclass(frozen=True)
class DailyExitDecision:
    reason: str
    exit_price: float
    stop_price: float
    mfe_pct: float
    mae_pct: float


@dataclass(frozen=True)
class FiveMinuteAtrBurstParams:
    atr_mult: float
    ema_stop_len: int
    ema_fast_len: int
    tp_rr: float


@dataclass(frozen=True)
class FiveMinuteSellDecision:
    signal_type: str
    reason: str
    exit_price: float | None = None
    partial_qty: float = 0.0
    stop_price: float | None = None
    force_shutdown: bool = False
    bypass_daily_pnl_limits: bool = False
    cancel_protection: bool = False
    replace_protection: bool = False


PARTIAL_PROFIT_USD = 100.0
PARTIAL_TP_FRACTION = 0.50
DEFAULT_MIN_NOTIONAL = 5.0


def five_min_atr_burst_params(symbol: str) -> FiveMinuteAtrBurstParams:
    normalized = symbol.replace("/", "").upper()
    if normalized.startswith("SOL"):
        return FiveMinuteAtrBurstParams(atr_mult=1.5, ema_stop_len=33, ema_fast_len=10, tp_rr=1.0)
    if normalized.startswith("ETH"):
        return FiveMinuteAtrBurstParams(atr_mult=2.0, ema_stop_len=21, ema_fast_len=20, tp_rr=0.0)
    return FiveMinuteAtrBurstParams(atr_mult=1.5, ema_stop_len=33, ema_fast_len=20, tp_rr=1.0 if normalized.startswith("BTC") else 0.0)


def initial_five_min_stop(entry_price: float, atr: float, atr_mult: float) -> float:
    stop = entry_price - (atr_mult * atr)
    if stop >= entry_price:
        stop = entry_price - atr
    return max(0.000001, stop)


def five_min_partial_tp_decision(
    row: pd.Series,
    active_trade: dict[str, Any],
    *,
    tp_rr: float,
    min_notional: float = DEFAULT_MIN_NOTIONAL,
) -> FiveMinuteSellDecision | None:
    if bool(active_trade.get("partial_taken", False)):
        return None
    entry = float(active_trade["entry"])
    high = float(row["high"])
    stop = float(active_trade["stop"])
    remaining = float(active_trade.get("remaining", 1.0))
    notional = float(active_trade.get("notional", 100.0))
    risk = max(0.0, entry - stop)
    rr_hit = tp_rr > 0 and risk > 0 and high >= entry + (tp_rr * risk)
    theoretical_qty = PARTIAL_PROFIT_USD / entry if entry > 0 else 0.0
    usd_profit = (high - entry) * theoretical_qty
    usd_hit = usd_profit >= PARTIAL_PROFIT_USD
    if not (rr_hit or usd_hit):
        return None
    remaining_after = remaining * (1.0 - PARTIAL_TP_FRACTION)
    force_final = (notional * remaining_after) < min_notional
    return FiveMinuteSellDecision(
        signal_type="PARTIAL_TP",
        reason="partial_tp_rr" if rr_hit else "partial_tp_usd",
        exit_price=high,
        partial_qty=PARTIAL_TP_FRACTION,
        stop_price=entry,
        force_shutdown=force_final,
        replace_protection=not force_final,
        cancel_protection=force_final,
    )


def five_min_final_exit_decision(row: pd.Series, active_trade: dict[str, Any]) -> FiveMinuteSellDecision | None:
    close = float(row["close"])
    low = float(row["low"])
    stop = float(active_trade["stop"])
    ema_fast = float(row.get("kcj_ema_fast", row.get("exit_ema_fast", row.get("ema20", close))))
    if low <= stop:
        return FiveMinuteSellDecision("FINAL_EXIT", "ATR_STOP", exit_price=stop, stop_price=stop, cancel_protection=True)
    if close < ema_fast:
        return FiveMinuteSellDecision("FINAL_EXIT", "MA_EXIT", exit_price=close, stop_price=stop, cancel_protection=True)
    return None


def five_min_emergency_exit_decision(row: pd.Series, active_trade: dict[str, Any], previous_completed_atr: float, atr_mult: float) -> FiveMinuteSellDecision | None:
    if bool(row.get("is_complete", True)):
        return None
    open_ = float(row["open"])
    close = float(row["close"])
    low = float(row["low"])
    adverse_move = max(abs(close - open_), abs(low - open_))
    if close < open_ and adverse_move >= atr_mult * float(previous_completed_atr):
        return FiveMinuteSellDecision(
            "EMERGENCY_EXIT",
            "EMERGENCY_ATR_BURST",
            exit_price=close,
            stop_price=float(active_trade["stop"]),
            force_shutdown=True,
            bypass_daily_pnl_limits=True,
            cancel_protection=True,
        )
    return None


def daily_timeframe_exit(row: pd.Series, active_trade: dict[str, Any], config: DailyExitConfig | None = None) -> DailyExitDecision | None:
    """Production swing-trading exit stack for daily strategies.

    The first triggered rule exits the trade using this priority:
    HARD_STOP, ATR_TRAIL, TREND_BREAK, TIME_STOP.
    """
    cfg = config or DailyExitConfig()
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    atr = float(row["atr14"])
    entry = float(active_trade["entry"])

    active_trade["highest_high"] = max(float(active_trade.get("highest_high", entry)), high)
    active_trade["lowest_low"] = min(float(active_trade.get("lowest_low", entry)), low)
    highest_high = float(active_trade["highest_high"])
    lowest_low = float(active_trade["lowest_low"])

    mfe_pct = (highest_high - entry) / entry * 100 if entry > 0 else 0.0
    mae_pct = (lowest_low - entry) / entry * 100 if entry > 0 else 0.0
    pnl_pct = (close - entry) / entry * 100 if entry > 0 else 0.0
    active_trade["mfe_pct"] = mfe_pct
    active_trade["mae_pct"] = mae_pct

    hard_stop = max(0.000001, entry - (cfg.hard_stop_atr_mult * atr))
    trail_stop = max(0.000001, highest_high - (cfg.atr_trail_mult * atr))
    active_trade["stop"] = max(float(active_trade.get("stop", hard_stop)), hard_stop, trail_stop)

    if close <= hard_stop:
        return DailyExitDecision("HARD_STOP", close, hard_stop, mfe_pct, mae_pct)
    if close < trail_stop:
        return DailyExitDecision("ATR_TRAIL", close, trail_stop, mfe_pct, mae_pct)
    if close < float(row[cfg.trend_column]):
        return DailyExitDecision("TREND_BREAK", close, float(row[cfg.trend_column]), mfe_pct, mae_pct)
    if int(active_trade["bars"]) > cfg.time_stop_bars and pnl_pct < cfg.time_stop_min_pnl_pct:
        return DailyExitDecision("TIME_STOP", close, active_trade["stop"], mfe_pct, mae_pct)
    return None
