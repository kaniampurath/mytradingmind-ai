# mytradingmind.ai Architecture Overview

This diagram is the visitor-friendly map of how the platform is intended to operate. The dashboard is an operator console; bot execution is owned by the headless runtime and shared command bus.

```mermaid
flowchart TB
    subgraph Exchange["Exchange Data Boundary"]
        Binance["Binance Spot Testnet"]
        Candles["Historical candles"]
        Socket["Websocket trades / book"]
    end

    subgraph Data["Market Data Layer"]
        Backfill["Backfill service"]
        Scanner["Live scanner"]
        Stream["Socket stream"]
    end

    subgraph Persistence["Persistence And State"]
        MariaDB["MariaDB<br/>bot instances, risk, journal, validation"]
        Redis["Redis<br/>realtime runtime cache"]
        Reports["Reports cache<br/>local dev fallback"]
    end

    subgraph Runtime["Headless Runtime"]
        Registry["Bot Registry"]
        CommandBus["Command Bus"]
        Manager["Runtime Manager"]
        Heartbeat["Heartbeat Monitor"]
        BotRunner["Bot Runner"]
    end

    subgraph Controls["Operator Interfaces"]
        Dashboard["Streamlit Dashboard"]
        BotAdmin["Bot Admin"]
        CLI["CLI"]
    end

    subgraph Decisioning["Decision And Safety"]
        Risk["Hard Risk Gates"]
        Strategies["Pluggable Strategies"]
        LLM["LLM Reasoning<br/>OpenAI or rule fallback"]
        Validation["Validation Lab"]
        Journal["Journal"]
    end

    Binance --> Candles --> Backfill
    Binance --> Socket --> Stream
    Backfill --> Scanner
    Stream --> Scanner
    Scanner --> MariaDB
    Scanner --> Redis
    Scanner --> Reports

    MariaDB --> Dashboard
    Redis --> Dashboard
    Reports --> Dashboard
    Dashboard --> BotAdmin
    BotAdmin --> CommandBus
    CLI --> CommandBus
    CommandBus --> Manager
    Manager --> Registry
    Manager --> Heartbeat
    Manager --> BotRunner
    Registry --> MariaDB
    Heartbeat --> Redis
    BotRunner --> Risk
    Risk --> Strategies
    Strategies --> Validation
    Strategies --> Journal
    LLM --> BotAdmin
    LLM --> Journal
    LLM --> Validation
    LLM --> Dashboard
    Journal --> MariaDB
    Validation --> MariaDB
```

## Core Responsibilities

- **Streamlit Dashboard**: visualization, configuration, Bot Admin controls, validation views, journal review, and operational health.
- **Bot Admin**: sends lifecycle commands through the shared command bus. It does not place exchange orders.
- **Command Bus**: normalizes runtime commands from UI and CLI, prevents duplicate commands, validates command shape, and routes actions to the runtime manager.
- **Headless Runtime**: owns bot state transitions, heartbeat state, runtime status, and 24x7 operation independent of browser sessions.
- **MariaDB**: durable source of truth for bot instances, risk settings, journal events, validation runs, scanner state, and replay metrics.
- **Redis**: intended low-latency cache for runtime status and live dashboard refresh.
- **LLM Reasoning**: explains, challenges, and summarizes decisions. If `OPENAI_API_KEY` is absent or `AEGIS_LLM_MODE=rules`, deterministic fallback rules are used.

## Safety Boundaries

- Strategies generate signals only.
- Bot Admin and CLI issue runtime commands only.
- Risk gates must approve any deployable runtime action.
- LLM output can explain or veto, but cannot place orders, bypass risk, or override execution.
- Live-money operation remains gated until testnet execution, reconciliation, restart recovery, and kill-switch drills are certified.

## Deployment Shape

```mermaid
flowchart LR
    GitHub["GitHub repository"] --> Droplet["Ubuntu DigitalOcean droplet"]
    Droplet --> Compose["docker compose"]
    Compose --> Runtime["mytradingmind_runtime"]
    Compose --> UI["mytradingmind_dashboard"]
    Compose --> DB["mariadb"]
    Compose --> Cache["redis"]
    Runtime --> DB
    Runtime --> Cache
    UI --> DB
    UI --> Cache
```
