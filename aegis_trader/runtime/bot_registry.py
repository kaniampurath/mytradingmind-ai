from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from aegis_trader.core.config import settings
from aegis_trader.storage.bot_repository import read_bot_instances, upsert_bot_instance
from aegis_trader.storage.db import build_engine, build_session_factory


BOT_INSTANCES_PATH = Path("reports/bot_instances.json")


def normalize_bot_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.strip()).strip("_") or "bot"


class BotRegistry:
    """Persistent bot registry shared by UI, CLI, and runtime modules."""

    def __init__(self, path: Path = BOT_INSTANCES_PATH) -> None:
        self.path = path

    def load(self) -> pd.DataFrame:
        rows = self._load_file_rows()
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        return self._normalize(frame)

    async def load_async(self) -> pd.DataFrame:
        file_frame = self.load()
        if not settings.database_enabled:
            return file_frame
        engine = build_engine()
        factory = build_session_factory(engine)
        async with factory() as session:
            db_frame = await read_bot_instances(session)
        await engine.dispose()
        if db_frame.empty:
            return file_frame
        return self.merge(self._normalize(db_frame), file_frame)

    def save(self, frame: pd.DataFrame) -> None:
        normalized = self._normalize(frame)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(normalized.to_dict(orient="records"), indent=2, default=str), encoding="utf-8")

    async def save_async(self, frame: pd.DataFrame) -> None:
        normalized = self._normalize(frame)
        self.save(normalized)
        if not settings.database_enabled:
            return
        engine = build_engine()
        factory = build_session_factory(engine)
        async with factory() as session:
            for row in normalized.to_dict(orient="records"):
                await upsert_bot_instance(session, row)
        await engine.dispose()

    def get(self, bot_id: str, frame: pd.DataFrame | None = None) -> dict[str, Any] | None:
        frame = self.load() if frame is None else self._normalize(frame)
        if frame.empty:
            return None
        mask = frame["bot_id"].astype(str).eq(bot_id) | frame["name"].astype(str).eq(bot_id)
        if not mask.any():
            return None
        return frame.loc[mask].iloc[0].to_dict()

    def update_state(self, bot_id: str, state: str, reason: str = "") -> dict[str, Any]:
        frame = self.load()
        if frame.empty:
            raise KeyError(f"Unknown bot: {bot_id}")
        frame = self._normalize(frame)
        mask = frame["bot_id"].astype(str).eq(bot_id) | frame["name"].astype(str).eq(bot_id)
        if not mask.any():
            raise KeyError(f"Unknown bot: {bot_id}")
        now = datetime.now(UTC).isoformat()
        frame.loc[mask, "state"] = state
        frame.loc[mask, "status_reason"] = reason
        frame.loc[mask, "heartbeat_at"] = now
        frame.loc[mask, "updated_at"] = now
        self.save(frame)
        return frame.loc[mask].iloc[0].to_dict()

    @staticmethod
    def merge(primary: pd.DataFrame, secondary: pd.DataFrame) -> pd.DataFrame:
        frames = [frame for frame in [primary, secondary] if not frame.empty]
        if not frames:
            return pd.DataFrame()
        merged = BotRegistry._normalize(pd.concat(frames, ignore_index=True))
        merged["_freshness"] = BotRegistry._freshness_series(merged)
        merged = merged.sort_values(["bot_id", "_freshness"], ascending=[True, False])
        return merged.drop_duplicates(subset=["bot_id"], keep="first").drop(columns=["_freshness"]).reset_index(drop=True)

    def _load_file_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        for column, default in {
            "name": "",
            "strategy": "Existing Momentum",
            "symbol": "",
            "timeframe": "1h",
            "capital": 250.0,
            "parameters": {},
            "state": "DRAFT",
            "status_reason": "",
            "deployed_at": "",
            "heartbeat_at": "",
            "created_at": "",
            "updated_at": "",
        }.items():
            if column not in frame:
                frame[column] = default
        if "bot_id" not in frame:
            frame["bot_id"] = frame["name"]
        if "description" not in frame:
            frame["description"] = ""
        if "mode" not in frame:
            frame["mode"] = "PAPER"
        frame["bot_id"] = frame["bot_id"].fillna(frame["name"]).astype(str).map(normalize_bot_id)
        frame["description"] = frame["description"].fillna("").astype(str)
        frame["mode"] = frame["mode"].fillna("PAPER").astype(str)
        return frame

    @staticmethod
    def _freshness_series(frame: pd.DataFrame) -> pd.Series:
        timestamps = []
        for column in ["updated_at", "heartbeat_at", "deployed_at", "created_at"]:
            if column in frame:
                timestamps.append(pd.to_datetime(frame[column], errors="coerce", utc=True))
        if not timestamps:
            return pd.Series(pd.Timestamp(0, tz="UTC"), index=frame.index)
        freshness = timestamps[0]
        for timestamp in timestamps[1:]:
            freshness = freshness.where(freshness.notna() & ((timestamp.isna()) | (freshness >= timestamp)), timestamp)
        return freshness.fillna(pd.Timestamp(0, tz="UTC"))
