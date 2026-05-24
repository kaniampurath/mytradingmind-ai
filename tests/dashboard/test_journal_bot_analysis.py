from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis_trader.dashboards.app import bot_journal_analysis, journal_learning_summary


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
    assert analysis.iloc[0]["Outcome"] in {"Working", "Watch", "Fix First"}
    assert analysis.iloc[0]["Learning Priority"] in {"Low", "Medium", "High"}
    assert float(analysis.iloc[0]["Outcome Score"]) > 0


def test_journal_learning_summary_turns_trades_into_operational_decision() -> None:
    journal = pd.DataFrame(
        [
            {"severity": "WARN", "event_type": "RISK_REJECTION"},
            {"severity": "INFO", "event_type": "VALIDATION_RUN"},
        ]
    )
    trades = pd.DataFrame([{"pnl": 12.0}, {"pnl": -4.0}, {"pnl": -3.0}])
    analysis = pd.DataFrame([{"Outcome Score": 42.0, "Human Approval Status": "PENDING"}])

    summary = journal_learning_summary(journal, trades, analysis)

    assert summary["Wins"] == 1
    assert summary["Losses"] == 2
    assert summary["Risk Blocks"] == 1
    assert summary["Decision"] == "Review Before Scaling"


def test_journal_correlation_is_nan_safe() -> None:
    from aegis_trader.dashboards.app import correlate_backtest_journal

    validation = pd.DataFrame(
        [
            {
                "bot_name": "NaN Bot",
                "symbol": "BTC/USDT",
                "strategy": "ATR Trend Burst",
                "profit_factor": 1.4,
                "win_rate": 45.0,
                "max_drawdown_pct": 5.0,
                "consecutive_losses": float("nan"),
            }
        ]
    )

    result = correlate_backtest_journal(validation, pd.DataFrame(), pd.DataFrame())

    assert result.iloc[0]["consecutive_losses"] == 0


def test_journal_screen_contains_bot_analysis_section() -> None:
    text = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")
    body = text[text.index("def journal_screen") : text.index("def metric_from_validation_row")]

    assert "### Trade Learning Loop" in body
    assert "### Bot Learning Board" in body
    assert "render_journal_learning_loop" in body
    assert "render_journal_action_queue" in body
    assert "never modifies strategy code or runtime configuration without human approval" in body
    assert "bot_journal_analysis" in text
    assert "Raw journal evidence" in body
