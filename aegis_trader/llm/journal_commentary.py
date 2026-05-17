from __future__ import annotations


def journal_commentary(event: dict[str, object]) -> str:
    decision = event.get("decision", "UNKNOWN")
    reason = event.get("reason", "")
    return f"{decision}: {reason}".strip()
