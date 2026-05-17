from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis_trader.core.enums import EventType


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: EventType
    sequence: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    symbol: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    replay: bool = False

    def with_sequence(self, sequence: int) -> "Event":
        return self.model_copy(update={"sequence": sequence})


class Signal(BaseModel):
    strategy: str
    symbol: str
    side: str = "buy"
    confidence: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    notional: float
    reason: str


class DAVerdict(BaseModel):
    concern_score: float
    veto: bool
    size_multiplier: float
    reasons: list[str]


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    adjusted_notional: float = 0.0
    kill_switch: bool = False


class OrderIntent(BaseModel):
    symbol: str
    side: str
    quantity: float
    notional: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    client_order_id: str
