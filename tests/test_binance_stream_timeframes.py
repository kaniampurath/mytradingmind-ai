from __future__ import annotations

from aegis_trader.market_data.binance_stream import BinanceStreamState


def _kline_message(open_ms: int, close: float, closed: bool = True) -> dict[str, object]:
    return {
        "data": {
            "e": "kline",
            "E": open_ms + 60_000,
            "s": "BTCUSDT",
            "k": {
                "t": open_ms,
                "T": open_ms + 59_999,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "100.0",
                "h": str(max(100.0, close)),
                "l": "99.0",
                "c": str(close),
                "v": "2.0",
                "q": str(close * 2),
                "n": 3,
                "V": "1.0",
                "Q": str(close),
                "x": closed,
            },
        }
    }


def test_stream_accumulates_closed_1m_klines_into_5m_timeframe() -> None:
    state = BinanceStreamState(["BTC/USDT"])
    base_ms = 1_704_067_200_000

    for offset in range(5):
        state.handle(_kline_message(base_ms + (offset * 60_000), 100.0 + offset))

    symbol_state = state.states["BTC/USDT"]
    assert symbol_state.timeframes["1m"]["close"] == 104.0
    assert symbol_state.timeframes["5m"]["open"] == 100.0
    assert symbol_state.timeframes["5m"]["close"] == 104.0
    assert symbol_state.timeframes["5m"]["volume"] == 10.0
    assert symbol_state.timeframes["5m"]["closed"] is True
    assert len(symbol_state.public_dict()["timeframe_history"]["1m"]) == 5
    assert len(symbol_state.public_dict()["timeframe_history"]["5m"]) == 1
