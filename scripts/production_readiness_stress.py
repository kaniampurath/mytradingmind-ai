from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from statistics import mean

from aegis_trader.analytics.strategy_reports import aggregate_strategy_matrix, run_strategy_matrix
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY, active_strategy_names


STRESS_SCENARIOS = {
    "base": {"pnl_multiplier": 1.00, "drawdown_multiplier": 1.00},
    "fee_slippage_x2": {"pnl_multiplier": 0.82, "drawdown_multiplier": 1.12},
    "spread_expansion": {"pnl_multiplier": 0.74, "drawdown_multiplier": 1.25},
    "liquidity_thin": {"pnl_multiplier": 0.68, "drawdown_multiplier": 1.40},
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run production readiness stress scenarios for selected strategy plugins.")
    parser.add_argument("--strategy-set", choices=["active", "all"], default="active")
    parser.add_argument("--strategies", default="", help="Comma-separated strategy names. Overrides --strategy-set.")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--interval", default="1h")
    args = parser.parse_args()

    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    effective_strategy_set = "custom" if args.strategies.strip() else args.strategy_set
    strategy_names = select_strategy_names(args.strategy_set, args.strategies)
    matrix = run_strategy_matrix(strategy_names, days=args.days, interval=args.interval)
    aggregate = aggregate_strategy_matrix(matrix)
    if matrix.empty or aggregate.empty:
        result = {
            "status": "FAIL",
            "score": 0,
            "reason": "No local Binance feature files were available for stress testing.",
        }
        print(json.dumps(result, indent=2))
        raise SystemExit(1)

    scoreable = aggregate[aggregate["trades"] > 0].copy()
    if scoreable.empty:
        result = {
            "status": "FAIL",
            "score": 0,
            "reason": "No registered strategies generated trades for stress testing.",
        }
        print(json.dumps(result, indent=2))
        raise SystemExit(1)

    scenarios = [_scenario_report(name, cfg, scoreable) for name, cfg in STRESS_SCENARIOS.items()]
    hard_failures = [
        item
        for item in scenarios
        if item["profit_factor"] < 1.0 or item["max_drawdown_pct"] >= 20.0 or item["win_rate"] < 35.0
    ]
    score = _institutional_score(scenarios)
    status = "PASS" if not hard_failures and score >= 75 else "WARN" if score >= 60 else "FAIL"
    result = {
        "status": status,
        "score": score,
        "strategies_tested": int(scoreable["strategy"].nunique()),
        "strategies_observed": int(aggregate["strategy"].nunique()),
        "strategy_set": effective_strategy_set,
        "strategies": strategy_names,
        "days": args.days,
        "interval": args.interval,
        "strategies_excluded_no_trades": sorted(str(item) for item in aggregate.loc[aggregate["trades"] <= 0, "strategy"].tolist()),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "remediation": _remediation(hard_failures),
    }
    report_name = "production_readiness_stress.json" if effective_strategy_set == "active" else "production_readiness_stress_custom.json"
    (out_dir / report_name).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if status in {"PASS", "WARN"} else 1)


def select_strategy_names(strategy_set: str, strategy_names: str = "") -> list[str]:
    if strategy_names.strip():
        requested = [item.strip() for item in strategy_names.split(",") if item.strip()]
    elif strategy_set == "active":
        requested = active_strategy_names()
    elif strategy_set == "all":
        requested = list(STRATEGY_REGISTRY)
    else:
        raise ValueError(f"unsupported strategy set: {strategy_set}")

    missing = [name for name in requested if name not in STRATEGY_REGISTRY]
    if missing:
        raise ValueError("unknown strategy names: " + ", ".join(missing))
    if not requested:
        raise ValueError("no strategies selected for production readiness stress")
    return requested


def _scenario_report(name: str, config: dict[str, float], aggregate) -> dict[str, float | str | int]:
    pnl = aggregate["total_pnl"] * config["pnl_multiplier"]
    drawdown = aggregate["max_drawdown_pct"] * config["drawdown_multiplier"]
    return {
        "scenario": name,
        "strategies": int(len(aggregate)),
        "total_pnl": round(float(pnl.sum()), 2),
        "win_rate": round(float(mean(aggregate["win_rate"])), 2),
        "profit_factor": round(float(max(0.0, 1.0 + (pnl.sum() / max(1.0, abs(pnl[pnl < 0].sum()) + 1000.0)))), 2),
        "max_drawdown_pct": round(float(drawdown.max()), 2),
        "confidence_score": round(float(mean(aggregate["confidence_score"])), 2),
    }


def _institutional_score(scenarios: list[dict[str, float | str | int]]) -> int:
    scores: list[float] = []
    for scenario in scenarios:
        pf = float(scenario["profit_factor"])
        dd = float(scenario["max_drawdown_pct"])
        wr = float(scenario["win_rate"])
        confidence = float(scenario["confidence_score"])
        scenario_score = 0.0
        scenario_score += min(30.0, pf / 1.3 * 30.0)
        scenario_score += max(0.0, 30.0 - max(0.0, dd - 8.0) * 2.0)
        scenario_score += min(20.0, wr / 50.0 * 20.0)
        scenario_score += min(20.0, confidence / 100.0 * 20.0)
        scores.append(scenario_score)
    return int(round(mean(scores)))


def _remediation(failures: list[dict[str, float | str | int]]) -> list[str]:
    if not failures:
        return ["Maintain testnet-only deployment until execution fills, restart recovery, and kill-switch drills are verified."]
    guidance: list[str] = []
    for failure in failures:
        if float(failure["max_drawdown_pct"]) >= 20.0:
            guidance.append(f"{failure['scenario']}: tighten exposure caps and pause strategies during volatility expansion.")
        if float(failure["profit_factor"]) < 1.0:
            guidance.append(f"{failure['scenario']}: increase orderflow confirmation threshold before deployment.")
        if float(failure["win_rate"]) < 35.0:
            guidance.append(f"{failure['scenario']}: reduce overtrading by raising watch-to-buy transition score.")
    return guidance


if __name__ == "__main__":
    main()
