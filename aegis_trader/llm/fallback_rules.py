from __future__ import annotations

from typing import Any


def rule_based_trade_review(context: dict[str, Any]) -> dict[str, Any]:
    spread = float(context.get("spread_bps", 0.0) or 0.0)
    liquidity = float(context.get("liquidity_score", 1.0) or 0.0)
    orderflow = float(context.get("orderflow_score", 0.0) or 0.0)
    concern = 0.12
    concerns: list[str] = []
    if spread > 12:
        concern += 0.18
        concerns.append("SPREAD_WIDE")
    if liquidity < 0.35:
        concern += 0.18
        concerns.append("LIQUIDITY_THIN")
    if orderflow < 35:
        concern += 0.16
        concerns.append("ORDERFLOW_WEAK")
    action = "REJECT" if concern > 0.35 else "ALLOW"
    return {
        "approved": action == "ALLOW",
        "concern_score": round(min(concern, 1.0), 3),
        "concerns": concerns or ["RULES_CLEAR"],
        "summary": "Rule-based fallback review used. " + ("Setup is blocked by risk concerns." if action == "REJECT" else "Setup has no major rule-based blockers."),
        "action": action,
    }
