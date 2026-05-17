from __future__ import annotations

from aegis_trader.llm.reasoning_agent import ReasoningAgent


def review_consensus(context: dict[str, object]) -> dict[str, object]:
    return ReasoningAgent().review_trade(context)
