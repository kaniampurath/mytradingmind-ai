# Institutional Readiness Check

This project is suitable for paper/testnet operation and continued production hardening. It should not be switched to real-money live execution until the live execution gate below is complete.

## Current Certification

| Area | Status | Evidence |
| --- | --- | --- |
| Event-driven core | Pass | Deterministic event bus tests and typed event models. |
| Strategy separation | Pass | Strategies emit signals/backtest outputs; bot framework is strategy-agnostic. |
| Risk gates | Pass | Portfolio cash, exposure, trade-window, and kill-switch settings persist in MariaDB and are enforced before deployment. |
| Persistence | Pass | MariaDB schema `bots`, table prefix `myts_bot_table_`, JSON fallback only when DB is unavailable. |
| Logging | Pass | Rotating app log plus Streamlit stdout/stderr logs under `logs/`. Passwords are redacted in diagnostics. |
| Dashboard | Pass for operations | Crypto-neutral symbol selection, live buckets, bot tiles, risk, journal, validation, and system health. |
| Binance testnet | Conditional | Testnet endpoints and scanner are wired; exchange credential probe is available. |
| Replay/backtest | Conditional | One-year feature files drive validation; direct exchange-quality replay metrics should expand before live trading. |
| Strategy drawdown locks | Pass for replay certification | Strategy replay modules stop opening new trades after module drawdown breaches the runtime lock threshold. |
| Real-money live trading | Not certified | Live mode must remain disabled until order protection, reconciliation, and kill-switch drills pass on a droplet. |

## Mandatory Live-Mode Gates

Do not enable `AEGIS_MODE=LIVE_MODE` until all are true:

- Binance testnet order placement, rejection handling, latency, and slippage logs are persisted.
- Startup reconciliation confirms balances, open orders, and protection state.
- Every opened position has exchange-native stop or OCO protection.
- Missing protection triggers kill switch and emergency flatten in testnet drills.
- MariaDB backup and restore has been tested.
- Dashboard, scanner, and database services survive restart and recover bot state deterministically.
- Firewall restricts dashboard and database access.
- API keys are stored only in environment or a secrets manager, never in Git.

## Operational Runbook

- Start with `PAPER_MODE`.
- Keep `AEGIS_BINANCE_TESTNET=true` until testnet certification is complete.
- Review `logs/mytradingmind.log` daily for `fallback`, `risk_gate_block`, `database_*_failed`, and `journal_database_save_failed`.
- Check System Health for database, websocket, bot heartbeat, failed bots, errors, and retries.
- Treat new research strategies as experimental until production stress confirms module drawdown stays inside the configured lock threshold.
- Back up MariaDB before code updates.
- Restart services after deployment and verify dashboard HTTP 200 plus a fresh app log entry.

## Current Score

| Category | Score | Note |
| --- | ---: | --- |
| Architecture | 8.6 / 10 | Solid separation and persistence; background worker hardening remains. |
| Risk controls | 8.4 / 10 | Hard gates exist; live execution reconciliation still needs full drill coverage. |
| Observability | 8.2 / 10 | Logs and health screen present; metrics exporter is a future upgrade. |
| Deployment readiness | 8.0 / 10 | Docker/Ubuntu path exists; CI and secrets workflow should be added after GitHub upload. |
| Live trading readiness | 6.5 / 10 | Testnet-ready, not real-money certified yet. |

Overall: production-track, testnet/paper certified, live-money gated.
