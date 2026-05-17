from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


WriteHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class AsyncWriter:
    handler: WriteHandler
    maxsize: int = 10_000
    queue: asyncio.Queue[dict[str, Any]] = field(init=False)
    running: bool = False

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.maxsize)

    def enqueue(self, record: dict[str, Any]) -> None:
        self.queue.put_nowait(record)

    async def run(self) -> None:
        self.running = True
        while self.running:
            record = await self.queue.get()
            await self.handler(record)
            self.queue.task_done()

    def stop(self) -> None:
        self.running = False
