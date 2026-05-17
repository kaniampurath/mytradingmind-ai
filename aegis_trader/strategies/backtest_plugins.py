from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd

from aegis_trader.analytics.replay_metrics import (
    SymbolMetrics,
    Trade,
)


@dataclass(frozen=True)
class BacktestSignal:
    entry: bool
    stop_price: float
    take_profit_price: float
    reason: str


class BacktestStrategy(ABC):
    name: str
    description: str
    max_hold_bars: int = 24

    @abstractmethod
    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        """Return an entry signal. Execution remains owned by the bot framework."""

    def exit_reason(self, row: pd.Series, active_trade: dict[str, Any]) -> str | None:
        close = float(row["close"])
        if float(row["low"]) <= float(active_trade["stop"]):
            return "stop"
        if float(row["high"]) >= float(active_trade["take_profit"]):
            return "take_profit"
        if int(active_trade["bars"]) >= self.max_hold_bars:
            return "time"
        if close < float(row["ema20"]):
            return "ema20_loss"
        return None

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        latest = rows.iloc[-1]
        previous = rows.iloc[-2] if len(rows) > 1 else None
        close = float(latest["close"])
        if active_trade is not None:
            entry = float(active_trade["entry"])
            pnl_pct = (close - entry) / entry * 100
            return "IN TRADE", f"{self.name} active replay position tracking mark-to-market", entry, notional * pnl_pct / 100, pnl_pct
        signal = self.entry_signal(latest, previous)
        if signal is not None:
            return "BUY", signal.reason, None, None, None
        proximity = _signal_proximity(latest)
        if float(proximity["watch_score"]) >= 60:
            return "WATCH", f"{self.name} setup forming", None, None, None
        return "NO SIGNAL", f"{self.name} conditions below entry threshold", None, None, None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        if df.empty:
            raise ValueError("cannot replay empty feature set")
        symbol = str(df["symbol"].iloc[-1])
        trades: list[Trade] = []
        equity = 0.0
        equity_curve: list[float] = []
        in_trade: dict[str, Any] | None = None
        rows = df.reset_index(drop=True)

        for index, row in rows.iterrows():
            if index < 200:
                equity_curve.append(equity)
                continue
            close = float(row["close"])
            if in_trade is not None:
                in_trade["bars"] += 1
                reason = self.exit_reason(row, in_trade)
                if reason:
                    exit_price = in_trade["stop"] if reason == "stop" else in_trade["take_profit"] if reason == "take_profit" else close
                    return_pct = (exit_price - in_trade["entry"]) / in_trade["entry"] * 100
                    pnl = notional * (return_pct / 100)
                    equity += pnl
                    trades.append(
                        Trade(
                            symbol=symbol,
                            entry_time=in_trade["entry_time"],
                            exit_time=row["open_time"].isoformat(),
                            entry_price=in_trade["entry"],
                            exit_price=exit_price,
                            stop_price=in_trade["stop"],
                            take_profit_price=in_trade["take_profit"],
                            bars_held=in_trade["bars"],
                            return_pct=return_pct,
                            pnl=pnl,
                            exit_reason=reason,
                        )
                    )
                    in_trade = None

            previous = rows.iloc[index - 1] if index > 0 else None
            signal = self.entry_signal(row, previous) if in_trade is None else None
            if signal is not None:
                in_trade = {
                    "entry": close,
                    "entry_time": row["open_time"].isoformat(),
                    "stop": signal.stop_price,
                    "take_profit": signal.take_profit_price,
                    "bars": 0,
                }
            equity_curve.append(equity)

        latest = rows.iloc[-1]
        bucket, reason, active_entry, active_pnl, active_pnl_pct = self.classify_latest(rows, in_trade, notional)
        proximity = _signal_proximity(latest, in_trade)
        confidence = _confidence_from_score_backtest(rows, proximity)
        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl <= 0)
        gains = sum(trade.pnl for trade in trades if trade.pnl > 0)
        gross_losses = abs(sum(trade.pnl for trade in trades if trade.pnl < 0))
        returns = [trade.return_pct for trade in trades]
        return (
            SymbolMetrics(
                symbol=symbol,
                candles=len(rows),
                trades=len(trades),
                wins=wins,
                losses=losses,
                win_rate=0.0 if not trades else wins / len(trades) * 100,
                total_pnl=equity,
                total_return_pct=equity / notional * 100,
                profit_factor=float("inf") if gross_losses == 0 and gains > 0 else 0.0 if gross_losses == 0 else gains / gross_losses,
                max_drawdown_pct=_max_drawdown_pct(equity_curve, notional),
                avg_trade_return_pct=0.0 if not returns else sum(returns) / len(returns),
                sharpe_proxy=_sharpe_proxy(returns),
                last_close=float(latest["close"]),
                scan_bucket=bucket,
                scan_reason=reason,
                active_entry=active_entry,
                active_pnl=active_pnl,
                active_pnl_pct=active_pnl_pct,
                watch_score=proximity["watch_score"],
                buy_score=proximity["buy_score"],
                sell_score=proximity["sell_score"],
                orderflow_score=proximity["orderflow_score"],
                confidence_score=confidence["confidence_score"],
                watch_missing=proximity["watch_missing"],
                buy_missing=proximity["buy_missing"],
                sell_missing=proximity["sell_missing"],
                orderflow_reason=proximity["orderflow_reason"],
                confidence_reason=confidence["confidence_reason"],
            ),
            trades,
        )


