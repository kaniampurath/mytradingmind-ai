from __future__ import annotations

from aegis_trader.core.enums import EventType
from aegis_trader.core.event_bus import EventBus
from aegis_trader.core.events import Event


async def test_event_bus_assigns_deterministic_sequences() -> None:
    seen: list[int] = []
    bus = EventBus()

    async def handler(event: Event) -> None:
        seen.append(event.sequence)

    bus.subscribe_all(handler)
    await bus.publish(Event(event_type=EventType.MARKET_TICK, symbol="BTC/USDT"))
    await bus.publish(Event(event_type=EventType.BAR_CLOSED, symbol="BTC/USDT"))

    await bus.drain_once()
    await bus.drain_once()

    assert seen == [1, 2]


async def test_replay_is_deterministic() -> None:
    bus = EventBus()
    events = [
        Event(event_type=EventType.MARKET_TICK, symbol="BTC/USDT"),
        Event(event_type=EventType.ORDERBOOK_UPDATE, symbol="BTC/USDT"),
    ]

    replayed = await bus.replay(events)

    assert [event.sequence for event in replayed] == [1, 2]
    assert all(event.replay for event in replayed)
