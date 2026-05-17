from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class JournalWriter:
    entries: list[dict[str, object]] = field(default_factory=list)

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.entries.append({"timestamp": datetime.now(UTC).isoformat(), "event": event, "payload": payload})
