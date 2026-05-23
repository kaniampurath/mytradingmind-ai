from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Literal

import pandas as pd

logger = logging.getLogger(__name__)

SignalName = Literal["BUY", "HOLD", "SELL_STOP", "PARTIAL_SELL_TP1", "PARTIAL_SELL_TP2", "PARTIAL_SELL_TP3"]

CRITICAL_VALUES = {
    "90%": 1.645,
    "95%": 1.96,
    "99%": 2.58,
}


@dataclass(frozen=True)
class AtrMeanReversionConfig:
    timeframe: str
    atr_stop_length: int = 14
    atr_multiplier: float = 2.0
    atr_fast_length: int = 5
    atr_slow_length: int = 50
    drift_length: int = 14
    confidence_interval: str = "95%"
    min_candles: int = 60

    @property
    def critical_value(self) -> float:
        return CRITICAL_VALUES.get(self.confidence_interval, 1.96)


@dataclass
class AtrMeanReversionState:
    symbol: str
    timeframe: str
    position_size: float = 0.0
    entry_signal_triggered: bool = False
    initial_entry_price: float = 0.0
    risk_amount: float = 0.0
    initial_order_size: float = 0.0
    stop_loss: float = 0.0
    tp_taken_count: int = 0

    def reset_trade(self) -> None:
        self.position_size = 0.0
        self.entry_signal_triggered = False
        self.initial_entry_price = 0.0
        self.risk_amount = 0.0
        self.initial_order_size = 0.0
        self.stop_loss = 0.0
        self.tp_taken_count = 0


@dataclass(frozen=True)
class AtrMeanReversionSignal:
    symbol: str
    timeframe: str
    close: float
    atr_14: float
    atr_fast_5: float
    atr_slow_50: float
    test_stat: float
    reject_H0: bool
    drift: float
    drift_confirmation: bool
    entry_signal: bool
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    signal: SignalName

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def default_atr_mean_reversion_config(timeframe: str) -> AtrMeanReversionConfig:
    normalized = timeframe.lower()
    if normalized == "10m":
        return AtrMeanReversionConfig(timeframe="10m", atr_multiplier=1.5, drift_length=14, confidence_interval="90%")
    return AtrMeanReversionConfig(timeframe="1h", atr_multiplier=2.0, drift_length=14, confidence_interval="95%")


