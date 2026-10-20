from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    database_path: str = "data/trading_agent.db"
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    paper_trading_only: bool = True
    live_trading_enabled: bool = False
    allow_live_order_endpoints: bool = False

    local_model_discovery_enabled: bool = True
    local_model_base_urls: str = (
        "http://127.0.0.1:8080/v1,"
        "http://127.0.0.1:1919/v1,"
        "http://127.0.0.1:8081/v1"
    )
    local_model_id: str = ""
    model_discovery_timeout_seconds: float = Field(default=3.0, ge=0.5, le=15)
    ai_request_timeout_seconds: float = Field(default=180, ge=5, le=600)
    ai_queue_max_pending: int = Field(default=100, ge=1, le=10000)
    inference_lock_path: str = ".agent_inference.lock"

    hosted_fallback_enabled: bool = False
    hosted_fallback_eligible: bool = False
    hosted_base_url: str = ""
    hosted_model_id: str = ""
    hosted_api_key: str = ""

    market_provider: str = "alpaca"
    market_data_base_url: str = "https://data.alpaca.markets"
    market_data_api_key: str = ""
    market_data_api_secret: str = ""
    market_data_feed: str = "iex"
    market_scan_enabled: bool = True
    market_scan_interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_stale_data_seconds: int = Field(default=600, ge=60, le=86400)
    watchlist_symbols: str = "SPY,QQQ,AAPL,MSFT"
    news_rss_urls: str = ""
    economic_events_rss_urls: str = ""
    news_feed_timeout_seconds: float = Field(default=8.0, ge=1, le=30)

    paper_initial_equity: float = Field(default=100000, gt=0)
    risk_per_trade_pct: float = Field(default=0.25, gt=0, le=1)
    daily_loss_limit_pct: float = Field(default=1.0, gt=0, le=10)
    max_open_positions: int = Field(default=5, ge=1, le=100)
    max_position_value_pct: float = Field(default=20, gt=0, le=100)
    min_reward_risk: float = Field(default=1.0, gt=0, le=10)
    require_approval_for_new_positions: bool = True
    auto_promote_strategies: bool = False

    @property
    def local_base_urls(self) -> list[str]:
        return [item.strip().rstrip("/") for item in self.local_model_base_urls.split(",") if item.strip()]

    @property
    def configured_watchlist(self) -> list[str]:
        return list(dict.fromkeys(
            item.strip().upper()
            for item in self.watchlist_symbols.split(",")
            if item.strip()
        ))

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @field_validator("market_data_feed")
    @classmethod
    def normalize_feed(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("MARKET_DATA_FEED must be iex, sip, or delayed_sip")
        return value

    @model_validator(mode="after")
    def enforce_safety_defaults(self):
        if self.live_trading_enabled or self.allow_live_order_endpoints or not self.paper_trading_only:
            raise ValueError(
                "This build is paper-only: PAPER_TRADING_ONLY=true, "
                "LIVE_TRADING_ENABLED=false, ALLOW_LIVE_ORDER_ENDPOINTS=false are mandatory."
            )
        if not self.require_approval_for_new_positions:
            raise ValueError("Phase 1 requires approval for every new paper position.")
        if self.auto_promote_strategies:
            raise ValueError("Automatic strategy promotion is not supported in Phase 1.")
        if self.hosted_fallback_enabled:
            if not (
                self.hosted_fallback_eligible and self.hosted_base_url
                and self.hosted_model_id and self.hosted_api_key
            ):
                raise ValueError(
                    "Hosted fallback requires an explicitly eligible quota plus base URL, model ID, and API key."
                )
        return self

    def ensure_local_paths(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.inference_lock_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
