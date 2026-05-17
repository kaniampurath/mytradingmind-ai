from __future__ import annotations

from typing import Any

import pandas as pd


def utc_day_window(start_date: Any, end_date: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return an inclusive start / exclusive end UTC window for date pickers."""

    start_ts = pd.to_datetime(start_date, utc=True)
    end_ts = pd.to_datetime(end_date, utc=True) + pd.Timedelta(days=1)
    return pd.Timestamp(start_ts), pd.Timestamp(end_ts)


def utc_datetime_series(values: pd.Series) -> pd.Series:
    """Normalize a datetime series to UTC for safe comparisons."""

    return pd.to_datetime(values, errors="coerce", utc=True)
