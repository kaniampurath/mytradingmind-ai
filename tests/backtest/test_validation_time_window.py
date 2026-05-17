from __future__ import annotations

from datetime import date

import pandas as pd

from aegis_trader.analytics.time_utils import utc_datetime_series, utc_day_window


def test_utc_day_window_compares_against_utc_feature_times() -> None:
    features = pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"],
                utc=True,
            )
        }
    )
    start_ts, end_ts = utc_day_window(date(2026, 1, 2), date(2026, 1, 2))
    features["open_time"] = utc_datetime_series(features["open_time"])

    filtered = features[(features["open_time"] >= start_ts) & (features["open_time"] < end_ts)]

    assert len(filtered) == 1
    assert filtered.iloc[0]["open_time"] == pd.Timestamp("2026-01-02T00:00:00Z")
