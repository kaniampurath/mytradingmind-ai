from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aegis_trader.core.enums import EventType
from aegis_trader.core.events import Event

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass
class EventBus:
    """Single deterministic async communication layer."""

    queue_maxsize: int = 10_000
    _sequence: int = 0
    _queue: asyncio.PriorityQueue[tuple[int, Event]] = field(init=False)
    _subscribers: dict[EventType, list[EventHandler]] = field(default_factory=lambda: defaultdict(list))
    _all_subscribers: list[EventHandler] = field(default_factory=list)
    _running: bool = False

    def __post_init__(self) -> None:
        self._queue = asyncio.PriorityQueue(maxsize=self.queue_maxsize)

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        self._all_subscribers.append(handler)

    async def publish(self, event: Event) -> Event:
        self._sequence += 1
        ordered = event.with_sequence(self._sequence)
        self._queue.put_nowait((ordered.sequence, ordered))
        return ordered

    async def drain_once(self) -> Event | None:
        if self._queue.empty():
            return None
        _, event = await self._queue.get()
        await self._dispatch(event)
        self._queue.task_done()
        return event

    async def run(self) -> None:
        self._running = True
        while self._running:
            _, event = await self._queue.get()
            await self._dispatch(event)
            self._queue.task_done()

    def stop(self) -> None:
        self._running = False

    async def replay(self, events: list[Event]) -> list[Event]:
        ordered: list[Event] = []
        for event in sorted(events, key=lambda item: (item.timestamp, item.sequence, item.event_id)):
            ordered.append(await self.publish(event.model_copy(update={"replay": True})))
        while not self._queue.empty():
            await self.drain_once()
        return ordered

    async def _dispatch(self, event: Event) -> None:
        handlers = [*self._all_subscribers, *self._subscribers.get(event.event_type, [])]
        for handler in handlers:
            await handler(event)
