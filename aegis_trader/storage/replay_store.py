from __future__ import annotations

from dataclasses import dataclass, field

from aegis_trader.core.events import Event


@dataclass
class InMemoryReplayStore:
    events: list[Event] = field(default_factory=list)

    async def append(self, event: Event) -> None:
        self.events.append(event)

    async def load(self) -> list[Event]:
        return sorted(self.events, key=lambda event: event.sequence)
