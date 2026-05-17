from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.event_bus import EventBus
from aegis_trader.core.events import Event


@dataclass(frozen=True)
class ReplayResult:
    input_events: int
    output_events: int
    deterministic: bool


class ReplayEngine:
    async def run(self, events: list[Event]) -> ReplayResult:
        first_bus = EventBus()
        second_bus = EventBus()
        first = await first_bus.replay(events)
        second = await second_bus.replay(events)
        return ReplayResult(
            input_events=len(events),
            output_events=len(first),
            deterministic=[event.sequence for event in first] == [event.sequence for event in second],
        )