def calculate_atr_mean_reversion_indicators(
    df: pd.DataFrame,
    timeframe: str,
    config: AtrMeanReversionConfig | None = None,
) -> pd.DataFrame:
    cfg = config or default_atr_mean_reversion_config(timeframe)
    rows = df.copy().reset_index(drop=True)
    if "is_complete" in rows:
        rows = rows[rows["is_complete"].astype(bool)].reset_index(drop=True)

    high = pd.to_numeric(rows["high"], errors="coerce")
    low = pd.to_numeric(rows["low"], errors="coerce")
    close = pd.to_numeric(rows["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)

    atr_stop = true_range.rolling(cfg.atr_stop_length, min_periods=cfg.atr_stop_length).mean()
    atr_fast = true_range.rolling(cfg.atr_fast_length, min_periods=cfg.atr_fast_length).mean()
    atr_slow = true_range.rolling(cfg.atr_slow_length, min_periods=cfg.atr_slow_length).mean()
    std_error = true_range.rolling(cfg.atr_fast_length, min_periods=cfg.atr_fast_length).std(ddof=1) / math.sqrt(cfg.atr_fast_length)
    test_stat = (atr_fast - atr_slow) / std_error.replace(0, float("nan"))

    ratio = close / previous_close
    log_change = ratio.apply(lambda value: math.log(value) if pd.notna(value) and value > 0 else float("nan")).astype("float64")
    drift_mean = log_change.rolling(cfg.drift_length, min_periods=cfg.drift_length).mean()
    drift_std = log_change.rolling(cfg.drift_length, min_periods=cfg.drift_length).std(ddof=1)
    drift = drift_mean - (0.5 * drift_std.pow(2))

    rows["timeframe"] = cfg.timeframe
    rows["true_range"] = true_range
    rows["atr_14"] = atr_stop
    rows["atr_fast_5"] = atr_fast
    rows["atr_slow_50"] = atr_slow
    rows["test_stat"] = test_stat.astype("float64")
    rows["reject_H0"] = rows["test_stat"].abs() > cfg.critical_value
    rows["drift"] = drift.astype("float64")
    rows["drift_confirmation"] = rows["drift"] > rows["drift"].shift(1)
    rows["stop_loss_seed"] = low - (rows["atr_14"] * cfg.atr_multiplier)
    rows["risk_amount"] = rows["atr_14"] * cfg.atr_multiplier
    return rows


def generate_timeframe_signal(
    indicators: pd.DataFrame | pd.Series,
    position_state: AtrMeanReversionState,
    config: AtrMeanReversionConfig | None = None,
) -> AtrMeanReversionSignal:
    row = indicators.iloc[-1] if isinstance(indicators, pd.DataFrame) else indicators
    cfg = config or default_atr_mean_reversion_config(position_state.timeframe)
    signal: SignalName = "HOLD"
    entry_signal = False
    close = _finite(row.get("close"))
    low = _finite(row.get("low"))
    atr_14 = _finite(row.get("atr_14"))
    risk_amount = _finite(row.get("risk_amount"), atr_14 * cfg.atr_multiplier)
    stop_seed = _finite(row.get("stop_loss_seed"), low - risk_amount)
    current_stop = max(position_state.stop_loss, stop_seed) if position_state.position_size > 0 else stop_seed
    tp1, tp2, tp3 = _tp_levels(position_state.initial_entry_price, position_state.risk_amount)

    if len(indicators) < cfg.min_candles if isinstance(indicators, pd.DataFrame) else pd.isna(row.get("atr_slow_50")):
        return _signal_from_row(row, position_state, current_stop, tp1, tp2, tp3, False, "HOLD")

    reject_h0 = bool(row.get("reject_H0", False))
    drift_confirmation = bool(row.get("drift_confirmation", False))

    if position_state.position_size <= 0:
        entry_signal = (position_state.entry_signal_triggered or reject_h0) and drift_confirmation
        signal = "BUY" if entry_signal else "HOLD"
        if entry_signal:
            current_stop = stop_seed
            tp1, tp2, tp3 = _tp_levels(close, risk_amount)
    else:
        tp1, tp2, tp3 = _tp_levels(position_state.initial_entry_price, position_state.risk_amount)
        if close <= current_stop:
            signal = "SELL_STOP"
        elif position_state.tp_taken_count == 0 and close >= tp1:
            signal = "PARTIAL_SELL_TP1"
        elif position_state.tp_taken_count == 1 and close >= tp2:
            signal = "PARTIAL_SELL_TP2"
        elif position_state.tp_taken_count == 2 and close >= tp3:
            signal = "PARTIAL_SELL_TP3"

    result = _signal_from_row(row, position_state, current_stop, tp1, tp2, tp3, entry_signal, signal)
    if signal != "HOLD":
        logger.info("atr_mean_reversion_signal", extra={"symbol": result.symbol, "timeframe": result.timeframe, "signal": result.signal, "close": result.close})
    return result


def update_position_state(
    signal: AtrMeanReversionSignal,
    position_state: AtrMeanReversionState,
    order_size: float = 1.0,
) -> AtrMeanReversionState:
    if signal.signal == "BUY":
        position_state.position_size = order_size
        position_state.entry_signal_triggered = False
        position_state.initial_entry_price = signal.close
        position_state.risk_amount = max(0.0, signal.close - signal.stop_loss)
        position_state.initial_order_size = order_size
        position_state.stop_loss = signal.stop_loss
        position_state.tp_taken_count = 0
        return position_state

    if signal.signal == "HOLD":
        if signal.reject_H0 and position_state.position_size <= 0:
            position_state.entry_signal_triggered = True
        if position_state.position_size > 0:
            position_state.stop_loss = max(position_state.stop_loss, signal.stop_loss)
        return position_state

    if signal.signal == "SELL_STOP":
        position_state.reset_trade()
        return position_state

    if signal.signal == "PARTIAL_SELL_TP1":
        position_state.position_size = max(0.0, position_state.position_size - (position_state.initial_order_size / 3.0))
        position_state.tp_taken_count = 1
    elif signal.signal == "PARTIAL_SELL_TP2":
        position_state.position_size = max(0.0, position_state.position_size - (position_state.initial_order_size / 3.0))
        position_state.tp_taken_count = 2
    elif signal.signal == "PARTIAL_SELL_TP3":
        position_state.reset_trade()
        return position_state

    position_state.stop_loss = max(position_state.stop_loss, signal.stop_loss)
    if position_state.position_size <= 0:
        position_state.reset_trade()
    return position_state


def combine_1h_10m_signals(
    signal_1h: AtrMeanReversionSignal,
    signal_10m: AtrMeanReversionSignal,
    mode: str = "confirmation",
) -> dict[str, object]:
    sell_signal = _sell_priority(signal_1h, signal_10m)
    if sell_signal:
        return {"signal": sell_signal.signal, "source_timeframe": sell_signal.timeframe, "reason": "sell priority"}

    ten_min_above_stop = signal_10m.close > signal_10m.stop_loss
    buy_allowed = (
        signal_1h.entry_signal
        and signal_1h.drift_confirmation
        and signal_1h.reject_H0
        and ten_min_above_stop
        and (signal_10m.entry_signal or (mode == "confirmation" and signal_10m.drift_confirmation))
    )
    return {
        "signal": "BUY" if buy_allowed else "HOLD",
        "source_timeframe": "multi",
        "reason": "1h trigger plus 10m confirmation" if buy_allowed else "multi-timeframe confirmation incomplete",
    }


def _sell_priority(signal_1h: AtrMeanReversionSignal, signal_10m: AtrMeanReversionSignal) -> AtrMeanReversionSignal | None:
    if signal_10m.signal == "SELL_STOP":
        return signal_10m
    if signal_1h.signal == "SELL_STOP":
        return signal_1h
    for signal in (signal_10m, signal_1h):
        if signal.signal.startswith("PARTIAL_SELL"):
            return signal
    return None


def _signal_from_row(
    row: pd.Series,
    state: AtrMeanReversionState,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    entry_signal: bool,
    signal: SignalName,
) -> AtrMeanReversionSignal:
    return AtrMeanReversionSignal(
        symbol=str(row.get("symbol", state.symbol)).replace("/", ""),
        timeframe=str(row.get("timeframe", state.timeframe)),
        close=_finite(row.get("close")),
        atr_14=_finite(row.get("atr_14")),
        atr_fast_5=_finite(row.get("atr_fast_5")),
        atr_slow_50=_finite(row.get("atr_slow_50")),
        test_stat=_finite(row.get("test_stat")),
        reject_H0=bool(row.get("reject_H0", False)),
        drift=_finite(row.get("drift")),
        drift_confirmation=bool(row.get("drift_confirmation", False)),
        entry_signal=entry_signal,
        stop_loss=_finite(stop_loss),
        tp1=_finite(tp1),
        tp2=_finite(tp2),
        tp3=_finite(tp3),
        signal=signal,
    )


def _tp_levels(entry_price: float, risk_amount: float) -> tuple[float, float, float]:
    if entry_price <= 0 or risk_amount <= 0:
        return 0.0, 0.0, 0.0
    return entry_price + risk_amount, entry_price + (2.0 * risk_amount), entry_price + (3.0 * risk_amount)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default
