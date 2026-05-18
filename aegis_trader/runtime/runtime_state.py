from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BotRuntimeState:
    bot_id: str
    name: str
    description: str = ""
    strategy: str = ""
    symbol: str = ""
    timeframe: str = "1h"
    status: str = "STOPPED"
    mode: str = "PAPER"
    runtime_mode: str = "HEADLESS"
    uptime_seconds: int = 0
    last_heartbeat: str = ""
    last_error: str = ""
    risk_state: str = "OK"
    validation_status: str = "UNKNOWN"
    llm_state: str = "RULE_BASED"
    exchange_mode: str = "BINANCE_TESTNET"
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeAuditEvent:
    timestamp: str
    source: str
    bot_id: str
    action: str
    previous_state: str
    new_state: str
    result: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
