from __future__ import annotations

from aegis_trader.core.logging import redact_url


def test_redact_url_hides_database_password() -> None:
    redacted = redact_url("mysql+pymysql://tradeuser:example_password@127.0.0.1:3307/bots")

    assert "example_password" not in redacted
    assert "tradeuser:***@" in redacted
