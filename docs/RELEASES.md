# mytradingmind.ai Releases

This repository keeps deployable release tags so operators can roll forward or back without depending on a moving branch.

## v1.0 Baseline

`v1.0` is the preserved baseline from the original `main` branch before the Bot Operations Platform upgrade.

Use it when you need the earlier app behavior:

```bash
git fetch --tags
git checkout v1.0
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python scripts/init_db.py --print-tables
python -m mytradingmind.dashboard start
```

## v1.2.7 Current Main

`v1.2.7` is the current main release. It includes the v1.2 operations platform plus:

- Security/RBAC foundations with hashed credentials and role-gated navigation
- Ubuntu/DigitalOcean install preflight, environment validation, upgrade automation, diagnostics, and reboot helpers
- Docker runtime image packaging for the `mytradingmind.runtime` compatibility entrypoint
- Dashboard resilience for empty or malformed live-scan data
- Runtime-state trade classification from persisted headless status and entry price
- Runtime status merge into bot tiles so running headless bots do not display as stopped
- AAPIF institutional strategy evolution clones preserved beside baseline strategies
- Persisted bot in-trade/out-of-trade state for UI restart recovery

`v1.2.5` remains the previous dashboard scan resilience release. `v1.2.4` remains the previous Docker runtime packaging release. `v1.2.3` remains the previous Ubuntu upgrade image-refresh release. `v1.2.2` remains the previous Ubuntu upgrade automation release. `v1.2.1` remains the previous security/install hardening release. `v1.2` remains the previous operations-platform release. It includes:

- Global Dashboard with Live Trading and SignalFlow panels
- Bot Management parent module
- Bot Runtime cockpit
- Bot Admin emergency controls
- Trade Management
- Journal bot analysis
- Headless runtime shell helpers
- Ubuntu database bootstrap script

Use it for current testnet/paper operations:

```bash
git fetch --tags
git checkout v1.2.7
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python scripts/init_db.py --print-tables
python -m mytradingmind.runtime start --mode headless
python -m mytradingmind.dashboard start
```

Ubuntu helper path:

```bash
sh scripts/create_ubuntu_database.sh
sh scripts/runtime_start.sh
sh scripts/runtime_monitor.sh --once
sh scripts/runtime_stop.sh
```

## Release Validation

Before tagging a release, run:

```bash
pytest -q
python scripts/validate_build.py --json
python scripts/production_readiness_stress.py
python scripts/institutional_check.py
```

All live trading remains testnet/paper by default. Do not use these releases for real-money trading without independent review, validation, and operational certification.
