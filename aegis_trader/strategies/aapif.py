from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pandas as pd


class AAPIFRegime(StrEnum):
    TREND_EXPANSION = "TREND_EXPANSION"
    VOLATILITY_BREAKOUT = "VOLATILITY_BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"
    VOLATILITY_COMPRESSION = "VOLATILITY_COMPRESSION"
    PANIC = "PANIC"
    LIQUIDITY_CRISIS = "LIQUIDITY_CRISIS"
    LOW_VOLATILITY_CHOP = "LOW_VOLATILITY_CHOP"
    MACRO_SHOCK = "MACRO_SHOCK"
    LIQUIDATION_CASCADE = "LIQUIDATION_CASCADE"
    TREND_EXHAUSTION = "TREND_EXHAUSTION"
    FALSE_BREAKOUT = "FALSE_BREAKOUT"
    RISK_OFF = "RISK_OFF"
    RISK_ON = "RISK_ON"


class AAPIFCertificationState(StrEnum):
    RESEARCH = "RESEARCH"
    EXPERIMENTAL = "EXPERIMENTAL"
    PAPER_TEST = "PAPER_TEST"
    SHADOW_MODE = "SHADOW_MODE"
    STRESS_VALIDATED = "STRESS_VALIDATED"
    CERTIFIED = "CERTIFIED"
    PRODUCTION = "PRODUCTION"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"
    BASELINE_REFERENCE = "BASELINE_REFERENCE"
    LEGACY_PRODUCTION = "LEGACY_PRODUCTION"


@dataclass(frozen=True)
class MarketRegimeSnapshot:
    regime: AAPIFRegime
    confidence: float
    reason: str
    volatility: float
    liquidity_score: float
    trend_score: float
    shock_score: float


@dataclass(frozen=True)
class ExecutionRealismEstimate:
    spread_bps: float
    slippage_bps: float
    latency_bps: float
    impact_bps: float
    outage_penalty_bps: float
    partial_fill_probability: float
    rejection_probability: float
    live_backtest_degradation_bps: float
    execution_fragility_score: float
    liquidity_sensitivity: float
    scalability_limit_notional: float


@dataclass(frozen=True)
class PortfolioSizingDecision:
    strategy_name: str
    symbol: str
    target_weight: float
    volatility_scaled_weight: float
    drawdown_throttle: float
    correlation_throttle: float
    regime_throttle: float
    final_weight: float
    reason: str


@dataclass(frozen=True)
class SurvivabilityAssessment:
    strategy_name: str
    survivability_score: float
    anti_fragility_score: float
    drawdown_cluster_risk: float
    liquidity_crisis_risk: float
    recommendation: str


@dataclass(frozen=True)
class InstitutionalMetrics:
    geometric_cagr: float
    max_drawdown_pct: float
    mar_ratio: float
    sharpe_ratio: float
    sortino_ratio: float
    ulcer_index: float
    recovery_factor: float
    volatility_drag: float
    live_backtest_parity: float
    survivability_score: float
    anti_fragility_score: float
    portfolio_contribution_score: float


