from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.events import Signal
from aegis_trader.market_data.models import MarketTick


@dataclass(frozen=True)
class OrderflowSnapshot:
    symbol: str
    cvd: float
    delta_imbalance: float
    sweeps: int
    absorption: float
    tape_acceleration: float
    liquidity_vacuum: bool
    spread_bps: float
    liquidity_score: float


class OrderflowEngine:
    def from_tick(self, tick: MarketTick) -> OrderflowSnapshot:
        imbalance = 1.0 if tick.price >= tick.ask else -1.0 if tick.price <= tick.bid else 0.0
        return OrderflowSnapshot(
            symbol=tick.symbol,
            cvd=tick.quantity * imbalance,
            delta_imbalance=imbalance,
            sweeps=1 if tick.quantity > 10 else 0,
            absorption=0.0,
            tape_acceleration=min(1.0, tick.quantity / 50),
            liquidity_vacuum=tick.spread_bps > 30,
            spread_bps=tick.spread_bps,
            liquidity_score=max(0.0, 1.0 - tick.spread_bps / 100),
        )


class OrderflowVerifier:
    def verify(self, signal: Signal, snapshot: OrderflowSnapshot) -> tuple[bool, str]:
        if snapshot.liquidity_vacuum:
            return False, "liquidity vacuum"
        if snapshot.spread_bps > 20:
            return False, "spread too wide"
        if signal.side == "buy" and snapshot.delta_imbalance < -0.75:
            return False, "hostile delta imbalance"
        return True, "orderflow verified"