class ExistingMomentumStrategy(BacktestStrategy):
    name = "Existing Momentum"
    description = "Current dashboard trend/VWAP/RVOL/delta strategy."

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        close = float(row["close"])
        atr = float(row["atr14"])
        if (
            close > float(row["ema20"]) > float(row["ema50"])
            and close > float(row["vwap"])
            and float(row["rvol30"]) >= 0.9
            and float(row["delta_ratio"]) > 0.02
            and 0.001 <= float(row["volatility"]) <= 0.08
        ):
            return BacktestSignal(True, max(0.000001, close - (1.4 * atr)), close + (2.4 * atr), "trend, VWAP, RVOL, and taker delta aligned")
        return None


class ATRTrendBurstStrategy(BacktestStrategy):
    name = "ATR Trend Burst"
    description = "Port of bot_prodv4 ATR trend-burst entry logic into attachable strategy form."
    max_hold_bars = 18

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None
        close = float(row["close"])
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        atr = float(row["atr14"])
        ema20 = float(row["ema20"])
        ema200 = float(row["ema200"])
        atr_frac = atr / close if close > 0 else 0.0
        bar_change = max(abs(close - open_), abs(high - open_), abs(low - open_))
        bull_trend = close > ema20 > ema200
        atr_burst = bar_change >= 1.5 * atr
        up_bar = close > open_
        vol_ok = 0.002 <= atr_frac <= 0.03
        flow_ok = float(row["delta_ratio"]) > -0.05 and float(row["rvol30"]) >= 0.8
        if bull_trend and atr_burst and up_bar and vol_ok and flow_ok:
            stop = min(float(row["low"]), close - (1.15 * atr))
            return BacktestSignal(True, max(0.000001, stop), close + (1.8 * (close - stop)), "ATR burst with EMA20/200 trend and acceptable flow")
        return None


class VWAPReclaimBacktestStrategy(BacktestStrategy):
    name = "VWAP Reclaim"
    description = "VWAP reclaim continuation strategy with orderflow confirmation."

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None
        close = float(row["close"])
        vwap = float(row["vwap"])
        atr = float(row["atr14"])
        reclaimed = float(previous["close"]) < float(previous["vwap"]) and close > vwap
        if reclaimed and close > float(row["ema20"]) and float(row["delta_ratio"]) > 0 and float(row["rvol30"]) >= 0.8:
            return BacktestSignal(True, max(0.000001, close - atr), close + (1.7 * atr), "VWAP reclaim with positive delta and volume confirmation")
        return None


STRATEGY_REGISTRY: dict[str, BacktestStrategy] = {
    strategy.name: strategy
    for strategy in (
        ExistingMomentumStrategy(),
        ATRTrendBurstStrategy(),
        VWAPReclaimBacktestStrategy(),
    )
}


def get_strategy(name: str) -> BacktestStrategy:
    return STRATEGY_REGISTRY[name]


def _max_drawdown_pct(equity_curve: list[float], notional: float) -> float:
    peak = -math.inf
    max_drawdown = 0.0
    for equity in equity_curve:
        account = notional + equity
        peak = max(peak, account)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - account) / peak * 100)
    return max_drawdown