class MarketRegimeClassifier:
    """Row-based institutional regime classifier for replay and shadow validation."""

    def classify_row(self, row: pd.Series, previous: pd.Series | None = None) -> MarketRegimeSnapshot:
        close = _safe_float(row.get("close"))
        open_price = _safe_float(row.get("open"), close)
        high = _safe_float(row.get("high"), close)
        low = _safe_float(row.get("low"), close)
        atr = _safe_float(row.get("atr14"))
        volatility = _safe_float(row.get("volatility"), atr / close if close > 0 else 0.0)
        rvol = _safe_float(row.get("rvol30"), 1.0)
        delta = _safe_float(row.get("delta_ratio"))
        ema20 = _safe_float(row.get("ema20"), close)
        ema50 = _safe_float(row.get("ema50"), ema20)
        ema200 = _safe_float(row.get("ema200"), ema50)
        spread_bps = _safe_float(row.get("spread_bps"))
        orderflow = _safe_float(row.get("orderflow_score"), _proxy_orderflow_score(rvol, delta, volatility))
        candle_range = max(0.000001, high - low)
        bar_return = (close - open_price) / open_price if open_price > 0 else 0.0
        previous_return = 0.0
        if previous is not None:
            previous_close = _safe_float(previous.get("close"))
            previous_open = _safe_float(previous.get("open"), previous_close)
            previous_return = (previous_close - previous_open) / previous_open if previous_open > 0 else 0.0
        atr_frac = atr / close if close > 0 else volatility
        trend_score = _clamp(((ema20 / ema200) - 1.0) * 20.0 if ema200 > 0 else 0.0, -1.0, 1.0)
        liquidity_score = _clamp(1.0 - (spread_bps / 40.0), 0.0, 1.0) if spread_bps > 0 else _clamp(rvol / 2.0, 0.0, 1.0)
        shock_score = _clamp(max(abs(bar_return), abs(previous_return), atr_frac) * 30.0, 0.0, 1.0)

        if spread_bps >= 35 or liquidity_score <= 0.15:
            return MarketRegimeSnapshot(AAPIFRegime.LIQUIDITY_CRISIS, 0.86, "spread/liquidity crisis detected", volatility, liquidity_score, trend_score, shock_score)
        if shock_score >= 0.82 and close < open_price and delta < -0.08:
            return MarketRegimeSnapshot(AAPIFRegime.LIQUIDATION_CASCADE, 0.84, "bearish shock with adverse orderflow", volatility, liquidity_score, trend_score, shock_score)
        if volatility >= 0.055 or (atr_frac >= 0.045 and rvol >= 1.8):
            return MarketRegimeSnapshot(AAPIFRegime.PANIC, 0.82, "volatility spike exceeds survivability threshold", volatility, liquidity_score, trend_score, shock_score)
        if close > ema20 > ema50 > ema200 and rvol >= 1.1 and delta >= 0:
            return MarketRegimeSnapshot(AAPIFRegime.TREND_EXPANSION, 0.78, "trend stack and flow expansion aligned", volatility, liquidity_score, trend_score, shock_score)
        if abs(close - open_price) >= 1.4 * atr and close > open_price and rvol >= 1.0:
            return MarketRegimeSnapshot(AAPIFRegime.VOLATILITY_BREAKOUT, 0.74, "ATR expansion breakout candle", volatility, liquidity_score, trend_score, shock_score)
        if close > ema200 and close < ema20 and orderflow > 50:
            return MarketRegimeSnapshot(AAPIFRegime.MEAN_REVERSION, 0.68, "pullback inside bullish structure", volatility, liquidity_score, trend_score, shock_score)
        if volatility <= 0.004 or candle_range / close <= 0.004:
            return MarketRegimeSnapshot(AAPIFRegime.VOLATILITY_COMPRESSION, 0.66, "compressed range and volatility", volatility, liquidity_score, trend_score, shock_score)
        if close > ema200 and ema20 < ema50 and delta >= 0:
            return MarketRegimeSnapshot(AAPIFRegime.TREND_EXHAUSTION, 0.62, "trend above long average but short structure weakening", volatility, liquidity_score, trend_score, shock_score)
        if close < ema200 and delta < 0:
            return MarketRegimeSnapshot(AAPIFRegime.RISK_OFF, 0.68, "below long trend with negative flow", volatility, liquidity_score, trend_score, shock_score)
        if close > ema200 and delta >= 0:
            return MarketRegimeSnapshot(AAPIFRegime.RISK_ON, 0.64, "above long trend with supportive flow", volatility, liquidity_score, trend_score, shock_score)
        return MarketRegimeSnapshot(AAPIFRegime.LOW_VOLATILITY_CHOP, 0.55, "no durable directional or reversion edge", volatility, liquidity_score, trend_score, shock_score)


