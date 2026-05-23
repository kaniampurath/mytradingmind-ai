from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

import pandas as pd

from aegis_trader.analytics.replay_metrics import (
    SymbolMetrics,
    Trade,
)
from aegis_trader.strategies.exits import (
    DailyExitConfig,
    five_min_atr_burst_params,
    five_min_final_exit_decision,
    five_min_partial_tp_decision,
    initial_five_min_stop,
    daily_timeframe_exit,
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
    default_timeframe: str = "1h"
    activation_state: str = "DORMANT"
    activation_reason: str = "Experimental strategy: keep dormant until it passes objective backtest, stress, journal, and risk certification gates."
    certified_symbols: tuple[str, ...] = ()
    max_hold_bars: int = 24
    runtime_drawdown_lock_pct: float | None = 6.0
    daily_exit_config = DailyExitConfig()
    use_lower_timeframe_sell_stack: bool = True

    @abstractmethod
    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        """Return an entry signal. Execution remains owned by the bot framework."""

    def exit_reason(self, row: pd.Series, active_trade: dict[str, Any]) -> str | None:
        if self._uses_daily_exit_stack():
            decision = daily_timeframe_exit(row, active_trade, self.daily_exit_config)
            if decision is not None:
                active_trade["exit_price"] = decision.exit_price
                active_trade["daily_exit_stop"] = decision.stop_price
                active_trade["mfe_pct"] = decision.mfe_pct
                active_trade["mae_pct"] = decision.mae_pct
                return decision.reason
            return None
        if self.use_lower_timeframe_sell_stack:
            decision = five_min_final_exit_decision(row, active_trade)
            if decision is not None:
                active_trade["exit_price"] = float(decision.exit_price or row["close"])
                active_trade["cancel_protection"] = decision.cancel_protection
                return decision.reason
            if int(active_trade["bars"]) >= self.max_hold_bars:
                active_trade["exit_price"] = float(row["close"])
                return "TIME_STOP"
            return None
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

    def _uses_daily_exit_stack(self) -> bool:
        return str(getattr(self, "default_timeframe", "")).lower() in {"1d", "d", "daily"}

    def _prepare_exit_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        if self._uses_daily_exit_stack() or not self.use_lower_timeframe_sell_stack:
            return rows
        symbol = str(rows["symbol"].iloc[-1]) if "symbol" in rows and not rows.empty else ""
        params = five_min_atr_burst_params(symbol)
        close = pd.to_numeric(rows["close"], errors="coerce")
        if "exit_ema_fast" not in rows:
            rows["exit_ema_fast"] = close.ewm(span=params.ema_fast_len, adjust=False).mean()
        if "exit_ema_stop" not in rows:
            rows["exit_ema_stop"] = close.ewm(span=params.ema_stop_len, adjust=False).mean()
        if "kcj_ema_fast" not in rows:
            rows["kcj_ema_fast"] = rows["exit_ema_fast"]
        if "kcj_ema_stop" not in rows:
            rows["kcj_ema_stop"] = rows["exit_ema_stop"]
        return rows

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
        peak_equity = notional
        trading_locked = False
        in_trade: dict[str, Any] | None = None
        rows = self._prepare_exit_rows(df)

        for index, row in rows.iterrows():
            if index < 200:
                equity_curve.append(equity)
                continue
            close = float(row["close"])
            if in_trade is not None:
                in_trade["bars"] += 1
                if self.use_lower_timeframe_sell_stack and not self._uses_daily_exit_stack():
                    symbol_params = five_min_atr_burst_params(symbol)
                    partial = five_min_partial_tp_decision(row, in_trade, tp_rr=symbol_params.tp_rr)
                    if partial is not None:
                        partial_price = float(partial.exit_price or close)
                        partial_qty = float(partial.partial_qty)
                        partial_return_pct = partial_qty * ((partial_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                        in_trade["realized_pct"] = float(in_trade.get("realized_pct", 0.0)) + partial_return_pct
                        in_trade["remaining"] = max(0.0, float(in_trade.get("remaining", 1.0)) - partial_qty)
                        in_trade["partial_taken"] = True
                        in_trade["stop"] = float(partial.stop_price or in_trade["entry"])
                        in_trade["replace_protection"] = partial.replace_protection
                        if partial.force_shutdown:
                            final_pct = float(in_trade["remaining"]) * ((partial_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                            in_trade["realized_pct"] = float(in_trade.get("realized_pct", 0.0)) + final_pct
                            in_trade["remaining"] = 0.0
                            in_trade["exit_price"] = partial_price
                            in_trade["force_shutdown"] = True
                            reason = "PARTIAL_TP_MIN_NOTIONAL_EXIT"
                        else:
                            reason = None
                    else:
                        reason = None
                else:
                    reason = None
                reason = reason or self.exit_reason(row, in_trade)
                if reason:
                    exit_price = float(in_trade["exit_price"]) if "exit_price" in in_trade else in_trade["stop"] if reason == "stop" else in_trade["take_profit"] if reason == "take_profit" else close
                    remaining = float(in_trade.get("remaining", 1.0))
                    open_return_pct = remaining * ((exit_price - in_trade["entry"]) / in_trade["entry"] * 100)
                    return_pct = float(in_trade.get("realized_pct", 0.0)) + open_return_pct
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
                    peak_equity = max(peak_equity, notional + equity)
                    if self.runtime_drawdown_lock_pct is not None and peak_equity > 0:
                        drawdown_pct = (peak_equity - (notional + equity)) / peak_equity * 100
                        if drawdown_pct >= self.runtime_drawdown_lock_pct:
                            trading_locked = True
                elif self.use_lower_timeframe_sell_stack and not self._uses_daily_exit_stack():
                    in_trade["stop"] = max(float(in_trade["stop"]), float(row.get("exit_ema_stop", row.get("kcj_ema_stop", in_trade["stop"]))))

            previous = rows.iloc[index - 1] if index > 0 else None
            signal = self.entry_signal(row, previous) if in_trade is None and not trading_locked else None
            if signal is not None:
                signal_stop = signal.stop_price
                signal_take_profit = signal.take_profit_price
                if self.use_lower_timeframe_sell_stack and not self._uses_daily_exit_stack():
                    symbol_params = five_min_atr_burst_params(symbol)
                    signal_stop = initial_five_min_stop(close, float(row["atr14"]), symbol_params.atr_mult)
                    risk = max(0.0, close - signal_stop)
                    signal_take_profit = close + (symbol_params.tp_rr * risk) if symbol_params.tp_rr > 0 and risk > 0 else signal.take_profit_price
                in_trade = {
                    "entry": close,
                    "entry_time": row["open_time"].isoformat(),
                    "stop": signal_stop,
                    "take_profit": signal_take_profit,
                    "bars": 0,
                    "notional": notional,
                    "remaining": 1.0,
                    "partial_taken": False,
                    "realized_pct": 0.0,
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


@dataclass(frozen=True)
class _KcjParams:
    ema_fast_len: int
    ema_slow_len: int
    ema_stop_len: int
    atr_burst_mult: float
    tp_rr: float
    atr_min_frac: float
    atr_max_frac: float
    min_rvol: float
    min_delta: float
    min_orderflow_score: float
    max_atr_strength: float
    min_close_location: float
    max_upper_wick_frac: float
    max_previous_drop_atr: float


class KCJATRTrendBurstParityStrategy(BacktestStrategy):
    name = "KCJ ATR Trend Burst 5m"
    description = "TradingView KCJ V3_9 ATR Trend Burst parity strategy hardened for 5-minute candle validation."
    default_timeframe = "5m"
    activation_state = "ACTIVE"
    activation_reason = "Deployable paper/testnet candidate after 5m hardening; latest top-coin backtest guidance certifies ETH and XRP for deployment."
    certified_symbols = ("ETH/USDT", "XRP/USDT")
    max_hold_bars = 288
    stop_mult = 1.22
    partial_qty = 0.50
    commission_rate = 0.0001
    deployment_tokens: tuple[str, ...] = ("BTC", "ETH", "XRP", "DOGE")

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        symbol = str(row.get("symbol", ""))
        if not self._is_supported_symbol(symbol):
            return None
        params = self._params(symbol)
        close = float(row["close"])
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        atr = float(row["atr14"])
        ratio = float(row.get("kcj_ratio", 0.0))
        ema_slope_fast = float(row.get("kcj_ema_slope_fast", 0.0))
        ema_slope_slow = float(row.get("kcj_ema_slope_slow", 0.0))
        atr_frac = atr / close if close > 0 else 0.0
        bull_floor = 0.975 if self._is_symbol(symbol, "BTC") else 0.985
        bull_trend = bull_floor < ratio < 1.05
        bar_change = abs(close - open_)
        atr_burst = bar_change >= params.atr_burst_mult * atr
        atr_strength = bar_change / atr if atr > 0 else 0.0
        candle_range = max(0.000001, high - low)
        close_location = (close - low) / candle_range
        upper_wick_frac = (high - max(open_, close)) / candle_range
        orderflow_score = (
            float(row["orderflow_score"])
            if "orderflow_score" in row and pd.notna(row["orderflow_score"])
            else _proxy_orderflow_score(float(row.get("rvol30", 1.0)), float(row.get("delta_ratio", 0.0)), float(row.get("volatility", atr_frac)))
        )
        previous_drop_atr = 0.0
        if previous is not None and atr > 0:
            previous_drop_atr = max(0.0, float(previous["open"]) - float(previous["close"])) / atr
        base_long = (
            bull_trend
            and atr_burst
            and atr_strength > 1.35
            and atr_strength <= params.max_atr_strength
            and close > open_
            and close > float(row.get("vwap", close))
            and close >= float(row.get("kcj_ema_fast", row.get("ema20", close)))
            and close_location >= params.min_close_location
            and upper_wick_frac <= params.max_upper_wick_frac
            and ema_slope_fast > 0
            and ema_slope_slow > 0
            and float(row.get("rvol30", 1.0)) >= params.min_rvol
            and float(row.get("delta_ratio", 0.0)) >= params.min_delta
            and orderflow_score >= params.min_orderflow_score
            and previous_drop_atr <= params.max_previous_drop_atr
            and params.atr_min_frac <= atr_frac <= params.atr_max_frac
            and bool(row.get("kcj_in_session", True))
        )
        if not base_long:
            return None
        stop = initial_five_min_stop(close, atr, five_min_atr_burst_params(symbol).atr_mult)
        take_profit = close + max(0.000001, params.tp_rr) * (close - stop) if params.tp_rr > 0 else close + (2.0 * atr)
        return BacktestSignal(True, stop, take_profit, "KCJ ATR burst parity: trend, session, volatility, and cycle gates aligned")

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        if df.empty:
            raise ValueError("cannot replay empty feature set")
        rows = self._prepare_rows(df)
        symbol = str(rows["symbol"].iloc[-1])
        params = self._params(symbol)
        trades: list[Trade] = []
        equity = 0.0
        equity_curve: list[float] = []
        peak_equity = notional
        in_trade: dict[str, Any] | None = None
        can_trade_cycle = False
        trading_locked = False

        for index, row in rows.iterrows():
            if index < max(params.ema_slow_len + 20, 220):
                equity_curve.append(equity)
                continue

            ratio = float(row["kcj_ratio"])
            previous_ratio = float(rows.iloc[index - 1]["kcj_ratio"])
            if previous_ratio <= 1.0 < ratio:
                can_trade_cycle = True
            if previous_ratio >= 1.0 > ratio:
                can_trade_cycle = False

            close = float(row["close"])
            if in_trade is not None:
                in_trade["bars"] += 1
                stop_price = float(in_trade["stop"])
                realized_pct = 0.0
                exit_reason = ""
                remaining = float(in_trade["remaining"])
                partial = five_min_partial_tp_decision(row, in_trade, tp_rr=params.tp_rr)
                if partial is not None:
                    partial_price = float(partial.exit_price or close)
                    realized_pct += self.partial_qty * ((partial_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    remaining -= self.partial_qty
                    in_trade["partial_taken"] = True
                    in_trade["stop"] = float(partial.stop_price or in_trade["entry"])
                    stop_price = float(in_trade["stop"])
                    if partial.force_shutdown:
                        realized_pct += remaining * ((partial_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                        remaining = 0.0
                        exit_reason = "partial_tp_min_notional_exit"
                final = five_min_final_exit_decision(row, in_trade) if remaining > 0.000001 else None
                if final is not None:
                    final_price = float(final.exit_price or close)
                    realized_pct += remaining * ((final_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    exit_reason = final.reason
                    remaining = 0.0
                elif int(in_trade["bars"]) >= self.max_hold_bars:
                    realized_pct += remaining * ((close - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    exit_reason = "TIME_STOP"
                    remaining = 0.0
                else:
                    in_trade["remaining"] = remaining
                    in_trade["stop"] = max(float(in_trade["stop"]), float(row["kcj_ema_stop"]))
                    stop_price = float(in_trade["stop"])

                if remaining <= 0.000001:
                    fee_pct = self.commission_rate * 100 * 2
                    return_pct = realized_pct - fee_pct
                    pnl = notional * (return_pct / 100)
                    equity += pnl
                    trades.append(
                        Trade(
                            symbol=symbol,
                            entry_time=str(in_trade["entry_time"]),
                            exit_time=row["open_time"].isoformat(),
                            entry_price=float(in_trade["entry"]),
                            exit_price=close,
                            stop_price=stop_price,
                            take_profit_price=float(in_trade.get("take_profit", close)),
                            bars_held=int(in_trade["bars"]),
                            return_pct=return_pct,
                            pnl=pnl,
                            exit_reason=exit_reason or "PARTIAL_TP",
                        )
                    )
                    in_trade = None
                    peak_equity = max(peak_equity, notional + equity)
                    if self.runtime_drawdown_lock_pct is not None and peak_equity > 0:
                        drawdown_pct = (peak_equity - (notional + equity)) / peak_equity * 100
                        if drawdown_pct >= self.runtime_drawdown_lock_pct:
                            trading_locked = True

            signal = self.entry_signal(row, rows.iloc[index - 1]) if in_trade is None and can_trade_cycle and not trading_locked else None
            if signal is not None:
                in_trade = {
                    "entry": close,
                    "entry_time": row["open_time"].isoformat(),
                    "stop": signal.stop_price,
                    "take_profit": signal.take_profit_price,
                    "notional": notional,
                    "bars": 0,
                    "remaining": 1.0,
                    "partial_taken": False,
                }
                can_trade_cycle = False
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

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        prepared = rows if "kcj_ratio" in rows else self._prepare_rows(rows)
        return super().classify_latest(prepared, active_trade, notional)

    def _prepare_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        symbol = str(rows["symbol"].iloc[-1])
        params = self._params(symbol)
        close = pd.to_numeric(rows["close"], errors="coerce")
        rows["kcj_ema_fast"] = close.ewm(span=params.ema_fast_len, adjust=False).mean()
        rows["kcj_ema_slow"] = close.ewm(span=params.ema_slow_len, adjust=False).mean()
        rows["kcj_ema_stop"] = close.ewm(span=params.ema_stop_len, adjust=False).mean()
        rows["kcj_ma_exit"] = close.ewm(span=20, adjust=False).mean()
        rows["kcj_ratio"] = rows["kcj_ema_fast"] / rows["kcj_ema_slow"]
        rows["kcj_ema_slope_fast"] = rows["kcj_ema_fast"] - rows["kcj_ema_fast"].shift(5)
        rows["kcj_ema_slope_slow"] = rows["kcj_ema_slow"] - rows["kcj_ema_slow"].shift(20)
        open_time = pd.to_datetime(rows["open_time"], utc=True, errors="coerce")
        minutes = (open_time.dt.hour * 60) + open_time.dt.minute
        rows["kcj_in_session"] = (minutes >= 330) & (minutes < 1050)
        rows["ema20"] = rows.get("ema20", rows["kcj_ma_exit"])
        rows["ema50"] = rows.get("ema50", rows["kcj_ema_fast"])
        rows["ema200"] = rows.get("ema200", rows["kcj_ema_slow"])
        rows["vwap"] = rows.get("vwap", close)
        rows["rvol30"] = rows.get("rvol30", 1.0)
        rows["delta_ratio"] = rows.get("delta_ratio", 0.0)
        rows["volatility"] = rows.get("volatility", rows["atr14"] / close)
        return rows.dropna(subset=["kcj_ratio", "kcj_ema_slope_fast", "kcj_ema_slope_slow", "atr14"]).reset_index(drop=True)

    @classmethod
    def _params(cls, symbol: str) -> _KcjParams:
        sell_params = five_min_atr_burst_params(symbol)
        is_eth = cls._is_symbol(symbol, "ETH")
        is_linea = cls._is_symbol(symbol, "LINEA")
        is_doge = cls._is_symbol(symbol, "DOGE")
        is_alt = is_linea or is_doge
        return _KcjParams(
            ema_fast_len=sell_params.ema_fast_len,
            ema_slow_len=100 if is_eth else 200,
            ema_stop_len=sell_params.ema_stop_len,
            atr_burst_mult=sell_params.atr_mult,
            tp_rr=sell_params.tp_rr,
            atr_min_frac=0.004 if is_alt else 0.002,
            atr_max_frac=0.021 if is_alt else 0.03,
            min_rvol=1.15 if is_alt else 1.05,
            min_delta=0.06 if is_alt else 0.03,
            min_orderflow_score=58.0 if is_alt else 55.0,
            max_atr_strength=3.2 if is_alt else 3.6,
            min_close_location=0.68,
            max_upper_wick_frac=0.28,
            max_previous_drop_atr=1.4,
        )

    @staticmethod
    def _is_symbol(symbol: str, token: str) -> bool:
        return token in symbol.replace("/", "").upper()

    @classmethod
    def _is_supported_symbol(cls, symbol: str) -> bool:
        normalized = symbol.replace("/", "").upper()
        return any(normalized.startswith(token) for token in cls.deployment_tokens)


class ResearchMomentumVolatilityStrategy(BacktestStrategy):
    name = "Research Momentum Volatility"
    description = "Paper-derived trend, momentum, ATR risk, and optional sentiment-gated strategy."
    default_timeframe = "1d"
    max_hold_bars = 40
    momentum_window = 63
    atr_trailing_mult = 2.0
    stop_atr_mult = 2.0
    target_rr = 2.0
    daily_exit_config = DailyExitConfig()

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None

        close = float(row["close"])
        atr = float(row["atr14"])
        ema50 = float(row["ema50"])
        ema200 = float(row["ema200"])
        momentum = float(row.get("momentum63", 0.0))
        sentiment = str(row.get("sentiment_label", "")).lower()
        top_momentum_eligible = bool(row.get("top_momentum_eligible", True))
        atr_pct = float(row.get("atr_pct", atr / close if close > 0 else 0.0))
        ratio = float(row.get("momentum_volatility_ratio", 0.0))

        bullish_regime = close > ema200
        trend_confirmed = ema50 > ema200 and close > ema50
        momentum_ok = momentum > 0
        volatility_ok = 0.001 <= atr_pct <= 0.08
        sentiment_ok = sentiment != "negative"

        if bullish_regime and trend_confirmed and momentum_ok and top_momentum_eligible and volatility_ok and sentiment_ok:
            stop = max(0.000001, close - (self.stop_atr_mult * atr))
            target = close + (self.target_rr * (close - stop))
            reason = "research trend/momentum setup with ATR risk control"
            if ratio > 0:
                reason = f"{reason}; momentum-volatility ratio {ratio:.2f}"
            return BacktestSignal(True, stop, target, reason)
        return None

    def exit_reason(self, row: pd.Series, active_trade: dict[str, Any]) -> str | None:
        decision = daily_timeframe_exit(row, active_trade, self.daily_exit_config)
        if decision is not None:
            active_trade["exit_price"] = decision.exit_price
            active_trade["daily_exit_stop"] = decision.stop_price
            active_trade["mfe_pct"] = decision.mfe_pct
            active_trade["mae_pct"] = decision.mae_pct
            return decision.reason
        return None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        return super().replay(self._prepare_rows(df), notional=notional)

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        prepared = rows if "momentum63" in rows else self._prepare_rows(rows)
        return super().classify_latest(prepared, active_trade, notional)

    def _prepare_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        close = pd.to_numeric(rows["close"], errors="coerce")
        atr = pd.to_numeric(rows["atr14"], errors="coerce")
        if "momentum63" not in rows:
            rows["momentum63"] = close.pct_change(self.momentum_window)
        if "atr_pct" not in rows:
            rows["atr_pct"] = atr / close
        if "momentum_volatility_ratio" not in rows:
            rows["momentum_volatility_ratio"] = rows["momentum63"] / rows["atr_pct"].replace(0, pd.NA)
        if "top_momentum_eligible" not in rows:
            rows["top_momentum_eligible"] = True
        return rows.dropna(subset=["momentum63", "atr_pct", "momentum_volatility_ratio", "atr14", "ema50", "ema200"]).reset_index(drop=True)


class AcademicTimeSeriesMomentumStrategy(BacktestStrategy):
    name = "Academic Time-Series Momentum"
    description = "Academic 12-month time-series momentum proxy with volatility-aware ATR risk controls."
    default_timeframe = "1d"
    max_hold_bars = 32
    lookback_bars = 252
    stop_atr_mult = 2.0
    target_rr = 2.0
    daily_exit_config = DailyExitConfig()

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None

        close = float(row["close"])
        atr = float(row["atr14"])
        momentum = float(row.get("tsmom252", 0.0))
        atr_pct = float(row.get("atr_pct", atr / close if close > 0 else 0.0))
        vol_scaled_momentum = float(row.get("tsmom_vol_score", 0.0))
        top_momentum_eligible = bool(row.get("top_momentum_eligible", True))

        trend_ok = close > float(row["ema200"]) and float(row["ema50"]) > float(row["ema200"])
        momentum_ok = momentum > 0 and vol_scaled_momentum > 0
        volatility_ok = 0.001 <= atr_pct <= 0.08

        if trend_ok and momentum_ok and volatility_ok and top_momentum_eligible:
            stop = max(0.000001, close - (self.stop_atr_mult * atr))
            target = close + (self.target_rr * (close - stop))
            return BacktestSignal(
                True,
                stop,
                target,
                f"academic time-series momentum positive over {self.lookback_bars} bars with volatility scaling",
            )
        return None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        return super().replay(self._prepare_rows(df), notional=notional)

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        prepared = rows if "tsmom252" in rows else self._prepare_rows(rows)
        return super().classify_latest(prepared, active_trade, notional)

    def _prepare_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        close = pd.to_numeric(rows["close"], errors="coerce")
        atr = pd.to_numeric(rows["atr14"], errors="coerce")
        if "tsmom252" not in rows:
            rows["tsmom252"] = close.pct_change(self.lookback_bars)
        if "atr_pct" not in rows:
            rows["atr_pct"] = atr / close
        if "tsmom_vol_score" not in rows:
            rows["tsmom_vol_score"] = rows["tsmom252"] / rows["atr_pct"].replace(0, pd.NA)
        if "top_momentum_eligible" not in rows:
            rows["top_momentum_eligible"] = True
        return rows.dropna(subset=["tsmom252", "atr_pct", "tsmom_vol_score", "atr14", "ema50", "ema200"]).reset_index(drop=True)


class AcademicShortTermReversalStrategy(BacktestStrategy):
    name = "Academic Short-Term Reversal"
    description = "Academic prior-month reversal proxy adapted to long-only crypto spot with liquidity and trend filters."
    default_timeframe = "1d"
    max_hold_bars = 21
    reversal_lookback_bars = 21
    min_prior_loss_pct = -0.08
    stop_atr_mult = 2.0
    target_rr = 1.6
    daily_exit_config = DailyExitConfig(time_stop_bars=25, time_stop_min_pnl_pct=2.0)

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None

        close = float(row["close"])
        atr = float(row["atr14"])
        prior_return = float(row.get("reversal21", 0.0))
        atr_pct = float(row.get("atr_pct", atr / close if close > 0 else 0.0))
        reclaiming = close > float(previous["close"]) and close >= float(row["ema20"])
        regime_ok = close > float(row["ema200"])
        liquidity_ok = float(row.get("rvol30", 1.0)) >= 0.8 and float(row.get("delta_ratio", 0.0)) > -0.15
        oversold_ok = prior_return <= self.min_prior_loss_pct
        volatility_ok = 0.001 <= atr_pct <= 0.10

        if oversold_ok and reclaiming and regime_ok and liquidity_ok and volatility_ok:
            stop = max(0.000001, close - (self.stop_atr_mult * atr))
            target = close + (self.target_rr * (close - stop))
            return BacktestSignal(
                True,
                stop,
                target,
                "academic short-term reversal: prior-month loser reclaim with liquidity confirmation",
            )
        return None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        return super().replay(self._prepare_rows(df), notional=notional)

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        prepared = rows if "reversal21" in rows else self._prepare_rows(rows)
        return super().classify_latest(prepared, active_trade, notional)

    def _prepare_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        close = pd.to_numeric(rows["close"], errors="coerce")
        atr = pd.to_numeric(rows["atr14"], errors="coerce")
        if "reversal21" not in rows:
            rows["reversal21"] = close.pct_change(self.reversal_lookback_bars)
        if "atr_pct" not in rows:
            rows["atr_pct"] = atr / close
        return rows.dropna(subset=["reversal21", "atr_pct", "atr14", "ema20", "ema200"]).reset_index(drop=True)


class TradingViewMeanReversionAtrStrategy(BacktestStrategy):
    name = "TradingView Mean Reversion ATR"
    description = "PineScript-derived ATR diversion mean-reversion strategy with drift confirmation, ATR trailing stop, and three staged partial exits."
    default_timeframe = "1h"
    max_hold_bars = 72
    use_lower_timeframe_sell_stack = False
    atr_trailing_len = 14
    atr_trailing_mult = 2.0
    atr_fast_len = 5
    atr_slow_len = 50
    drift_len = 14
    critical_value = 1.96
    first_level_profit = 1.0
    second_level_profit = 2.0
    third_level_profit = 3.0
    deployment_tokens: tuple[str, ...] = ()

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None
        if self.deployment_tokens and not self._is_supported_symbol(str(row.get("symbol", ""))):
            return None
        if not bool(row.get("kl_entry_signal", False)):
            return None
        close = float(row["close"])
        risk = float(row.get("kl_risk_amt", row["atr14"] * self.atr_trailing_mult))
        stop = max(0.000001, close - risk)
        return BacktestSignal(
            True,
            stop,
            close + (self.third_level_profit * risk),
            "TradingView ATR diversion rejected H0 with upward drift confirmation",
        )

    def exit_reason(self, row: pd.Series, active_trade: dict[str, Any]) -> str | None:
        stop = float(active_trade["stop"])
        if float(row["low"]) <= stop:
            active_trade["exit_price"] = stop
            return "ATR_TRAILING_STOP"
        if int(active_trade["bars"]) >= self.max_hold_bars:
            active_trade["exit_price"] = float(row["close"])
            return "TIME_STOP"
        return None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        if df.empty:
            raise ValueError("cannot replay empty feature set")
        rows = self._prepare_rows(df)
        if rows.empty:
            raise ValueError("cannot replay empty feature set")
        symbol = str(rows["symbol"].iloc[-1])
        trades: list[Trade] = []
        equity = 0.0
        equity_curve: list[float] = []
        peak_equity = notional
        trading_locked = False
        in_trade: dict[str, Any] | None = None

        for index, row in rows.iterrows():
            if index < max(self.atr_slow_len, self.drift_len) + 3:
                equity_curve.append(equity)
                continue
            close = float(row["close"])
            if in_trade is not None:
                in_trade["bars"] += 1
                in_trade["stop"] = max(float(in_trade["stop"]), float(row["low"]) - float(row["kl_atr_tsl"]))
                realized_pct = 0.0
                remaining = float(in_trade["remaining"])
                for level, multiple in enumerate((self.first_level_profit, self.second_level_profit, self.third_level_profit), start=1):
                    if int(in_trade["tp_count"]) == level - 1 and close >= float(in_trade["entry"]) + (multiple * float(in_trade["risk_amt"])):
                        qty = min(remaining, 1.0 / 3.0)
                        realized_pct += qty * ((close - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                        remaining -= qty
                        in_trade["tp_count"] = level
                        in_trade["remaining"] = remaining

                reason = self.exit_reason(row, in_trade)
                if remaining <= 0.000001:
                    reason = "TP_LVL3"
                    in_trade["exit_price"] = close
                if reason:
                    exit_price = float(in_trade.get("exit_price", close))
                    open_return_pct = max(0.0, remaining) * ((exit_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    return_pct = float(in_trade.get("realized_pct", 0.0)) + realized_pct + open_return_pct
                    pnl = notional * return_pct / 100
                    equity += pnl
                    trades.append(
                        Trade(
                            symbol=symbol,
                            entry_time=in_trade["entry_time"],
                            exit_time=row["open_time"].isoformat(),
                            entry_price=float(in_trade["entry"]),
                            exit_price=exit_price,
                            stop_price=float(in_trade["stop"]),
                            take_profit_price=float(in_trade["take_profit"]),
                            bars_held=int(in_trade["bars"]),
                            return_pct=return_pct,
                            pnl=pnl,
                            exit_reason=reason,
                        )
                    )
                    in_trade = None
                    peak_equity = max(peak_equity, notional + equity)
                    if self.runtime_drawdown_lock_pct is not None and peak_equity > 0:
                        drawdown_pct = (peak_equity - (notional + equity)) / peak_equity * 100
                        if drawdown_pct >= self.runtime_drawdown_lock_pct:
                            trading_locked = True
                else:
                    in_trade["realized_pct"] = float(in_trade.get("realized_pct", 0.0)) + realized_pct
                    in_trade["remaining"] = remaining

            previous = rows.iloc[index - 1] if index > 0 else None
            signal = self.entry_signal(row, previous) if in_trade is None and not trading_locked else None
            if signal is not None:
                risk = max(0.000001, close - signal.stop_price)
                in_trade = {
                    "entry": close,
                    "entry_time": row["open_time"].isoformat(),
                    "stop": signal.stop_price,
                    "take_profit": signal.take_profit_price,
                    "risk_amt": risk,
                    "bars": 0,
                    "remaining": 1.0,
                    "realized_pct": 0.0,
                    "tp_count": 0,
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

    def classify_latest(self, rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
        prepared = rows if "kl_entry_signal" in rows else self._prepare_rows(rows)
        return super().classify_latest(prepared, active_trade, notional)

    def _prepare_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        rows = df.copy().reset_index(drop=True)
        prepared_columns = {"kl_entry_signal", "kl_risk_amt", "kl_atr_tsl", "kl_test_stat", "kl_drift"}
        if prepared_columns.issubset(rows.columns):
            rows["kl_entry_signal"] = rows["kl_entry_signal"].fillna(False).astype(bool)
            return rows.dropna(subset=["kl_atr_tsl", "kl_risk_amt", "kl_test_stat", "kl_drift", "atr14", "ema20", "ema50"]).reset_index(drop=True)
        stateful_prepared_columns = {"kl_risk_amt", "kl_atr_tsl", "kl_test_stat", "kl_drift", "kl_reject_h0", "kl_drift_confirmation"}
        if stateful_prepared_columns.issubset(rows.columns):
            rows["kl_entry_signal"] = self._stateful_entry_signals(rows)
            return rows.dropna(subset=["kl_atr_tsl", "kl_risk_amt", "kl_test_stat", "kl_drift", "atr14", "ema20", "ema50"]).reset_index(drop=True)
        high = pd.to_numeric(rows["high"], errors="coerce")
        low = pd.to_numeric(rows["low"], errors="coerce")
        close = pd.to_numeric(rows["close"], errors="coerce")
        previous_close = close.shift(1)
        tr = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
        atr_fast = tr.rolling(self.atr_fast_len, min_periods=self.atr_fast_len).mean()
        atr_slow = tr.rolling(self.atr_slow_len, min_periods=self.atr_slow_len).mean()
        std_error = tr.rolling(self.atr_fast_len, min_periods=self.atr_fast_len).std(ddof=1) / math.sqrt(self.atr_fast_len)
        test_stat = (atr_fast - atr_slow) / std_error.replace(0, pd.NA)
        log_return = (close / previous_close).apply(lambda value: math.log(value) if value and value > 0 else float("nan")).astype("float64")
        drift = log_return.rolling(self.drift_len, min_periods=self.drift_len).mean() - (log_return.rolling(self.drift_len, min_periods=self.drift_len).std(ddof=1) ** 2 * 0.5)
        rows["kl_atr_tsl"] = tr.rolling(self.atr_trailing_len, min_periods=self.atr_trailing_len).mean() * self.atr_trailing_mult
        rows["kl_test_stat"] = test_stat
        rows["kl_reject_h0"] = test_stat.abs() > self.critical_value
        rows["kl_drift"] = drift
        rows["kl_drift_confirmation"] = drift > drift.shift(1)
        rows["kl_entry_signal"] = self._stateful_entry_signals(rows)
        rows["kl_risk_amt"] = rows["kl_atr_tsl"]
        return rows.dropna(subset=["kl_atr_tsl", "kl_test_stat", "kl_drift", "atr14", "ema20", "ema50"]).reset_index(drop=True)

    @classmethod
    def _stateful_entry_signals(cls, rows: pd.DataFrame) -> pd.Series:
        triggered = False
        signals: list[bool] = []
        for _, row in rows.iterrows():
            if bool(row.get("kl_reject_h0", False)):
                triggered = True
            entry_signal = triggered and bool(row.get("kl_drift_confirmation", False))
            signals.append(entry_signal)
            if entry_signal:
                triggered = False
        return pd.Series(signals, index=rows.index, dtype=bool)

    @classmethod
    def _is_supported_symbol(cls, symbol: str) -> bool:
        normalized = symbol.replace("/", "").upper()
        return any(normalized.startswith(token) for token in cls.deployment_tokens)


class TradingViewMeanReversionAtr10mStrategy(TradingViewMeanReversionAtrStrategy):
    name = "TradingView Mean Reversion ATR 10m"
    description = "10-minute PineScript-derived ATR diversion mean-reversion variant. Requires 10m features resampled from 5m Binance candles."
    default_timeframe = "10m"
    max_hold_bars = 288


class TradingViewMeanReversionAtr1hStrategy(TradingViewMeanReversionAtrStrategy):
    name = "TradingView Mean Reversion ATR 1h"
    description = "1-hour PineScript-derived ATR diversion mean-reversion variant, production-scoped to the validated TRX universe."
    default_timeframe = "1h"
    activation_state = "ACTIVE"
    activation_reason = "Deployable paper/testnet candidate after Pine trigger parity fix; passed 720-day TRX-scoped readiness and flash-crash gates."
    certified_symbols = ("TRX/USDT",)
    max_hold_bars = 72
    deployment_tokens = ("TRX",)


class CertifiedRiskManagedCompositeStrategy(BacktestStrategy):
    name = "Certified Risk Managed Composite"
    description = "Institutional certified strategy collection with profit-factor/drawdown gates and an 8% module drawdown lock."
    activation_state = "ACTIVE"
    activation_reason = "Only deployable strategy in the latest readiness matrix; passed positive PnL, drawdown, stress, and flash-crash gates."
    certified_symbols = ("BTC/USDT", "SOL/USDT", "TRX/USDT")
    runtime_drawdown_lock_pct = 8.0

    def __init__(self) -> None:
        self._modules: dict[str, tuple[BacktestStrategy, ...]] = {
            "BTC/USDT": (self._module(ATRTrendBurstStrategy()),),
            "ETH/USDT": (self._module(ATRTrendBurstStrategy()),),
            "BNB/USDT": (self._module(ExistingMomentumStrategy()),),
            "DOGE/USDT": (self._module(ExistingMomentumStrategy()),),
            "TRX/USDT": (self._module(ATRTrendBurstStrategy()),),
            "AVAX/USDT": (self._module(VWAPReclaimBacktestStrategy()),),
            "SOL/USDT": (self._module(VWAPReclaimBacktestStrategy()),),
            "XRP/USDT": (self._module(VWAPReclaimBacktestStrategy()),),
        }

    def _module(self, strategy: BacktestStrategy) -> BacktestStrategy:
        strategy.runtime_drawdown_lock_pct = self.runtime_drawdown_lock_pct
        return strategy

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        symbol = str(row.get("symbol", ""))
        for module in self._modules.get(symbol, ()):
            signal = module.entry_signal(row, previous)
            if signal is not None:
                return BacktestSignal(
                    entry=True,
                    stop_price=signal.stop_price,
                    take_profit_price=signal.take_profit_price,
                    reason=f"certified module {module.name}: {signal.reason}",
                )
        return None

    def replay(self, df: pd.DataFrame, notional: float = 1_000.0) -> tuple[SymbolMetrics, list[Trade]]:
        if df.empty:
            raise ValueError("cannot replay empty feature set")
        symbol = str(df["symbol"].iloc[-1])
        rows = df.reset_index(drop=True)
        modules = self._modules.get(symbol, ())
        if not modules:
            return self._metrics_from_trades(rows, [], notional, "NO SIGNAL", "symbol is not certified for this risk-managed composite")

        selected: list[Trade] = []
        for module in modules:
            _, trades = module.replay(rows, notional=notional)
            selected.extend(self._apply_module_drawdown_lock(trades, notional))

        selected = self._remove_overlapping_trades(selected)
        bucket, reason, active_entry, active_pnl, active_pnl_pct = self.classify_latest(rows, None, notional)
        metrics, trades = self._metrics_from_trades(rows, selected, notional, bucket, reason)
        return (
            replace(
                metrics,
                active_entry=active_entry,
                active_pnl=active_pnl,
                active_pnl_pct=active_pnl_pct,
            ),
            trades,
        )

    def _apply_module_drawdown_lock(self, trades: list[Trade], notional: float) -> list[Trade]:
        accepted: list[Trade] = []
        equity = 0.0
        peak = notional
        for trade in sorted(trades, key=lambda item: item.entry_time):
            accepted.append(trade)
            equity += trade.pnl
            account = notional + equity
            peak = max(peak, account)
            if peak > 0 and (peak - account) / peak * 100 >= self.runtime_drawdown_lock_pct:
                break
        return accepted

    @staticmethod
    def _remove_overlapping_trades(trades: list[Trade]) -> list[Trade]:
        accepted: list[Trade] = []
        last_exit = pd.Timestamp.min.tz_localize("UTC")
        for trade in sorted(trades, key=lambda item: item.entry_time):
            entry_time = pd.Timestamp(trade.entry_time)
            exit_time = pd.Timestamp(trade.exit_time)
            if entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize("UTC")
            if exit_time.tzinfo is None:
                exit_time = exit_time.tz_localize("UTC")
            if entry_time < last_exit:
                continue
            accepted.append(trade)
            last_exit = exit_time
        return accepted

    def _metrics_from_trades(
        self,
        rows: pd.DataFrame,
        trades: list[Trade],
        notional: float,
        bucket: str,
        reason: str,
    ) -> tuple[SymbolMetrics, list[Trade]]:
        symbol = str(rows["symbol"].iloc[-1])
        latest = rows.iloc[-1]
        equity = 0.0
        equity_curve: list[float] = []
        for trade in sorted(trades, key=lambda item: item.exit_time):
            equity += trade.pnl
            equity_curve.append(equity)

        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl <= 0)
        gains = sum(trade.pnl for trade in trades if trade.pnl > 0)
        gross_losses = abs(sum(trade.pnl for trade in trades if trade.pnl < 0))
        returns = [trade.return_pct for trade in trades]
        proximity = _signal_proximity(latest)
        confidence = _confidence_from_score_backtest(rows, proximity)
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
                max_drawdown_pct=_max_drawdown_pct(equity_curve or [0.0], notional),
                avg_trade_return_pct=0.0 if not returns else sum(returns) / len(returns),
                sharpe_proxy=_sharpe_proxy(returns),
                last_close=float(latest["close"]),
                scan_bucket=bucket,
                scan_reason=reason,
                active_entry=None,
                active_pnl=None,
                active_pnl_pct=None,
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
            sorted(trades, key=lambda item: item.entry_time),
        )


STRATEGY_REGISTRY: dict[str, BacktestStrategy] = {
    strategy.name: strategy
    for strategy in (
        ExistingMomentumStrategy(),
        ATRTrendBurstStrategy(),
        VWAPReclaimBacktestStrategy(),
        KCJATRTrendBurstParityStrategy(),
        ResearchMomentumVolatilityStrategy(),
        AcademicTimeSeriesMomentumStrategy(),
        AcademicShortTermReversalStrategy(),
        TradingViewMeanReversionAtrStrategy(),
        TradingViewMeanReversionAtr10mStrategy(),
        TradingViewMeanReversionAtr1hStrategy(),
        CertifiedRiskManagedCompositeStrategy(),
    )
}


def get_strategy(name: str) -> BacktestStrategy:
    return STRATEGY_REGISTRY[name]


def active_strategy_names() -> list[str]:
    return [name for name, strategy in STRATEGY_REGISTRY.items() if str(getattr(strategy, "activation_state", "")).upper() == "ACTIVE"]


def dormant_strategy_names() -> list[str]:
    return [name for name, strategy in STRATEGY_REGISTRY.items() if str(getattr(strategy, "activation_state", "")).upper() != "ACTIVE"]


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
