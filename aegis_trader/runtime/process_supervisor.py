from __future__ import annotations

from aegis_trader.runtime.command_bus import RuntimeCommand, RuntimeCommandBus


class ProcessSupervisor:
    """Lightweight process supervisor facade for local and Docker startup checks."""

    def __init__(self, bus: RuntimeCommandBus | None = None) -> None:
        self.bus = bus or RuntimeCommandBus()

    def start(self, mode: str = "HEADLESS") -> bool:
        return self.bus.dispatch(RuntimeCommand("START_RUNTIME", payload={"mode": mode}, source="SUPERVISOR")).ok

    def stop(self) -> bool:
        return self.bus.dispatch(RuntimeCommand("STOP_RUNTIME", source="SUPERVISOR")).ok
