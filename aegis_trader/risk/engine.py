from __future__ import annotations

from dataclasses import dataclass, field

from aegis_trader.core.events import RiskDecision, Signal


@dataclass
class RiskLimits:
    max_daily_loss_pct: float = 0.02
    max_position_notional: float = 250.0
    max_portfolio_exposure: float = 1000.0
    max_trades_per_day: int = 12
    consecutive_loss_lock: int = 3


@dataclass
class PortfolioRiskEngine:
    limits: RiskLimits
    kill_switch_active: bool = False
    daily_loss_pct: float = 0.0
    open_exposure: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    alerts: list[str] = field(default_factory=list)

    def approve(self, signal: Signal) -> RiskDecision:
        if self.kill_switch_active:
            return RiskDecision(approved=False, reason="kill switch active", kill_switch=True)
        if self.daily_loss_pct >= self.limits.max_daily_loss_pct:
            return self.trigger_kill_switch("daily loss lock")
        if self.trades_today >= self.limits.max_trades_per_day:
            return RiskDecision(approved=False, reason="max trades per day reached")
        if self.consecutive_losses >= self.limits.consecutive_loss_lock:
            return self.trigger_kill_switch("consecutive loss lock")
        if signal.notional > self.limits.max_position_notional:
            return RiskDecision(
                approved=True,
                reason="position notional reduced",
                adjusted_notional=self.limits.max_position_notional,
            )
        if self.open_exposure + signal.notional > self.limits.max_portfolio_exposure:
            return RiskDecision(approved=False, reason="portfolio exposure limit")
        return RiskDecision(approved=True, reason="risk approved", adjusted_notional=signal.notional)

    def trigger_kill_switch(self, reason: str) -> RiskDecision:
        self.kill_switch_active = True
        self.alerts.append(reason)
        return RiskDecision(approved=False, reason=reason, kill_switch=True)

    def record_submitted_trade(self, notional: float) -> None:
        self.trades_today += 1
        self.open_exposure += notional