def _sharpe_proxy(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    series = pd.Series(returns, dtype="float64") / 100
    std = float(series.std(ddof=1))
    if std == 0:
        return 0.0
    return float(series.mean() / std * math.sqrt(max(1, len(series))))


def _confidence_from_score_backtest(rows: pd.DataFrame, current_proximity: dict[str, float | str], horizon_bars: int = 12) -> dict[str, float | str]:
    samples: list[float] = []
    wins = 0
    for index in range(200, max(200, len(rows) - horizon_bars)):
        row = rows.iloc[index]
        score = float(_signal_proximity(row)["buy_score"])
        if score < 80:
            continue
        entry = float(row["close"])
        future = rows.iloc[index + horizon_bars]
        forward_return = (float(future["close"]) - entry) / entry * 100
        samples.append(forward_return)
        if forward_return > 0:
            wins += 1

    current_buy_score = float(current_proximity["buy_score"])
    if not samples:
        return {
            "confidence_score": round(current_buy_score * 0.35, 1),
            "confidence_reason": "low confidence: no historical high-score samples",
        }

    avg_return = sum(samples) / len(samples)
    win_rate = wins / len(samples)
    sample_quality = min(1.0, len(samples) / 80)
    expectancy_component = max(0.0, min(1.0, (avg_return + 1.0) / 2.0))
    reliability = ((win_rate * 0.65) + (expectancy_component * 0.35)) * sample_quality
    confidence = (current_buy_score * 0.55) + (reliability * 100 * 0.45)
    return {
        "confidence_score": round(max(0.0, min(100.0, confidence)), 1),
        "confidence_reason": f"{len(samples)} high-score samples, win {win_rate * 100:.1f}%, avg {avg_return:.2f}% over {horizon_bars} bars",
    }


def _signal_proximity(row: pd.Series, active_trade: dict[str, Any] | None = None) -> dict[str, float | str]:
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    vwap = float(row["vwap"])
    rvol = float(row["rvol30"])
    delta = float(row["delta_ratio"])
    volatility = float(row["volatility"])
    orderflow_score = float(row["orderflow_score"]) if "orderflow_score" in row and pd.notna(row["orderflow_score"]) else _proxy_orderflow_score(rvol, delta, volatility)

    watch_gates = [
        ("above EMA20", close > ema20),
        ("EMA20 >= EMA50", ema20 >= ema50),
        ("near VWAP", close >= vwap * 0.995),
        ("RVOL >= 0.70", rvol >= 0.7),
        ("delta > -0.05", delta > -0.05),
    ]
    buy_gates = [
        ("above EMA20", close > ema20),
        ("EMA20 > EMA50", ema20 > ema50),
        ("above VWAP", close > vwap),
        ("RVOL >= 0.90", rvol >= 0.9),
        ("delta > 0.02", delta > 0.02),
        ("orderflow >= 55", orderflow_score >= 55),
        ("volatility in band", 0.001 <= volatility <= 0.08),
    ]
    sell_gates = [
        ("below EMA20", close < ema20),
        ("below VWAP", close < vwap),
        ("negative delta", delta < -0.02),
        ("orderflow weak", orderflow_score < 45),
        ("volatility breach", volatility > 0.08),
        ("active trade", active_trade is not None),
    ]
    return {
        "watch_score": _gate_score(watch_gates),
        "buy_score": _gate_score(buy_gates),
        "sell_score": _gate_score(sell_gates),
        "orderflow_score": round(orderflow_score, 1),
        "watch_missing": _missing_gates(watch_gates),
        "buy_missing": _missing_gates(buy_gates),
        "sell_missing": _missing_gates(sell_gates),
        "orderflow_reason": _orderflow_reason(orderflow_score, delta, rvol, volatility),
    }


def _gate_score(gates: list[tuple[str, bool]]) -> float:
    if not gates:
        return 0.0
    return round(sum(1 for _, passed in gates if passed) / len(gates) * 100, 1)


def _missing_gates(gates: list[tuple[str, bool]]) -> str:
    missing = [name for name, passed in gates if not passed]
    return "ready" if not missing else ", ".join(missing)


def _proxy_orderflow_score(rvol: float, delta: float, volatility: float) -> float:
    delta_component = max(0.0, min(1.0, (delta + 1) / 2))
    rvol_component = max(0.0, min(1.0, rvol / 2.0))
    volatility_component = 1.0 if 0.001 <= volatility <= 0.08 else 0.35
    return round(((delta_component * 0.45) + (rvol_component * 0.35) + (volatility_component * 0.20)) * 100, 1)


def _orderflow_reason(orderflow_score: float, delta: float, rvol: float, volatility: float) -> str:
    if orderflow_score >= 70:
        return "supportive flow: positive pressure/liquidity"
    if orderflow_score >= 55:
        return "acceptable flow: confirmation present"
    if orderflow_score >= 45:
        return "neutral flow: needs stronger confirmation"
    return f"weak flow: delta {delta:.2f}, rvol {rvol:.2f}, volatility {volatility:.4f}"
