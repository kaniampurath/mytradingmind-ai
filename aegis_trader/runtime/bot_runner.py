from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aegis_trader.runtime.runtime_manager import RuntimeManager


@dataclass
class HeadlessBotRunner:
    bot_id: str
    heartbeat_seconds: float = 5.0

    async def run_forever(self) -> None:
        manager = RuntimeManager()
        manager.start_bot(self.bot_id, source="HEADLESS")
        while True:
            manager.runtime_heartbeat("HEADLESS")
            await asyncio.sleep(self.heartbeat_seconds)
