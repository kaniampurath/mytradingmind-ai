from __future__ import annotations

import asyncio
import signal

from aegis_trader.runtime.runtime_manager import RuntimeManager


async def run_runtime(mode: str, heartbeat_seconds: float, once: bool = False) -> None:
    manager = RuntimeManager()
    manager.start_runtime(mode)
    stop_event = asyncio.Event()

    def request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    while not stop_event.is_set():
        if str(manager.runtime_status().get("runtime", "STOPPED")) == "STOPPED":
            break
        manager.runtime_heartbeat(mode)
        for state in manager.list_bot_states():
            if state.get("status") == "RUNNING":
                manager.start_bot(str(state["bot_id"]), source="HEADLESS_HEARTBEAT")
        if once:
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=heartbeat_seconds)
        except TimeoutError:
            continue

    if stop_event.is_set():
        manager.stop_runtime()
