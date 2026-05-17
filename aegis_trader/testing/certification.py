from __future__ import annotations

from dataclasses import dataclass

from aegis_trader.core.enums import CertificationState


@dataclass(frozen=True)
class CertificationMetrics:
    sharpe: float
    profit_factor: float
    max_drawdown_pct: float
    replay_determinism: float
    risk_violations: int


@dataclass(frozen=True)
class CertificationReport:
    state: CertificationState
    reasons: list[str]


class CertificationEngine:
    def certify(self, metrics: CertificationMetrics) -> CertificationReport:
        reasons: list[str] = []
        if metrics.sharpe <= 1.2:
            reasons.append("Sharpe <= 1.2")
        if metrics.profit_factor <= 1.3:
            reasons.append("profit factor <= 1.3")
        if metrics.max_drawdown_pct >= 12:
            reasons.append("max drawdown >= 12%")
        if metrics.replay_determinism != 100:
            reasons.append("replay determinism below 100%")
        if metrics.risk_violations != 0:
            reasons.append("risk violations detected")
        if not reasons:
            return CertificationReport(CertificationState.CERTIFIED, [])
        if metrics.risk_violations or metrics.replay_determinism != 100:
            return CertificationReport(CertificationState.REJECTED, reasons)
        return CertificationReport(CertificationState.CONDITIONAL, reasons)
