from __future__ import annotations

from aegis_trader.market_data.binance_history import BinanceKline, calculate_features


def test_binance_kline_parsing_and_feature_calculation() -> None:
    row = [
        1710000000000,
        "100.0",
        "110.0",
        "95.0",
        "105.0",
        "12.0",
        1710003599999,
        "1260.0",
        42,
        "7.0",
        "735.0",
        "0",
    ]

    kline = BinanceKline.from_api(row)
    features = calculate_features([kline.as_dict("BTC/USDT", "1h")])

    assert features[0]["atr14"] == 15.0
    assert features[0]["ema20"] == 105.0
    assert features[0]["vwap"] == 105.0
    assert round(features[0]["delta_ratio"], 4) == 0.1667
