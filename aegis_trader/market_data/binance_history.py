from __future__ import annotations

import asyncio
import csv
import json
import ssl
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


@dataclass(frozen=True)
class BinanceKline:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime
    quote_asset_volume: float
    trade_count: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float

    @classmethod
    def from_api(cls, row: list[Any]) -> "BinanceKline":
        return cls(
            open_time=datetime.fromtimestamp(int(row[0]) / 1000, UTC),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=datetime.fromtimestamp(int(row[6]) / 1000, UTC),
            quote_asset_volume=float(row[7]),
            trade_count=int(row[8]),
            taker_buy_base_volume=float(row[9]),
            taker_buy_quote_volume=float(row[10]),
        )

    def as_dict(self, symbol: str, interval: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "interval": interval,
            "open_time": self.open_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "close_time": self.close_time.isoformat(),
            "quote_asset_volume": self.quote_asset_volume,
            "trade_count": self.trade_count,
            "taker_buy_base_volume": self.taker_buy_base_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
        }


@dataclass(frozen=True)
class BinanceMarketSnapshot:
    symbol: str
    best_bid: float
    best_ask: float
    spread_bps: float
    bid_depth: float
    ask_depth: float
    depth_imbalance: float
    agg_trade_count: int
    agg_trade_notional: float
    taker_buy_ratio: float
    orderflow_score: float
    captured_at: datetime


class BinanceHistoricalClient:
    def __init__(self, base_url: str = "https://api.binance.com", transport: str = "python") -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[BinanceKline]:
        if interval not in BINANCE_INTERVAL_MS:
            raise ValueError(f"unsupported Binance interval: {interval}")

        normalized_symbol = symbol.replace("/", "").upper()
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        interval_ms = BINANCE_INTERVAL_MS[interval]
        output: list[BinanceKline] = []

        while start_ms < end_ms:
            params = {
                "symbol": normalized_symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            }
            rows = await asyncio.to_thread(self._get_json, "/api/v3/klines", params)
            if not rows:
                break
            chunk = [BinanceKline.from_api(row) for row in rows]
            output.extend(chunk)
            next_start = int(rows[-1][0]) + interval_ms
            if next_start <= start_ms:
                break
            start_ms = next_start
            await asyncio.sleep(0.08)
        return output

    async def fetch_market_snapshot(self, symbol: str, depth_limit: int = 100) -> BinanceMarketSnapshot:
        normalized_symbol = symbol.replace("/", "").upper()
        book_ticker, depth, agg_trades = await asyncio.gather(
            asyncio.to_thread(self._get_json, "/api/v3/ticker/bookTicker", {"symbol": normalized_symbol}),
            asyncio.to_thread(self._get_json, "/api/v3/depth", {"symbol": normalized_symbol, "limit": depth_limit}),
            asyncio.to_thread(self._get_json, "/api/v3/aggTrades", {"symbol": normalized_symbol, "limit": 500}),
        )
        best_bid = float(book_ticker["bidPrice"])
        best_ask = float(book_ticker["askPrice"])
        mid = (best_bid + best_ask) / 2
        spread_bps = 0.0 if mid <= 0 else (best_ask - best_bid) / mid * 10_000
        bid_depth = sum(float(price) * float(quantity) for price, quantity in depth.get("bids", []))
        ask_depth = sum(float(price) * float(quantity) for price, quantity in depth.get("asks", []))
        total_depth = bid_depth + ask_depth
        depth_imbalance = 0.0 if total_depth == 0 else (bid_depth - ask_depth) / total_depth
        trade_notional = sum(float(trade["p"]) * float(trade["q"]) for trade in agg_trades)
        taker_buy_notional = sum(float(trade["p"]) * float(trade["q"]) for trade in agg_trades if not bool(trade.get("m")))
        taker_buy_ratio = 0.5 if trade_notional == 0 else taker_buy_notional / trade_notional
        orderflow_score = _orderflow_score(spread_bps, depth_imbalance, taker_buy_ratio, len(agg_trades))
        return BinanceMarketSnapshot(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            depth_imbalance=depth_imbalance,
            agg_trade_count=len(agg_trades),
            agg_trade_notional=trade_notional,
            taker_buy_ratio=taker_buy_ratio,
            orderflow_score=orderflow_score,
            captured_at=datetime.now(UTC),
        )

    def _get_json(self, path: str, params: dict[str, Any]) -> list[Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        if self.transport == "powershell":
            return self._get_json_with_powershell(url)
        request = Request(url, headers={"User-Agent": "mytradingmind.ai/2.1"})
        context = _ssl_context()
        with urlopen(request, timeout=30, context=context) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_json_with_powershell(self, url: str) -> list[Any]:
        safe_url = url.replace("'", "''")
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '{safe_url}' | Select-Object -ExpandProperty Content",
        ]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout)


def calculate_features(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes: list[float] = []
    volumes: list[float] = []
    ranges: list[float] = []
    enriched: list[dict[str, Any]] = []
    ema20 = ema50 = ema200 = None
    cumulative_quote = 0.0
    cumulative_volume = 0.0

    for row in rows:
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        volume = float(row["volume"])
        quote_volume = float(row["quote_asset_volume"])
        closes.append(close)
        volumes.append(volume)
        ranges.append(high - low)
        cumulative_quote += quote_volume
        cumulative_volume += volume
        ema20 = _ema_next(ema20, close, 20)
        ema50 = _ema_next(ema50, close, 50)
        ema200 = _ema_next(ema200, close, 200)
        atr = sum(ranges[-14:]) / max(1, min(14, len(ranges)))
        avg_volume = sum(volumes[-30:]) / max(1, min(30, len(volumes)))
        taker_buy = float(row["taker_buy_base_volume"])
        delta_ratio = 0.0 if volume == 0 else ((taker_buy * 2) - volume) / volume
        enriched.append(
            {
                **row,
                "atr14": atr,
                "ema20": ema20,
                "ema50": ema50,
                "ema200": ema200,
                "vwap": 0.0 if cumulative_volume == 0 else cumulative_quote / cumulative_volume,
                "rvol30": 0.0 if avg_volume == 0 else volume / avg_volume,
                "volatility": 0.0 if close == 0 else atr / close,
                "delta_ratio": delta_ratio,
            }
        )
    return enriched


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_parquet_if_available(path: Path, rows: list[dict[str, Any]]) -> bool:
    try:
        import pandas as pd
    except ImportError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return True


def default_history_window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(days=days), end


def _ema_next(previous: float | None, value: float, period: int) -> float:
    if previous is None:
        return value
    alpha = 2 / (period + 1)
    return (value * alpha) + (previous * (1 - alpha))


def _orderflow_score(spread_bps: float, depth_imbalance: float, taker_buy_ratio: float, trade_count: int) -> float:
    spread_component = max(0.0, min(1.0, 1 - (spread_bps / 20)))
    depth_component = max(0.0, min(1.0, (depth_imbalance + 1) / 2))
    taker_component = max(0.0, min(1.0, taker_buy_ratio))
    activity_component = max(0.0, min(1.0, trade_count / 500))
    return round(((spread_component * 0.25) + (depth_component * 0.25) + (taker_component * 0.35) + (activity_component * 0.15)) * 100, 1)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
