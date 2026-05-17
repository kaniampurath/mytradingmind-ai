from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from aegis_trader.market_data.models import Bar, MarketTick


@dataclass
class FeatureSnapshot:
    symbol: str
    atr: float
    ema20: float
    ema50: float
    ema200: float
    vwap: float
    rvol: float
    volatility: float
    spread_bps: float
    liquidity_depth: float
    microstructure_score: float


@dataclass
class FeatureEngine:
    bars: dict[str, deque[Bar]] = field(default_factory=dict)

    def on_bar(self, bar: Bar) -> FeatureSnapshot:
        history = self.bars.setdefault(bar.symbol, deque(maxlen=250))
        history.append(bar)
        closes = [item.close for item in history]
        ranges = [item.high - item.low for item in history]
        volume = [item.volume for item in history]
        atr = sum(ranges[-14:]) / max(1, min(14, len(ranges)))
        vwap_denominator = sum(volume[-20:]) or 1.0
        vwap = sum(c * v for c, v in zip(closes[-20:], volume[-20:], strict=False)) / vwap_denominator
        avg_volume = sum(volume[:-1] or [bar.volume]) / max(1, len(volume[:-1] or [bar.volume]))
        return FeatureSnapshot(
            symbol=bar.symbol,
            atr=atr,
            ema20=_ema(closes, 20),
            ema50=_ema(closes, 50),
            ema200=_ema(closes, 200),
            vwap=vwap,
            rvol=bar.volume / avg_volume if avg_volume else 1.0,
            volatility=atr / bar.close if bar.close else 0.0,
            spread_bps=0.0,
            liquidity_depth=1.0,
            microstructure_score=0.5,
        )

    def enrich_tick(self, tick: MarketTick, snapshot: FeatureSnapshot) -> FeatureSnapshot:
        return FeatureSnapshot(**{**snapshot.__dict__, "spread_bps": tick.spread_bps})


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema
