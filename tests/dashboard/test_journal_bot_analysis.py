from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards.app import bot_journal_analysis


def test_journal_bot_analysis_reports_pros_cons_and_improvements() -> None:
    bots = pd.DataFrame(
        [
            {
                "name": "Alpha Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "BTC/USDT",
            }
        ]
    )
    validation = pd.DataFrame(
        [
            {
                "bot_name": "Alpha Bot",
                "strategy": "ATR Trend Burst",
                "symbol": "BTC/USDT",
                "metrics": {
                    "total_trades": 12,
                    "net_pnl": 240.0,
                    "profit_factor": 1.8,
                    "win_rate": 50.0,
                    "max_drawdown_pct": 4.5,
                },
            }
        ]
    )
    journal = pd.DataFrame(
        [
            {
                "bot_name": "Alpha Bot",
                "symbol": "BTC/USDT",
                "event_type": "VALIDATION_RUN",
                "severity": "INFO",
            }
        ]
    )
    trades = pd.DataFrame(
        [
            {"symbol": "BTC/USDT", "pnl": 10.0},
            {"symbol": "BTC/USDT", "pnl": -3.0},
        ]
    )

    analysis = bot_journal_analysis(bots, validation, journal, trades)

    assert analysis.iloc[0]["Bot"] == "Alpha Bot"
    assert "positive validated net P&L" in analysis.iloc[0]["Pros"]
    assert "Insufficient evidence" in analysis.iloc[0]["Cons"]
    assert "collect more live journal events" in analysis.iloc[0]["Areas of Improvement"]
    assert "PF 1.80" in analysis.iloc[0]["Evidence"]


def test_journal_screen_contains_bot_analysis_section() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    body = text[text.index("def journal_screen") : text.index("def metric_from_validation_row")]

    assert "### Bot Analysis" in body
    assert "Evidence-based pros, cons, improvement areas, and approval-gated recommendations" in body
    assert "never modifies strategy code or runtime configuration without human approval" in body
    assert "bot_journal_analysis" in text
