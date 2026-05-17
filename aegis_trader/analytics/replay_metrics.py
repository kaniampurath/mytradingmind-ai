from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


TOP_TRADING_SYMBOLS: tuple[str, ...] = (
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "SOL/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "TRX/USDT",
    "LINK/USDT",
    "AVAX/USDT",
)

DEFAULT_LIVE_SYMBOLS: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    stop_price: float
    take_profit_price: float
    bars_held: int
    return_pct: float
    pnl: float
    exit_reason: str


@dataclass(frozen=True)
class SymbolMetrics:
    symbol: str
    candles: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    total_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    avg_trade_return_pct: float
    sharpe_proxy: float
    last_close: float
    scan_bucket: str
    scan_reason: str
    active_entry: float | None
    active_pnl: float | None
    active_pnl_pct: float | None
    watch_score: float
    buy_score: float
    sell_score: float
    orderflow_score: float
    confidence_score: float
    watch_missing: str
    buy_missing: str
    sell_missing: str
    orderflow_reason: str
    confidence_reason: str


def load_feature_file(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    df = df.copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    for column in ["open", "high", "low", "close", "volume", "atr14", "ema20", "ema50", "ema200", "vwap", "rvol30", "volatility", "delta_ratio"]:
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["close", "atr14", "ema20", "ema50", "vwap", "rvol30", "delta_ratio"])


def run_symbol_replay(df: pd.DataFrame, notional: float = 1_000.0, max_hold_bars: int = 24) -> tuple[SymbolMetrics, list[Trade]]:
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
        atr = float(row["atr14"])
        if in_trade is not None:
            in_trade["bars"] += 1
            exit_reason = ""
            exit_price = close
            if float(row["low"]) <= in_trade["stop"]:
                exit_price = in_trade["stop"]
                exit_reason = "stop"
            elif float(row["high"]) >= in_trade["take_profit"]:
                exit_price = in_trade["take_profit"]
                exit_reason = "take_profit"
            elif in_trade["bars"] >= max_hold_bars:
                exit_reason = "time"
            elif close < float(row["ema20"]):
                exit_reason = "ema20_loss"

            if exit_reason:
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
                        exit_reason=exit_reason,
                    )
                )
                in_trade = None

        if in_trade is None and _buy_signal(row):
            in_trade = {
                "entry": close,
                "entry_time": row["open_time"].isoformat(),
                "stop": max(0.000001, close - (1.4 * atr)),
                "take_profit": close + (2.4 * atr),
                "bars": 0,
            }
        equity_curve.append(equity)

    latest = rows.iloc[-1]
    bucket, reason, active_entry, active_pnl, active_pnl_pct = classify_latest(rows, in_trade, notional)
    proximity = signal_proximity(latest, in_trade)
    confidence = confidence_from_score_backtest(rows, proximity)
    wins = sum(1 for trade in trades if trade.pnl > 0)
    losses = sum(1 for trade in trades if trade.pnl <= 0)
    gains = sum(trade.pnl for trade in trades if trade.pnl > 0)
    gross_losses = abs(sum(trade.pnl for trade in trades if trade.pnl < 0))
    returns = [trade.return_pct for trade in trades]
    metrics = SymbolMetrics(
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
    )
    return metrics, trades


def classify_latest(rows: pd.DataFrame, active_trade: dict[str, Any] | None, notional: float) -> tuple[str, str, float | None, float | None, float | None]:
    latest = rows.iloc[-1]
    close = float(latest["close"])
    if active_trade is not None:
        entry = float(active_trade["entry"])
        pnl_pct = (close - entry) / entry * 100
        return "IN TRADE", "active replay position tracking mark-to-market", entry, notional * pnl_pct / 100, pnl_pct
    if _buy_signal(latest):
        return "BUY", "trend, VWAP, RVOL, and taker delta aligned", None, None, None
    if _watch_signal(latest):
        return "WATCH", "setup forming but confirmation incomplete", None, None, None
    return "NO SIGNAL", "conditions below entry threshold", None, None, None


def write_reports(metrics: list[SymbolMetrics], trades: list[Trade], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_df = pd.DataFrame([asdict(item) for item in metrics])
    trades_df = pd.DataFrame([asdict(item) for item in trades])
    metrics_df.to_csv(out_dir / "top10_replay_metrics.csv", index=False)
    metrics_df.to_json(out_dir / "top10_replay_metrics.json", orient="records", indent=2)
    trades_df.to_csv(out_dir / "top10_replay_trades.csv", index=False)
    scan_columns = [
        "symbol",
        "scan_bucket",
        "scan_reason",
        "last_close",
        "active_entry",
        "active_pnl",
        "active_pnl_pct",
        "watch_score",
        "buy_score",
        "sell_score",
        "orderflow_score",
        "confidence_score",
        "watch_missing",
        "buy_missing",
        "sell_missing",
        "orderflow_reason",
        "confidence_reason",
        "trades",
        "win_rate",
        "total_pnl",
        "profit_factor",
    ]
    metrics_df[scan_columns].to_json(out_dir / "live_scan.json", orient="records", indent=2)


def signal_proximity(row: pd.Series, active_trade: dict[str, Any] | None = None) -> dict[str, float | str]:
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


def confidence_from_score_backtest(rows: pd.DataFrame, current_proximity: dict[str, float | str], horizon_bars: int = 12) -> dict[str, float | str]:
    samples: list[float] = []
    wins = 0
    for index in range(200, max(200, len(rows) - horizon_bars)):
        row = rows.iloc[index]
        score = float(signal_proximity(row)["buy_score"])
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


def _buy_signal(row: pd.Series) -> bool:
    close = float(row["close"])
    return (
        close > float(row["ema20"]) > float(row["ema50"])
        and close > float(row["vwap"])
        and float(row["rvol30"]) >= 0.9
        and float(row["delta_ratio"]) > 0.02
        and 0.001 <= float(row["volatility"]) <= 0.08
    )


def _watch_signal(row: pd.Series) -> bool:
    close = float(row["close"])
    score = 0
    score += close > float(row["ema20"])
    score += float(row["ema20"]) >= float(row["ema50"])
    score += close >= float(row["vwap"]) * 0.995
    score += float(row["rvol30"]) >= 0.7
    score += float(row["delta_ratio"]) > -0.05
    return score >= 3


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
