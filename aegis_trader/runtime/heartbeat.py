from __future__ import annotations

from aegis_trader.runtime.runtime_manager import RuntimeManager


def runtime_heartbeat() -> dict[str, object]:
    return RuntimeManager().runtime_status()
