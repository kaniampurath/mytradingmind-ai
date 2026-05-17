from __future__ import annotations

import json
import os
import time
from typing import Any

from aegis_trader.core.config import settings
from aegis_trader.llm.fallback_rules import rule_based_trade_review
from aegis_trader.llm.prompts import PROMPT_VERSION, TRADE_REVIEW_PROMPT


class LLMClient:
    def review_trade(self, context: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        llm_mode = str(getattr(settings, "llm_mode", "rules"))
        llm_enabled = bool(getattr(settings, "llm_enabled", False))
        llm_model = str(getattr(settings, "llm_model", getattr(settings, "openai_model", "gpt-4o")))
        llm_timeout_ms = int(getattr(settings, "llm_timeout_ms", 2000))
        if llm_mode.lower() == "rules" or not llm_enabled or not os.environ.get("OPENAI_API_KEY"):
            response = rule_based_trade_review(context)
            return self._audit(response, "rules", True, started)
        try:
            from openai import OpenAI

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=llm_timeout_ms / 1000)
            completion = client.chat.completions.create(
                model=llm_model,
                messages=[
                    {"role": "system", "content": TRADE_REVIEW_PROMPT},
                    {"role": "user", "content": json.dumps(context, default=str)},
                ],
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content or "{}"
            return self._audit(json.loads(content), llm_model, False, started)
        except Exception:
            response = rule_based_trade_review(context)
            return self._audit(response, "rules", True, started)

    @staticmethod
    def _audit(response: dict[str, Any], model: str, fallback_used: bool, started: float) -> dict[str, Any]:
        response.setdefault("approved", response.get("action") != "REJECT")
        response.setdefault("concern_score", 0.25)
        response.setdefault("concerns", [])
        response.setdefault("summary", "")
        response.setdefault("action", "ALLOW" if response["approved"] else "REJECT")
        response["model_used"] = model
        response["prompt_version"] = PROMPT_VERSION
        response["fallback_used"] = fallback_used
        response["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response
