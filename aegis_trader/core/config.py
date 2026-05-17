from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aegis_trader.core.enums import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    mode: TradingMode = TradingMode.PAPER_MODE
    environment: str = "dev"
    symbols: list[str] = Field(default_factory=list)
    binance_testnet: bool = True
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_spot_base_url: str = "https://api.binance.com"
    binance_spot_testnet_base_url: str = "https://testnet.binance.vision"
    binance_history_interval: str = "1h"
    binance_history_days: int = 365
    live_scan_refresh_seconds: int = 300
    dashboard_refresh_seconds: int = 60
    market_data_dir: str = "data/binance"
    database_url: str = "mysql+pymysql://tradeuser:replace_with_strong_password@127.0.0.1:3306/bots"
    database_schema: str = "bots"
    database_enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5
    openai_model: str = "gpt-4o"
    openai_fallback_model: str = "gpt-4o-mini"

    max_daily_loss_pct: float = 0.02
    max_position_notional: float = 250.0
    max_portfolio_exposure: float = 1000.0
    max_trades_per_day: int = 12
    consecutive_loss_lock: int = 3
    slippage_threshold_bps: float = 15.0
    stale_feed_seconds: float = 5.0
    event_queue_maxsize: int = 10_000


settings = Settings()
