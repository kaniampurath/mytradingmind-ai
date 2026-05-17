from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.risk.engine import PortfolioRiskEngine


@dataclass
class FailureInjector:
    risk: PortfolioRiskEngine

    def stale_feed(self) -> None:
        self.risk.trigger_kill_switch("stale feeds")

    def oms_desync(self) -> None:
        self.risk.trigger_kill_switch("OMS desync")

    def queue_overload(self) -> None:
        self.risk.trigger_kill_switch("queue overload")