class ExecutionRealismModel:
    def estimate(self, row: pd.Series, *, notional: float = 1_000.0, latency_ms: float = 250.0) -> ExecutionRealismEstimate:
        close = _safe_float(row.get("close"))
        volume = max(0.0, _safe_float(row.get("volume")))
        quote_volume = max(close * volume, 1.0)
        volatility = _safe_float(row.get("volatility"), _safe_float(row.get("atr14")) / close if close > 0 else 0.0)
        rvol = _safe_float(row.get("rvol30"), 1.0)
        spread_bps = _safe_float(row.get("spread_bps"), 4.0 + min(20.0, volatility * 500.0))
        participation = _clamp(notional / quote_volume, 0.0, 1.0)
        slippage_bps = spread_bps * 0.5 + volatility * 2_000.0 * (1.0 / max(0.35, min(rvol, 3.0)))
        latency_bps = volatility * max(0.0, latency_ms) / 1_000.0 * 100.0
        impact_bps = 10_000.0 * math.sqrt(participation) * max(0.2, volatility)
        outage_penalty_bps = 8.0 if volatility >= 0.05 else 2.0 if volatility >= 0.025 else 0.5
        partial_fill_probability = _clamp(participation * 5.0 + max(0.0, spread_bps - 15.0) / 50.0, 0.0, 0.95)
        rejection_probability = _clamp(max(0.0, spread_bps - 25.0) / 100.0 + max(0.0, volatility - 0.04) * 4.0, 0.0, 0.8)
        degradation = spread_bps + slippage_bps + latency_bps + impact_bps + outage_penalty_bps
        fragility = _clamp(degradation / 160.0 + partial_fill_probability * 0.3 + rejection_probability * 0.4, 0.0, 1.0)
        liquidity_sensitivity = _clamp(participation * 20.0 + spread_bps / 40.0, 0.0, 1.0)
        scalability_limit = quote_volume * 0.002
        return ExecutionRealismEstimate(
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            latency_bps=latency_bps,
            impact_bps=impact_bps,
            outage_penalty_bps=outage_penalty_bps,
            partial_fill_probability=partial_fill_probability,
            rejection_probability=rejection_probability,
            live_backtest_degradation_bps=degradation,
            execution_fragility_score=fragility,
            liquidity_sensitivity=liquidity_sensitivity,
            scalability_limit_notional=scalability_limit,
        )


class AdaptivePositionSizer:
    def size(
        self,
        *,
        strategy_name: str,
        symbol: str,
        base_weight: float,
        volatility: float,
        regime: AAPIFRegime,
        current_drawdown_pct: float = 0.0,
        correlation: float = 0.0,
    ) -> PortfolioSizingDecision:
        volatility_scaled = base_weight * _clamp(0.015 / max(volatility, 0.001), 0.25, 1.5)
        drawdown_throttle = _clamp(1.0 - max(0.0, current_drawdown_pct) / 12.0, 0.1, 1.0)
        correlation_throttle = _clamp(1.0 - max(0.0, correlation - 0.55), 0.35, 1.0)
        regime_throttle = 0.0 if regime in CRISIS_REGIMES else 0.45 if regime in CAUTION_REGIMES else 1.0
        final = _clamp(volatility_scaled * drawdown_throttle * correlation_throttle * regime_throttle, 0.0, base_weight * 1.5)
        return PortfolioSizingDecision(
            strategy_name=strategy_name,
            symbol=symbol,
            target_weight=base_weight,
            volatility_scaled_weight=volatility_scaled,
            drawdown_throttle=drawdown_throttle,
            correlation_throttle=correlation_throttle,
            regime_throttle=regime_throttle,
            final_weight=final,
            reason=f"volatility/regime/correlation sizing for {regime}",
        )


class SurvivabilityEngine:
    def assess(self, *, strategy_name: str, max_drawdown_pct: float, execution_fragility: float, regime: AAPIFRegime, correlation: float = 0.0) -> SurvivabilityAssessment:
        crisis_penalty = 0.25 if regime in CRISIS_REGIMES else 0.1 if regime in CAUTION_REGIMES else 0.0
        drawdown_risk = _clamp(max_drawdown_pct / 20.0, 0.0, 1.0)
        cluster_risk = _clamp((drawdown_risk * 0.55) + (correlation * 0.3) + crisis_penalty, 0.0, 1.0)
        liquidity_risk = _clamp(execution_fragility + crisis_penalty, 0.0, 1.0)
        survivability = round(100.0 * (1.0 - _clamp((cluster_risk + liquidity_risk) / 2.0, 0.0, 1.0)), 2)
        anti_fragility = round(100.0 * (1.0 - _clamp(drawdown_risk + max(0.0, correlation - 0.4), 0.0, 1.0)), 2)
        recommendation = "shadow only" if survivability < 65 else "stress validation required" if survivability < 80 else "eligible for certification gates"
        return SurvivabilityAssessment(strategy_name, survivability, anti_fragility, cluster_risk, liquidity_risk, recommendation)


