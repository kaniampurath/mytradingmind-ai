from __future__ import annotations

from aegis_trader.llm.reasoning_agent import ReasoningAgent


def test_llm_rule_fallback_is_json_like(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    verdict = ReasoningAgent().review_trade({"spread_bps": 20, "liquidity_score": 0.2, "orderflow_score": 10})
    assert verdict["action"] == "REJECT"
    assert verdict["concern_score"] > 0.35
    assert verdict["fallback_used"] is True
