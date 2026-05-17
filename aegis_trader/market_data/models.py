from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class MarketTick(BaseModel):
    symbol: str
    price: float
    quantity: float
    bid: float
    ask: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return 0.0 if mid <= 0 else ((self.ask - self.bid) / mid) * 10_000


class Bar(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    start: datetime
    end: datetime
    closed: bool = True


class FeedHealth(BaseModel):
    symbol: str
    stale: bool
    latency_ms: float
    reason: str = ""