class AegisAdaptivePortfolioOrchestrator:
    def __init__(self) -> None:
        self.regime_classifier = MarketRegimeClassifier()
        self.execution_model = ExecutionRealismModel()
        self.sizer = AdaptivePositionSizer()
        self.survivability = SurvivabilityEngine()

    def strategy_allowed(self, strategy_name: str, regime: AAPIFRegime) -> bool:
        allowed = STRATEGY_REGIME_MAP.get(strategy_name, ())
        return not allowed or regime in allowed

    def shadow_decision(self, *, strategy_name: str, row: pd.Series, previous: pd.Series | None = None, base_weight: float = 0.1) -> dict[str, Any]:
        regime = self.regime_classifier.classify_row(row, previous)
        execution = self.execution_model.estimate(row)
        allowed = self.strategy_allowed(strategy_name, regime.regime)
        sizing = self.sizer.size(
            strategy_name=strategy_name,
            symbol=str(row.get("symbol", "")),
            base_weight=base_weight,
            volatility=regime.volatility,
            regime=regime.regime,
        )
        survivability = self.survivability.assess(
            strategy_name=strategy_name,
            max_drawdown_pct=0.0,
            execution_fragility=execution.execution_fragility_score,
            regime=regime.regime,
        )
        return {
            "strategy_name": strategy_name,
            "regime": regime,
            "execution": execution,
            "sizing": sizing,
            "survivability": survivability,
            "allowed": allowed and execution.execution_fragility_score <= 0.72 and sizing.final_weight > 0,
            "mode": AAPIFCertificationState.SHADOW_MODE.value,
        }


CRISIS_REGIMES = {
    AAPIFRegime.PANIC,
    AAPIFRegime.LIQUIDITY_CRISIS,
    AAPIFRegime.MACRO_SHOCK,
    AAPIFRegime.LIQUIDATION_CASCADE,
    AAPIFRegime.RISK_OFF,
}

CAUTION_REGIMES = {
    AAPIFRegime.FALSE_BREAKOUT,
    AAPIFRegime.TREND_EXHAUSTION,
    AAPIFRegime.LOW_VOLATILITY_CHOP,
}

STRATEGY_REGIME_MAP: dict[str, tuple[AAPIFRegime, ...]] = {
    "Aegis Adaptive Momentum Expansion Engine (AAMX-5)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.VOLATILITY_BREAKOUT, AAPIFRegime.RISK_ON),
    "Aegis Statistical Volatility Reversion Engine (ASVR-1H)": (AAPIFRegime.MEAN_REVERSION, AAPIFRegime.TREND_EXHAUSTION, AAPIFRegime.RISK_ON),
    "Aegis Tactical Reversion Confirmation Engine (ATRC-10)": (AAPIFRegime.MEAN_REVERSION, AAPIFRegime.TREND_EXHAUSTION, AAPIFRegime.FALSE_BREAKOUT),
    "Aegis Directional Volatility Expansion Engine (ADVE)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.VOLATILITY_BREAKOUT),
    "Aegis Liquidity Reclaim Continuation Engine (ALRC)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.RISK_ON),
    "Aegis Flow Momentum Alignment Engine (AFMA)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.RISK_ON),
    "Aegis Quantitative Momentum Regime Engine (AQMR)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.RISK_ON, AAPIFRegime.VOLATILITY_BREAKOUT),
    "Aegis Systematic Trend Persistence Engine (ASTP)": (AAPIFRegime.TREND_EXPANSION, AAPIFRegime.RISK_ON),
    "Aegis Tactical Mean Reversion Engine (ATMR)": (AAPIFRegime.MEAN_REVERSION, AAPIFRegime.TREND_EXHAUSTION),
    "Aegis Adaptive Portfolio Orchestration Engine (AAPO)": tuple(AAPIFRegime),
}

