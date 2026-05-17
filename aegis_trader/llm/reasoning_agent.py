from __future__ import annotations

from typing import Any

from aegis_trader.llm.llm_client import LLMClient


class ReasoningAgent:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient()

    def review_trade(self, context: dict[str, Any]) -> dict[str, Any]:
        verdict = self.client.review_trade(context)
        if float(verdict.get("concern_score", 0.0) or 0.0) > 0.35:
            verdict["approved"] = False
            verdict["action"] = "REJECT"
        return verdict

    def explain_status(self, context: dict[str, Any]) -> str:
        verdict = self.review_trade(context)
        return str(verdict.get("summary", "Rule-based reasoning unavailable."))
