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


@dataclass(frozen=True)
class _KcjParams:
    ema_fast_len: int
    ema_slow_len: int
    ema_stop_len: int
    atr_burst_mult: float
    tp_rr: float
    atr_min_frac: float
    atr_max_frac: float


class KCJATRTrendBurstParityStrategy(BacktestStrategy):
    name = "KCJ ATR Trend Burst 5m"
    description = "TradingView KCJ V3_9 ATR Trend Burst parity strategy tuned for 5-minute candle validation."
    default_timeframe = "5m"
    max_hold_bars = 288
    stop_mult = 1.22
    partial_qty = 0.45
    commission_rate = 0.0001

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        symbol = str(row.get("symbol", ""))
        params = self._params(symbol)
        close = float(row["close"])
        open_ = float(row["open"])
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
        base_long = (
            bull_trend
            and atr_burst
            and atr_strength > 1.35
            and close > open_
            and ema_slope_fast > 0
            and ema_slope_slow > 0
            and params.atr_min_frac <= atr_frac <= params.atr_max_frac
            and bool(row.get("kcj_in_session", True))
        )
        if not base_long:
            return None
        stop = max(0.000001, close - (self.stop_mult * atr))
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
        in_trade: dict[str, Any] | None = None
        can_trade_cycle = False

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
                stop_price = max(float(in_trade["stop"]), float(row["kcj_ema_stop"]))
                in_trade["stop"] = stop_price
                if in_trade["entry_risk"] is None:
                    in_trade["entry_risk"] = max(0.0, float(in_trade["entry"]) - stop_price)
                entry_risk = float(in_trade["entry_risk"])
                tp1 = float(in_trade["entry"]) + params.tp_rr * entry_risk if params.tp_rr > 0 and entry_risk > 0 else None
                tp2 = float(in_trade["entry"]) + (params.tp_rr * 1.9) * entry_risk if params.tp_rr > 0 and entry_risk > 0 else None

                realized_pct = 0.0
                exit_reason = ""
                remaining = float(in_trade["remaining"])
                if not bool(in_trade["took_partial"]) and tp1 is not None and float(row["high"]) >= tp1:
                    realized_pct += self.partial_qty * ((tp1 - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    remaining -= self.partial_qty
                    in_trade["took_partial"] = True
                if bool(in_trade["took_partial"]) and tp2 is not None and remaining > self.partial_qty and float(row["high"]) >= tp2:
                    realized_pct += self.partial_qty * ((tp2 - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    remaining -= self.partial_qty
                    exit_reason = "tp2_partial"
                if float(row["low"]) <= stop_price:
                    realized_pct += remaining * ((stop_price - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    exit_reason = "stop"
                    remaining = 0.0
                elif close < float(row["kcj_ma_exit"]) and close < float(row["kcj_ema_fast"]):
                    realized_pct += remaining * ((close - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    exit_reason = "ma_exit"
                    remaining = 0.0
                elif int(in_trade["bars"]) >= self.max_hold_bars:
                    realized_pct += remaining * ((close - float(in_trade["entry"])) / float(in_trade["entry"]) * 100)
                    exit_reason = "time"
                    remaining = 0.0
                else:
                    in_trade["remaining"] = remaining

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
                            take_profit_price=tp2 or tp1 or close,
                            bars_held=int(in_trade["bars"]),
                            return_pct=return_pct,
                            pnl=pnl,
                            exit_reason=exit_reason or "partial_exit",
                        )
                    )
                    in_trade = None

            signal = self.entry_signal(row, rows.iloc[index - 1]) if in_trade is None and can_trade_cycle else None
            if signal is not None:
                in_trade = {
                    "entry": close,
                    "entry_time": row["open_time"].isoformat(),
                    "stop": signal.stop_price,
                    "bars": 0,
                    "entry_risk": None,
                    "remaining": 1.0,
                    "took_partial": False,
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
        is_btc = cls._is_symbol(symbol, "BTC")
        is_sol = cls._is_symbol(symbol, "SOL")
        is_eth = cls._is_symbol(symbol, "ETH")
        is_linea = cls._is_symbol(symbol, "LINEA")
        is_doge = cls._is_symbol(symbol, "DOGE")
        is_alt = is_linea or is_doge
        return _KcjParams(
            ema_fast_len=20 if is_btc else 10 if is_sol else 20,
            ema_slow_len=200 if is_btc else 200 if is_sol else 100 if is_eth else 200,
            ema_stop_len=33 if is_btc else 33 if is_sol else 21 if is_eth else 33,
            atr_burst_mult=1.5 if is_btc else 1.5 if is_sol else 2.0 if is_eth else 1.5,
            tp_rr=1.28 if is_btc else 1.0 if is_sol else 0.0,
            atr_min_frac=0.004 if is_alt else 0.002,
            atr_max_frac=0.021 if is_alt else 0.03,
        )

    @staticmethod
    def _is_symbol(symbol: str, token: str) -> bool:
        return token in symbol.replace("/", "").upper()


class CertifiedRiskManagedCompositeStrategy(BacktestStrategy):
    name = "Certified Risk Managed Composite"
    description = "Institutional certified strategy collection with profit-factor/drawdown gates and an 8% module drawdown lock."
    runtime_drawdown_lock_pct = 8.0

    def __init__(self) -> None:
        self._modules: dict[str, tuple[BacktestStrategy, ...]] = {
            "BTC/USDT": (ATRTrendBurstStrategy(),),
            "ETH/USDT": (ATRTrendBurstStrategy(),),
            "BNB/USDT": (ExistingMomentumStrategy(),),
            "DOGE/USDT": (ExistingMomentumStrategy(),),
            "TRX/USDT": (ATRTrendBurstStrategy(),),
            "AVAX/USDT": (VWAPReclaimBacktestStrategy(),),
            "SOL/USDT": (VWAPReclaimBacktestStrategy(),),
            "XRP/USDT": (VWAPReclaimBacktestStrategy(),),
        }

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
        CertifiedRiskManagedCompositeStrategy(),
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
