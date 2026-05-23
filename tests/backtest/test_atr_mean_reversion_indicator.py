from __future__ import annotations

import pandas as pd

from aegis_trader.strategies.atr_mean_reversion import (
    AtrMeanReversionConfig,
    AtrMeanReversionSignal,
    AtrMeanReversionState,
    calculate_atr_mean_reversion_indicators,
    combine_1h_10m_signals,
    default_atr_mean_reversion_config,
    generate_timeframe_signal,
    update_position_state,
)


def test_atr_calculation_and_reject_h0_logic() -> None:
    rows = _rows(80)
    for offset, index in enumerate(range(70, 80), start=1):
        width = 4.0 + offset
        rows.loc[index, "high"] = rows.loc[index, "close"] + width
        rows.loc[index, "low"] = rows.loc[index, "close"] - width
    config = AtrMeanReversionConfig(timeframe="1h", confidence_interval="90%")

    indicators = calculate_atr_mean_reversion_indicators(rows, "1h", config)

    latest = indicators.dropna(subset=["test_stat"]).iloc[-1]
    assert latest["atr_14"] > 0
    assert latest["atr_fast_5"] > latest["atr_slow_50"]
    assert latest["test_stat"] > 0
    assert bool(latest["reject_H0"])


def test_drift_confirmation_uses_completed_candle_history() -> None:
    rows = _rows(80)
    rows["close"] = [100.0 * (1.002 ** index) for index in range(len(rows))]
    rows.loc[78, "close"] = rows.loc[77, "close"] * 1.001
    rows.loc[79, "close"] = rows.loc[78, "close"] * 1.006

    indicators = calculate_atr_mean_reversion_indicators(rows, "1h")

    assert bool(indicators.iloc[-1]["drift_confirmation"])


def test_entry_trigger_is_stateful_until_drift_confirms() -> None:
    state = AtrMeanReversionState("BTCUSDT", "1h")
    not_confirmed = _signal_row(reject=True, drift_confirmation=False, close=100.0)

    hold = generate_timeframe_signal(not_confirmed, state)
    update_position_state(hold, state)

    assert hold.signal == "HOLD"
    assert state.entry_signal_triggered

    confirmed = _signal_row(reject=False, drift_confirmation=True, close=101.0)
    buy = generate_timeframe_signal(confirmed, state)
    update_position_state(buy, state, order_size=3.0)

    assert buy.signal == "BUY"
    assert buy.entry_signal
    assert state.position_size == 3.0
    assert not state.entry_signal_triggered
    assert state.tp_taken_count == 0


def test_tp_sequence_and_state_reset_after_full_exit() -> None:
    state = AtrMeanReversionState("BTCUSDT", "10m", position_size=3.0, initial_entry_price=100.0, risk_amount=5.0, initial_order_size=3.0, stop_loss=90.0)

    tp1 = generate_timeframe_signal(_signal_row(close=105.0, timeframe="10m"), state)
    update_position_state(tp1, state)
    tp2 = generate_timeframe_signal(_signal_row(close=110.0, timeframe="10m"), state)
    update_position_state(tp2, state)
    tp3 = generate_timeframe_signal(_signal_row(close=115.0, timeframe="10m"), state)
    update_position_state(tp3, state)

    assert tp1.signal == "PARTIAL_SELL_TP1"
    assert tp2.signal == "PARTIAL_SELL_TP2"
    assert tp3.signal == "PARTIAL_SELL_TP3"
    assert state.position_size == 0.0
    assert state.initial_entry_price == 0.0
    assert state.stop_loss == 0.0
    assert state.tp_taken_count == 0


def test_trailing_stop_updates_and_stop_exit_resets() -> None:
    state = AtrMeanReversionState("BTCUSDT", "1h", position_size=1.0, initial_entry_price=100.0, risk_amount=5.0, initial_order_size=1.0, stop_loss=90.0)

    hold = generate_timeframe_signal(_signal_row(close=101.0, low=99.0, stop_loss_seed=94.0), state)
    update_position_state(hold, state)
    stop = generate_timeframe_signal(_signal_row(close=93.0, low=92.0, stop_loss_seed=95.0), state)
    update_position_state(stop, state)

    assert hold.signal == "HOLD"
    assert state.position_size == 0.0
    assert stop.signal == "SELL_STOP"


def test_combined_decision_requires_1h_trigger_and_10m_confirmation() -> None:
    one_hour = _output_signal("1h", signal="BUY", entry=True, reject=True, drift=True, close=100.0, stop=95.0)
    ten_min = _output_signal("10m", signal="HOLD", entry=False, reject=False, drift=True, close=101.0, stop=96.0)

    decision = combine_1h_10m_signals(one_hour, ten_min)

    assert decision["signal"] == "BUY"
    assert decision["reason"] == "1h trigger plus 10m confirmation"


def test_combined_decision_prioritizes_10m_stop() -> None:
    one_hour = _output_signal("1h", signal="BUY", entry=True, reject=True, drift=True, close=100.0, stop=95.0)
    ten_min = _output_signal("10m", signal="SELL_STOP", entry=False, reject=False, drift=False, close=94.0, stop=96.0)

    decision = combine_1h_10m_signals(one_hour, ten_min)

    assert decision["signal"] == "SELL_STOP"
    assert decision["source_timeframe"] == "10m"


def test_default_configs_are_timeframe_specific() -> None:
    one_hour = default_atr_mean_reversion_config("1h")
    ten_min = default_atr_mean_reversion_config("10m")

    assert one_hour.atr_multiplier == 2.0
    assert one_hour.confidence_interval == "95%"
    assert ten_min.atr_multiplier == 1.5
    assert ten_min.confidence_interval == "90%"


def _rows(count: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "open_time": pd.date_range("2026-01-01", periods=count, freq="1h", tz="UTC"),
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "is_complete": True,
        }
    )


def _signal_row(
    *,
    reject: bool = False,
    drift_confirmation: bool = False,
    close: float = 100.0,
    low: float = 95.0,
    timeframe: str = "1h",
    stop_loss_seed: float = 90.0,
) -> pd.Series:
    return pd.Series(
        {
            "symbol": "BTCUSDT",
            "timeframe": timeframe,
            "close": close,
            "low": low,
            "atr_14": 2.5,
            "atr_fast_5": 3.0,
            "atr_slow_50": 2.0,
            "test_stat": 2.2 if reject else 0.3,
            "reject_H0": reject,
            "drift": 0.01,
            "drift_confirmation": drift_confirmation,
            "stop_loss_seed": stop_loss_seed,
            "risk_amount": 5.0,
        }
    )


def _output_signal(
    timeframe: str,
    *,
    signal: str,
    entry: bool,
    reject: bool,
    drift: bool,
    close: float,
    stop: float,
) -> AtrMeanReversionSignal:
    return AtrMeanReversionSignal(
        symbol="BTCUSDT",
        timeframe=timeframe,
        close=close,
        atr_14=2.0,
        atr_fast_5=3.0,
        atr_slow_50=2.0,
        test_stat=2.0,
        reject_H0=reject,
        drift=0.01,
        drift_confirmation=drift,
        entry_signal=entry,
        stop_loss=stop,
        tp1=105.0,
        tp2=110.0,
        tp3=115.0,
        signal=signal,
    )
