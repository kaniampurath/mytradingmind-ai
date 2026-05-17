from __future__ import annotations

import pandas as pd

from aegis_trader.core.config import settings
from aegis_trader.storage.bot_repository import _clean_dict
from aegis_trader.storage.db import normalize_async_database_url
from aegis_trader.storage.models import Base


def test_mariadb_url_is_async_and_schema_scoped() -> None:
    resolved = normalize_async_database_url(settings.database_url)

    assert resolved.startswith("mysql+aiomysql://")
    assert resolved.endswith("/bots")


def test_table_names_use_mytradingmind_prefix() -> None:
    names = {table.name for table in Base.metadata.tables.values()}

    assert names
    assert all(name.startswith("myts_bot_table_") for name in names)


def test_repository_payload_cleaning_removes_dataframe_nulls() -> None:
    payload = _clean_dict({"deployed_at": pd.NaT, "capital": float("nan"), "name": "bot"})

    assert payload == {"deployed_at": None, "capital": None, "name": "bot"}
