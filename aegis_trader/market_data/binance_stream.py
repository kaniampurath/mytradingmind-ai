from __future__ import annotations

import asyncio
import json
import math
import ssl
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from aegis_trader.analytics.replay_metrics import TOP_TRADING_SYMBOLS


@dataclass
class StreamSymbolState:
    symbol: str
    last_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    depth_imbalance: float = 0.0
    taker_buy_ratio: float = 0.5
    trade_count: int = 0
    trade_notional: float = 0.0
    closed_kline: bool = False
    kline_interval: str = "1m"
    kline_close: float = 0.0
    kline_volume: float = 0.0
    orderflow_score: float = 0.0
    event_time: str = ""
    updated_at: str = ""
    status: str = "WAITING"
    _trades: deque[tuple[float, float, bool]] = field(default_factory=lambda: deque(maxlen=500), repr=False)

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("_trades", None)
        return payload

    def recompute(self) -> None:
        mid = (self.best_bid + self.best_ask) / 2
        self.spread_bps = 0.0 if mid <= 0 else (self.best_ask - self.best_bid) / mid * 10_000
        total_depth = self.bid_depth + self.ask_depth
        self.depth_imbalance = 0.0 if total_depth <= 0 else (self.bid_depth - self.ask_depth) / total_depth
        total_notional = sum(price * qty for price, qty, _ in self._trades)
        taker_buy_notional = sum(price * qty for price, qty, is_buyer_taker in self._trades if not is_buyer_taker)
        self.trade_count = len(self._trades)
        self.trade_notional = total_notional
        self.taker_buy_ratio = 0.5 if total_notional <= 0 else taker_buy_notional / total_notional
        self.orderflow_score = _orderflow_score(self.spread_bps, self.depth_imbalance, self.taker_buy_ratio, self.trade_count)
        self.updated_at = datetime.now(UTC).isoformat()
        self.status = "STREAMING"


class BinanceStreamState:
    def __init__(self, symbols: list[str] | tuple[str, ...]) -> None:
        self.symbols = list(symbols)
        self.states = {symbol: StreamSymbolState(symbol=symbol) for symbol in self.symbols}
        self.connected_at = datetime.now(UTC).isoformat()

    def handle(self, message: dict[str, Any]) -> None:
        data = message.get("data", message)
        event_type = data.get("e")
        symbol = _display_symbol(data.get("s", ""))
        if symbol not in self.states:
            return
        state = self.states[symbol]
        state.event_time = _event_time(data.get("E"))
        if event_type == "bookTicker" or "b" in data and "a" in data and "u" in data:
            state.best_bid = float(data.get("b", 0.0))
            state.best_ask = float(data.get("a", 0.0))
            state.last_price = state.last_price or (state.best_bid + state.best_ask) / 2
        elif event_type == "depthUpdate":
            bids = data.get("b", [])
            asks = data.get("a", [])
            state.bid_depth = sum(float(price) * float(qty) for price, qty in bids[:20])
            state.ask_depth = sum(float(price) * float(qty) for price, qty in asks[:20])
        elif event_type == "aggTrade":
            price = float(data.get("p", 0.0))
            qty = float(data.get("q", 0.0))
            state.last_price = price
            state._trades.append((price, qty, bool(data.get("m", False))))
        elif event_type == "kline":
            kline = data.get("k", {})
            state.kline_interval = str(kline.get("i", state.kline_interval))
            state.kline_close = float(kline.get("c", state.kline_close or 0.0))
            state.kline_volume = float(kline.get("v", 0.0))
            state.closed_kline = bool(kline.get("x", False))
            state.last_price = state.kline_close or state.last_price
        state.recompute()

    def as_payload(self, source: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "source": source,
            "connected_at": self.connected_at,
            "updated_at": now,
            "symbols": {symbol: state.public_dict() for symbol, state in self.states.items()},
        }


async def stream_binance(
    symbols: list[str] | tuple[str, ...] = TOP_TRADING_SYMBOLS,
    output: str | Path = "reports/live_stream.json",
    interval: str = "1m",
    base_url: str = "wss://stream.testnet.binance.vision",
    write_seconds: float = 2.0,
    insecure_ssl: bool = False,
) -> None:
    try:
        import certifi
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets and certifi are required for Binance stream mode. Run: pip install -e .") from exc

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stream_names = _stream_names(symbols, interval)
    url = f"{base_url.rstrip('/')}/stream?streams={'/'.join(stream_names)}"
    state = BinanceStreamState(symbols)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    tls_mode = "verified"
    if insecure_ssl:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        tls_mode = "insecure_dev"

    async def writer() -> None:
        while True:
            _write_json_atomic(output_path, {**state.as_payload(source=url), "tls_mode": tls_mode})
            await asyncio.sleep(write_seconds)

    while True:
        writer_task = asyncio.create_task(writer())
        try:
            async with websockets.connect(url, ssl=ssl_context, ping_interval=20, ping_timeout=20, close_timeout=5) as websocket:
                async for raw in websocket:
                    state.handle(json.loads(raw))
        except Exception as exc:
            _write_json_atomic(
                output_path,
                {
                    **state.as_payload(source=url),
                    "tls_mode": tls_mode,
                    "error": str(exc),
                    "status": "RECONNECTING",
                },
            )
            await asyncio.sleep(5)
        finally:
            writer_task.cancel()


def _stream_names(symbols: list[str] | tuple[str, ...], interval: str) -> list[str]:
    streams: list[str] = []
    for symbol in symbols:
        normalized = symbol.replace("/", "").lower()
        streams.extend(
            [
                f"{normalized}@bookTicker",
                f"{normalized}@depth20@100ms",
                f"{normalized}@aggTrade",
                f"{normalized}@kline_{interval}",
            ]
        )
    return streams


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    with NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=path.parent, suffix=".tmp") as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _display_symbol(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def _event_time(value: Any) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _orderflow_score(spread_bps: float, depth_imbalance: float, taker_buy_ratio: float, trade_count: int) -> float:
    if not math.isfinite(spread_bps):
        spread_bps = 99.0
    spread_component = max(0.0, min(1.0, 1 - (spread_bps / 20)))
    depth_component = max(0.0, min(1.0, (depth_imbalance + 1) / 2))
    taker_component = max(0.0, min(1.0, taker_buy_ratio))
    activity_component = max(0.0, min(1.0, trade_count / 500))
    return round(((spread_component * 0.25) + (depth_component * 0.25) + (taker_component * 0.35) + (activity_component * 0.15)) * 100, 1)
