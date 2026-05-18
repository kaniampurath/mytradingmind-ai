# mytradingmind.ai

mytradingmind.ai is an educational, testnet-first crypto trading operations platform. It is built to help you study how an institutional-style trading system is organized: market data, bot lifecycle, strategy validation, risk controls, journaling, and observability.

> Educational use only. This project is not financial advice. Do not use the included strategies for real-money production trading. Strategies must be independently reviewed, tested, risk-approved, and legally/commercially assessed before any live deployment.

## What It Does

- Watches Binance Spot Testnet market data
- Accumulates websocket candles across multiple timeframes
- Lets you create and manage bot instances
- Runs a dashboard independently from the headless runtime
- Backtests and stress-tests strategy modules
- Persists bot state, risk settings, validation runs, and journal events
- Shows live bot/runtime status through an operator console
- Keeps risk gates and runtime controls separate from strategy code

## Technology Architecture

![mytradingmind.ai technology architecture](docs/assets/mytradingmind-technology-architecture.png)

The system is split into independent layers:

- **Market Connectivity**: Binance Spot Testnet websocket, REST candles, order book, trades, klines
- **Market Data Fabric**: multi-timeframe candles, features, orderflow, regime context
- **Runtime Layer**: headless runtime, command bus, bot registry, runtime state, heartbeat
- **Strategy Layer**: pluggable strategy registry and strategy-specific default timeframes
- **Decision and Risk Layer**: consensus, risk gates, LLM/rules reasoning, kill-switch controls
- **Execution Layer**: testnet execution gateway, OMS, protection, reconciliation
- **Persistence Layer**: MariaDB, Redis-ready state, Parquet feature files, logs, journal, validation runs
- **Operator UI**: Streamlit dashboard, Bot Admin, Bot Framework, Bot Runtime, Risk, Journal, Validation Lab
- **Deployment Layer**: Windows local development, GitHub, Docker Compose, Ubuntu/DigitalOcean

Detailed notes: [Architecture Overview](docs/ARCHITECTURE.md)

## Screens

- Dashboard
- Live Trading
- Order Flow
- Risk
- Bot Framework
- Bot Runtime
- Bot Admin
- System Health
- Journal
- Validation Lab

## Safety First

The project is intentionally designed to fail closed:

- Strategies do not call the exchange directly
- Risk gates are hard blocks, not advisory labels
- Bot state survives browser refreshes and dashboard restarts
- Headless runtime can run without Streamlit
- Dashboard is an operator/control surface, not the trading loop
- Binance Testnet is the default operating assumption
- Live-money operation is not certified

Keep these settings until you have completed testnet-only validation:

```text
AEGIS_MODE=PAPER_MODE
AEGIS_BINANCE_TESTNET=true
```

## Quick Start: Windows

```powershell
git clone https://github.com/kaniampurath/mytradingmind-ai.git
cd mytradingmind-ai

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

copy .env.example .env
python scripts\init_db.py
pytest
```

Start the dashboard:

```powershell
python -m mytradingmind.dashboard start
```

Open:

```text
http://127.0.0.1:8501
```

Start the headless runtime separately:

```powershell
python -m mytradingmind.runtime start --mode headless
```

You can also start/stop runtime and bots from **Bot Admin** inside the dashboard.

## Quick Start: Ubuntu / DigitalOcean

```bash
git clone https://github.com/kaniampurath/mytradingmind-ai.git
cd mytradingmind-ai

chmod +x setup.sh
./setup.sh

cp deploy/ubuntu.env.example .env
nano .env

mkdir -p data reports logs backups
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mariadb redis
docker compose -f deploy/docker-compose.yml --env-file .env run --rm mytradingmind_dashboard python scripts/init_db.py
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build mytradingmind_runtime mytradingmind_dashboard scanner
```

Open the dashboard:

```text
http://YOUR_DROPLET_IP:8501
```

Full guide: [Ubuntu Droplet Deployment](docs/UBUNTU_DROPLET_DEPLOYMENT.md)

## Runtime Commands

Dashboard only:

```bash
python -m mytradingmind.dashboard start
```

Headless runtime only:

```bash
python -m mytradingmind.runtime start --mode headless
```

Runtime status:

```bash
python -m mytradingmind.runtime status
```

Bot control:

