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
    started_at: str = ""
    pnl_started_at: str = ""
    runtime_entry_price: float = 0.0
    pnl_since_start: float = 0.0
    pnl_since_start_pct: float = 0.0
    last_heartbeat: str = ""
    last_error: str = ""
    risk_state: str = "OK"
    framework: str = "PRODUCTION_STABILITY_V2"
    framework_status: str = "READY"
    protection_state: str = "UNKNOWN"
    restart_required: bool = False
    last_framework_reason: str = ""
    supervisor_action: str = "NONE"
    alert_level: str = "INFO"
    alert_code: str = ""
    data_state: str = "UNKNOWN"
    reconciliation_state: str = "NOT_RUN"
    portfolio_state: str = "UNKNOWN"
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
