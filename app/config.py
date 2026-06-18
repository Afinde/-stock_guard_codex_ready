from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    database_url: str = "sqlite:///./data/stock_guard.db"
    timezone: str = "Asia/Shanghai"
    account_equity: float = 100_000.0
    risk_per_trade: float = 0.005
    stop_loss_pct: float = 0.05
    max_single_position_pct: float = 0.15
    max_total_position_pct: float = 0.60
    max_daily_loss_pct: float = 0.02
    watchlist: List[str] = Field(default_factory=lambda: ["600519", "000858"])
    webhook_url: str = ""
    enable_live_order: bool = False
    manual_confirm_required: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("watchlist", mode="before")
    @classmethod
    def parse_watchlist(cls, value):
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
