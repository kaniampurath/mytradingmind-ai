from __future__ import annotations


def explain_trade(verdict: dict[str, object]) -> str:
    return str(verdict.get("summary") or "Trade explanation is available through deterministic fallback.")