```bash
python -m mytradingmind.runtime start-bot --bot-id BOT_ID
python -m mytradingmind.runtime stop-bot --bot-id BOT_ID
python -m mytradingmind.runtime pause-bot --bot-id BOT_ID
python -m mytradingmind.runtime resume-bot --bot-id BOT_ID
```

## Market Data

Run the Binance websocket stream:

```bash
python scripts/binance_stream.py --interval 1m --write-seconds 2
```

The stream accumulates closed candles into multiple timeframes:

```text
1m, 5m, 15m, 1h, 4h, 1d
```

Backfill historical Binance candles:

```bash
python scripts/binance_backfill.py --symbols BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,TRX/USDT
```

## Add A New Strategy

Strategies are plugins. They should only produce signals and replay results. They must not call Binance, write directly to the database, bypass risk gates, or place orders.

1. Open the strategy registry file:

```text
aegis_trader/strategies/backtest_plugins.py
```

2. Create a new class that extends `BacktestStrategy`:

```python
class MyNewStrategy(BacktestStrategy):
    name = "My New Strategy"
    description = "Plain-English description of the idea."
    default_timeframe = "5m"  # or 1h, 4h, 1d
    max_hold_bars = 24

    def entry_signal(self, row: pd.Series, previous: pd.Series | None) -> BacktestSignal | None:
        if previous is None:
            return None

        close = float(row["close"])
        atr = float(row["atr14"])

        if close > float(row["ema20"]) and float(row["rvol30"]) >= 1.0:
            return BacktestSignal(
                entry=True,
                stop_price=max(0.000001, close - (1.2 * atr)),
                take_profit_price=close + (2.0 * atr),
                reason="trend and volume confirmation",
            )
        return None
```

3. Register it in `STRATEGY_REGISTRY`:

```python
STRATEGY_REGISTRY: dict[str, BacktestStrategy] = {
    strategy.name: strategy
    for strategy in (
        ExistingMomentumStrategy(),
        ATRTrendBurstStrategy(),
        VWAPReclaimBacktestStrategy(),
        KCJATRTrendBurstParityStrategy(),
        CertifiedRiskManagedCompositeStrategy(),
        MyNewStrategy(),
    )
}
```

4. Add a focused test under `tests/backtest/`:

```python
def test_my_new_strategy_is_registered() -> None:
    assert "My New Strategy" in STRATEGY_REGISTRY
```

5. Backfill data for the strategy timeframe:

```bash
python scripts/binance_backfill.py --interval 5m --symbols BTC/USDT,ETH/USDT,SOL/USDT
```

6. Run validation:

```bash
pytest
python scripts/validate_build.py --json
python scripts/production_readiness_stress.py
```

7. Open the dashboard and create a bot:

```bash
python -m mytradingmind.dashboard start
```

Then use:

- **Bot Framework** to create a bot with the new strategy
- **Validation Lab** to backtest it
- **Bot Runtime** / **Bot Admin** to monitor or control it

New strategies should be considered experimental until they pass backtesting, stress testing, journal review, and risk certification.

## Validation And Benchmarks

Run tests:

```bash
pytest
```

Run build validation:

```bash
python scripts/validate_build.py --json
```

Run institutional checks:

```bash
python scripts/institutional_check.py --run-tests
```

Run stress testing:

```bash
python scripts/production_readiness_stress.py
```

Benchmark outputs are written under `reports/`.

## Database

Default local schema/database:

```text
bots
```

Table prefix:

```text
myts_bot_table_
```

Example `.env` values:

```text
AEGIS_DATABASE_ENABLED=true
AEGIS_DATABASE_SCHEMA=bots
AEGIS_DATABASE_URL=mysql+pymysql://tradeuser:<password>@127.0.0.1:3307/bots
```

Do not commit `.env` or real credentials.

## Secrets

Never commit:

- `.env`
- Binance API keys
- OpenAI API keys
- database passwords
- private keys
- production logs

Use `.env.example` or `deploy/ubuntu.env.example` as templates.

## Current Readiness

- Educational/testnet workflow: ready for continued experimentation
- Headless runtime/dashboard separation: implemented
- Multi-timeframe websocket accumulation: implemented
- Strategy registry: pluggable and extensible
- Live-money trading: not approved, not certified, and not recommended

Read more: [Institutional Readiness Check](docs/INSTITUTIONAL_READINESS.md)