GAP_ANALYSIS_ARCHETYPES: tuple[dict[str, str], ...] = (
    {"archetype": "volatility breakout", "primary_value": "CAGR expansion", "status": "partially covered by AAMX-5/ADVE"},
    {"archetype": "volatility compression", "primary_value": "entry timing and drawdown reduction", "status": "missing dedicated compression breakout sleeve"},
    {"archetype": "market-neutral", "primary_value": "drawdown reduction", "status": "missing"},
    {"archetype": "statistical arbitrage", "primary_value": "uncorrelated CAGR", "status": "missing"},
    {"archetype": "funding-rate mean reversion", "primary_value": "crypto carry/reversion", "status": "missing"},
    {"archetype": "liquidity sweep reversal", "primary_value": "anti-fragility", "status": "future orderflow evolution"},
    {"archetype": "carry strategies", "primary_value": "capital efficiency", "status": "missing"},
    {"archetype": "basis arbitrage", "primary_value": "lower-volatility CAGR", "status": "missing"},
    {"archetype": "crisis alpha", "primary_value": "survivability", "status": "missing"},
    {"archetype": "defensive low-volatility systems", "primary_value": "drawdown containment", "status": "missing"},
    {"archetype": "tail-risk hedging", "primary_value": "catastrophic drawdown reduction", "status": "missing"},
)

EVOLUTION_ROADMAP: tuple[dict[str, tuple[str, ...]], ...] = (
    {"phase": ("PHASE 1", "accounting correctness", "replay/live parity", "execution realism", "risk consistency", "regression protection")},
    {"phase": ("PHASE 2", "regime engine", "portfolio allocator", "adaptive sizing", "portfolio risk engine")},
    {"phase": ("PHASE 3", "microstructure intelligence", "liquidity modeling", "execution optimization", "orderflow intelligence")},
    {"phase": ("PHASE 4", "adaptive orchestration", "self-learning allocation", "anti-fragile architecture", "portfolio optimization engine")},
)


def institutional_metrics_from_returns(returns_pct: list[float], max_drawdown_pct: float, live_degradation_bps: float = 0.0) -> InstitutionalMetrics:
    if not returns_pct:
        return InstitutionalMetrics(0.0, max_drawdown_pct, 0.0, 0.0, 0.0, max_drawdown_pct, 0.0, 0.0, 0.0, 50.0, 50.0, 0.0)
    returns = [value / 100.0 for value in returns_pct]
    compounded = 1.0
    for value in returns:
        compounded *= max(0.000001, 1.0 + value)
    geometric = (compounded - 1.0) * 100.0
    avg = sum(returns) / len(returns)
    variance = sum((value - avg) ** 2 for value in returns) / max(1, len(returns) - 1)
    stdev = math.sqrt(variance)
    downside = [min(0.0, value) for value in returns]
    downside_dev = math.sqrt(sum(value * value for value in downside) / max(1, len(downside)))
    sharpe = 0.0 if stdev == 0 else avg / stdev * math.sqrt(len(returns))
    sortino = 0.0 if downside_dev == 0 else avg / downside_dev * math.sqrt(len(returns))
    volatility_drag = max(0.0, ((1 + avg) ** len(returns) - compounded) * 100.0)
    recovery_factor = 0.0 if max_drawdown_pct <= 0 else geometric / max_drawdown_pct
    mar = 0.0 if max_drawdown_pct <= 0 else geometric / max_drawdown_pct
    parity = _clamp(1.0 - live_degradation_bps / 300.0, 0.0, 1.0) * 100.0
    survivability = _clamp(100.0 - max_drawdown_pct * 4.0 - live_degradation_bps / 8.0, 0.0, 100.0)
    anti_fragility = _clamp(50.0 + max(0.0, -min(returns)) * 300.0 - max_drawdown_pct * 2.0, 0.0, 100.0)
    contribution = _clamp((mar * 20.0) + (sortino * 10.0) + survivability * 0.4, 0.0, 100.0)
    return InstitutionalMetrics(
        geometric_cagr=geometric,
        max_drawdown_pct=max_drawdown_pct,
        mar_ratio=mar,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        ulcer_index=max_drawdown_pct,
        recovery_factor=recovery_factor,
        volatility_drag=volatility_drag,
        live_backtest_parity=parity,
        survivability_score=survivability,
        anti_fragility_score=anti_fragility,
        portfolio_contribution_score=contribution,
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _proxy_orderflow_score(rvol: float, delta: float, volatility: float) -> float:
    return _clamp(50.0 + (rvol - 1.0) * 18.0 + delta * 120.0 - volatility * 120.0, 0.0, 100.0)
