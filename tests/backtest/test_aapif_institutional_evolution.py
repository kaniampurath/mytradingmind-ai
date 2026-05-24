from __future__ import annotations

import pandas as pd

from aegis_trader.strategies.aapif import (
    AAPIFCertificationState,
    AAPIFRegime,
    AegisAdaptivePortfolioOrchestrator,
    GAP_ANALYSIS_ARCHETYPES,
    MarketRegimeClassifier,
    STRATEGY_REGIME_MAP,
)
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY, active_strategy_names, dormant_strategy_names


INSTITUTIONAL_EVOLUTION_MAP = {
    "Aegis Adaptive Momentum Expansion Engine (AAMX-5)": "KCJ ATR Trend Burst 5m",
    "Aegis Statistical Volatility Reversion Engine (ASVR-1H)": "TradingView Mean Reversion ATR 1h",
    "Aegis Tactical Reversion Confirmation Engine (ATRC-10)": "TradingView Mean Reversion ATR 10m",
    "Aegis Directional Volatility Expansion Engine (ADVE)": "ATR Trend Burst",
    "Aegis Liquidity Reclaim Continuation Engine (ALRC)": "VWAP Reclaim",
    "Aegis Flow Momentum Alignment Engine (AFMA)": "Existing Momentum",
    "Aegis Quantitative Momentum Regime Engine (AQMR)": "Research Momentum Volatility",
    "Aegis Systematic Trend Persistence Engine (ASTP)": "Academic Time-Series Momentum",
    "Aegis Tactical Mean Reversion Engine (ATMR)": "Academic Short-Term Reversal",
    "Aegis Adaptive Portfolio Orchestration Engine (AAPO)": "Certified Risk Managed Composite",
}


def test_aapif_cloned_strategies_are_registered_as_shadow_dormant_variants() -> None:
    assert active_strategy_names() == ["KCJ ATR Trend Burst 5m", "TradingView Mean Reversion ATR 1h", "Certified Risk Managed Composite"]

    for evolved_name, baseline_name in INSTITUTIONAL_EVOLUTION_MAP.items():
        strategy = STRATEGY_REGISTRY[evolved_name]
        baseline = STRATEGY_REGISTRY[baseline_name]

        assert evolved_name in dormant_strategy_names()
        assert getattr(strategy, "baseline_strategy_name") == baseline_name
        assert getattr(strategy, "certification_state") == AAPIFCertificationState.SHADOW_MODE.value
        assert getattr(strategy, "shadow_validation_mode") is True
        assert getattr(strategy, "protected_baseline") is True
        assert strategy is not baseline


def test_market_regime_classifier_covers_required_institutional_regimes() -> None:
    required = {
        "TREND_EXPANSION",
        "VOLATILITY_BREAKOUT",
        "MEAN_REVERSION",
        "VOLATILITY_COMPRESSION",
        "PANIC",
        "LIQUIDITY_CRISIS",
        "LOW_VOLATILITY_CHOP",
        "MACRO_SHOCK",
        "LIQUIDATION_CASCADE",
        "TREND_EXHAUSTION",
        "FALSE_BREAKOUT",
        "RISK_OFF",
        "RISK_ON",
    }

    assert required.issubset({regime.value for regime in AAPIFRegime})


def test_regime_classifier_identifies_trend_expansion_from_candle_row() -> None:
    row = pd.Series(
        {
            "symbol": "BTC/USDT",
            "open": 100.0,
            "high": 106.0,
            "low": 99.0,
            "close": 105.0,
            "atr14": 2.0,
            "ema20": 103.0,
            "ema50": 101.0,
            "ema200": 95.0,
            "rvol30": 1.4,
            "delta_ratio": 0.08,
            "volatility": 0.018,
        }
    )

    snapshot = MarketRegimeClassifier().classify_row(row, None)

    assert snapshot.regime == AAPIFRegime.TREND_EXPANSION
    assert snapshot.confidence > 0.7


def test_aapo_orchestrator_blocks_crisis_regime_and_maps_components() -> None:
    orchestrator = AegisAdaptivePortfolioOrchestrator()
    crisis_row = pd.Series(
        {
            "symbol": "BTC/USDT",
            "open": 100.0,
            "high": 101.0,
            "low": 82.0,
            "close": 84.0,
            "atr14": 8.0,
            "ema20": 95.0,
            "ema50": 96.0,
            "ema200": 97.0,
            "rvol30": 3.0,
            "delta_ratio": -0.2,
            "volatility": 0.09,
        }
    )

    decision = orchestrator.shadow_decision(strategy_name="Aegis Adaptive Momentum Expansion Engine (AAMX-5)", row=crisis_row)

    assert decision["mode"] == AAPIFCertificationState.SHADOW_MODE.value
    assert decision["allowed"] is False
    assert STRATEGY_REGIME_MAP["Aegis Adaptive Portfolio Orchestration Engine (AAPO)"] == tuple(AAPIFRegime)


def test_gap_analysis_includes_missing_defensive_and_uncorrelated_archetypes() -> None:
    archetypes = {row["archetype"] for row in GAP_ANALYSIS_ARCHETYPES}

    assert "market-neutral" in archetypes
    assert "basis arbitrage" in archetypes
    assert "crisis alpha" in archetypes
    assert "tail-risk hedging" in archetypes
