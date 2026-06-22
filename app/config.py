from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from datetime import time
from typing import Annotated, List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    app_version: str = "0.1.0"
    deployment_profile: str = "LOCAL"
    api_prefix: str = "/api/v1"
    enable_server_backtest: bool = False
    enable_light_scan: bool = True
    enable_paper_order_write: bool = False
    enable_live_provider: bool = False
    enable_websocket: bool = False
    max_page_size: int = Field(default=100, ge=1)
    dashboard_cache_seconds: int = Field(default=30, ge=0)
    database_url: str = "sqlite:///./data/stock_guard.db"
    timezone: str = "Asia/Shanghai"
    account_equity: float = 100_000.0
    risk_per_trade: float = 0.005
    stop_loss_pct: float = 0.05
    max_single_position_pct: float = 0.15
    max_total_position_pct: float = 0.60
    max_daily_loss_pct: float = 0.02
    watchlist: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["600519", "000858"]
    )
    market_data_adjust: str = "qfq"
    market_data_min_history_bars: int = Field(default=80, ge=1)
    market_data_max_stale_days: int = Field(default=0, ge=0)
    market_calendar_source: str = "local"
    market_calendar_path: str = "app/resources/a_share_calendar.json"
    market_close_time: str = "15:00"
    market_cache_enabled: bool = True
    market_cache_dir: str = ".cache/market_data"
    market_cache_schema_version: str = "daily-v1"
    market_cache_refresh_latest: bool = False
    market_provider_max_attempts: int = Field(default=3, ge=1)
    market_provider_initial_backoff_seconds: float = Field(default=1.0, ge=0)
    market_provider_max_backoff_seconds: float = Field(default=8.0, ge=0)
    market_provider_jitter_seconds: float = Field(default=0.1, ge=0)
    market_provider_requests_per_second: int = Field(default=2, ge=1)
    market_provider_requests_per_minute: int = Field(default=60, ge=1)
    market_provider_max_concurrency: int = Field(default=2, ge=1)
    market_data_mode: str = "FIXTURE"
    market_live_provider: str = "fixture"
    market_live_enabled: bool = False
    market_live_api_base_url: str = ""
    market_live_api_key: str = ""
    market_live_api_secret: str = ""
    market_live_account_id: str = ""
    market_live_poll_seconds: float = Field(default=5.0, gt=0)
    market_live_max_age_seconds: float = Field(default=30.0, gt=0)
    market_live_batch_size: int = Field(default=50, ge=1)
    market_live_max_symbols_per_request: int = Field(default=50, ge=1)
    market_live_clock_skew_seconds: float = Field(default=2.0, ge=0)
    market_live_max_attempts: int = Field(default=3, ge=1)
    market_live_initial_backoff_seconds: float = Field(default=1.0, ge=0)
    market_live_backoff_seconds: float = Field(default=1.0, ge=0)
    market_live_max_backoff_seconds: float = Field(default=8.0, ge=0)
    market_live_jitter_seconds: float = Field(default=0.1, ge=0)
    market_live_requests_per_second: int = Field(default=2, ge=1)
    market_live_requests_per_minute: int = Field(default=60, ge=1)
    market_live_max_concurrency: int = Field(default=2, ge=1)
    market_live_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    market_live_read_timeout_seconds: float = Field(default=5.0, gt=0)
    market_live_provider_failure_threshold: int = Field(default=3, ge=1)
    market_live_provider_recovery_success_count: int = Field(default=2, ge=1)
    market_live_record_raw_responses: bool = False
    market_live_record_quotes: bool = True
    market_live_record_dir: str = "data/recorded_quotes"
    market_live_raw_retention_days: int = Field(default=7, ge=1)
    market_live_normalized_retention_days: int = Field(default=90, ge=1)
    market_live_shadow_mode: bool = True
    market_live_fail_closed: bool = True
    market_live_price_conflict_bps: int = Field(default=100, ge=0)
    market_admission_minimum_complete_trading_days: int = Field(default=10, ge=1)
    market_admission_minimum_provider_availability: float = Field(default=0.99, ge=0, le=1)
    market_admission_minimum_symbol_coverage: float = Field(default=0.99, ge=0, le=1)
    market_admission_maximum_p95_latency_seconds: float = Field(default=5.0, ge=0)
    market_admission_maximum_missing_symbol_rate: float = Field(default=0.005, ge=0, le=1)
    market_admission_maximum_invalid_quote_rate: float = Field(default=0.001, ge=0, le=1)
    market_admission_maximum_out_of_order_rate: float = Field(default=0.001, ge=0, le=1)
    market_admission_maximum_schema_error_count: int = Field(default=0, ge=0)
    market_admission_maximum_unknown_suspension_rate: float = Field(default=0.001, ge=0, le=1)
    market_admission_maximum_unknown_limit_rule_rate: float = Field(default=0.001, ge=0, le=1)
    market_admission_minimum_replay_consistency: float = Field(default=1.0, ge=0, le=1)
    market_admission_minimum_account_immutability: float = Field(default=1.0, ge=0, le=1)
    market_admission_maximum_shadow_fill_count: int = Field(default=0, ge=0)
    market_admission_maximum_unhandled_error_count: int = Field(default=0, ge=0)
    market_quote_retention_days: int = Field(default=30, ge=1)
    postgres_tx_max_attempts: int = Field(default=3, ge=1)
    postgres_tx_initial_backoff_ms: int = Field(default=50, ge=0)
    postgres_tx_max_backoff_ms: int = Field(default=500, ge=0)
    postgres_tx_jitter_ms: int = Field(default=30, ge=0)
    postgres_retry_lock_not_available: bool = False
    postgres_lock_timeout_ms: int = Field(default=2000, ge=0)
    postgres_statement_timeout_ms: int = Field(default=15000, ge=0)
    strategy_name: str = "multi_factor_v1"
    strategy_version: str = "1.0.0"
    strategy_ma_short_period: int = Field(default=20, ge=1)
    strategy_ma_long_period: int = Field(default=60, ge=1)
    strategy_momentum_period: int = Field(default=20, ge=1)
    strategy_volatility_period: int = Field(default=20, ge=1)
    strategy_volume_period: int = Field(default=20, ge=1)
    strategy_rsi_period: int = Field(default=14, ge=1)
    strategy_breakout_period: int = Field(default=20, ge=1)
    strategy_trend_weight: float = Field(default=30.0, ge=0)
    strategy_momentum_weight: float = Field(default=20.0, ge=0)
    strategy_volatility_weight: float = Field(default=15.0, ge=0)
    strategy_volume_weight: float = Field(default=15.0, ge=0)
    strategy_rsi_weight: float = Field(default=10.0, ge=0)
    strategy_breakout_weight: float = Field(default=10.0, ge=0)
    strategy_buy_watch_threshold: float = 70.0
    strategy_stop_loss_pct: float = 0.05
    strategy_take_profit_1_pct: float = 0.05
    strategy_take_profit_2_pct: float = 0.08
    risk_per_trade_policy: float = 0.005
    risk_stop_loss_pct: float = 0.05
    risk_max_symbol_weight: float = 0.15
    risk_max_portfolio_weight: float = 0.60
    risk_max_industry_weight: float = 0.25
    risk_max_daily_loss_pct: float = 0.02
    risk_max_consecutive_losses: int = Field(default=3, ge=1)
    risk_reduce_drawdown: float = 0.08
    risk_off_drawdown: float = 0.12
    risk_reduced_max_portfolio_weight: float = 0.30
    risk_lot_size: int = Field(default=100, ge=1)
    auto_generate_order_proposals: bool = False
    paper_runtime_enabled: bool = False
    paper_runtime_instance_id: str = "paper-runtime-local"
    paper_manual_confirm_required: bool = True
    paper_scheduler_poll_seconds: float = Field(default=30.0, gt=0)
    paper_task_lease_seconds: float = Field(default=120.0, gt=0)
    paper_task_heartbeat_seconds: float = Field(default=30.0, gt=0)
    paper_task_max_attempts: int = Field(default=3, ge=1)
    paper_recovery_on_startup: bool = True
    paper_notification_worker_enabled: bool = False
    paper_allow_manual_task_trigger: bool = False
    paper_market_monitor_enabled: bool = False
    paper_market_monitor_batch_size: int = Field(default=50, ge=1)
    paper_market_data_max_age_seconds: float = Field(default=60.0, gt=0)
    paper_order_processing_max_attempts: int = Field(default=3, ge=1)
    paper_order_conflict_retry_attempts: int = Field(default=2, ge=0)
    paper_blocked_risk_policy: str = "keep_open"
    paper_stale_valuation_policy: str = "fail_closed"
    paper_settlement_require_all_prices: bool = True
    paper_ledger_tolerance: float = Field(default=0.01, ge=0)
    paper_valuation_adjust: str = ""
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

    @field_validator("market_data_adjust")
    @classmethod
    def validate_market_data_adjust(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {"", "qfq", "hfq"}
        if value not in allowed:
            raise ValueError(f"market_data_adjust must be one of {sorted(allowed)}")
        return value

    @field_validator("market_data_mode")
    @classmethod
    def validate_market_data_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"FIXTURE", "RECORDED", "LIVE_PAPER"}:
            raise ValueError("market_data_mode must be FIXTURE, RECORDED, or LIVE_PAPER")
        return normalized

    @field_validator("market_close_time")
    @classmethod
    def validate_market_close_time(cls, value: str) -> str:
        try:
            time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("market_close_time must use HH:MM or HH:MM:SS") from exc
        return value

    @property
    def market_close_time_value(self) -> time:
        return time.fromisoformat(self.market_close_time)

    @property
    def market_calendar_resolved_path(self) -> Path:
        return _resolve_project_path(self.market_calendar_path)

    @property
    def market_cache_resolved_dir(self) -> Path:
        return _resolve_project_path(self.market_cache_dir)

    @property
    def market_live_record_resolved_dir(self) -> Path:
        return _resolve_project_path(self.market_live_record_dir)

    @model_validator(mode="after")
    def validate_strategy_config(self):
        if self.deployment_profile not in {"LOCAL", "ECS_LITE"}:
            raise ValueError("deployment_profile must be LOCAL or ECS_LITE")
        if not self.api_prefix.startswith("/api/"):
            raise ValueError("api_prefix must start with /api/")
        if self.max_page_size > 1000:
            raise ValueError("max_page_size is too large")
        if self.deployment_profile == "ECS_LITE":
            if self.enable_server_backtest:
                raise ValueError("ECS_LITE must keep ENABLE_SERVER_BACKTEST=false")
            if self.enable_paper_order_write:
                raise ValueError("ECS_LITE must keep ENABLE_PAPER_ORDER_WRITE=false")
            if self.enable_live_provider:
                raise ValueError("ECS_LITE must keep ENABLE_LIVE_PROVIDER=false")
            if self.enable_websocket:
                raise ValueError("ECS_LITE must keep ENABLE_WEBSOCKET=false")
        weights = [
            self.strategy_trend_weight,
            self.strategy_momentum_weight,
            self.strategy_volatility_weight,
            self.strategy_volume_weight,
            self.strategy_rsi_weight,
            self.strategy_breakout_weight,
        ]
        total_weight = sum(weights)
        if abs(total_weight - 100.0) > 1e-9:
            raise ValueError("strategy weights must sum to 100")
        if not 0 <= self.strategy_buy_watch_threshold <= total_weight:
            raise ValueError("strategy_buy_watch_threshold must be between 0 and total strategy weight")
        if not 0 < self.strategy_stop_loss_pct < 1:
            raise ValueError("strategy_stop_loss_pct must be greater than 0 and less than 1")
        if self.strategy_take_profit_1_pct <= 0 or self.strategy_take_profit_2_pct <= 0:
            raise ValueError("strategy take-profit percentages must be greater than 0")
        if self.strategy_take_profit_2_pct <= self.strategy_take_profit_1_pct:
            raise ValueError("strategy_take_profit_2_pct must be greater than strategy_take_profit_1_pct")
        if self.strategy_ma_long_period <= self.strategy_ma_short_period:
            raise ValueError("strategy_ma_long_period must be greater than strategy_ma_short_period")
        if self.market_calendar_source != "local":
            raise ValueError("market_calendar_source currently supports only 'local'")
        if not self.market_cache_schema_version.strip():
            raise ValueError("market_cache_schema_version must not be empty")
        if self.market_provider_max_backoff_seconds < self.market_provider_initial_backoff_seconds:
            raise ValueError("market_provider_max_backoff_seconds must be >= initial backoff")
        if self.market_live_max_backoff_seconds < self.market_live_initial_backoff_seconds:
            raise ValueError("market_live_max_backoff_seconds must be >= market_live_initial_backoff_seconds")
        if self.market_live_backoff_seconds != self.market_live_initial_backoff_seconds:
            object.__setattr__(self, "market_live_backoff_seconds", self.market_live_initial_backoff_seconds)
        if self.postgres_tx_max_backoff_ms < self.postgres_tx_initial_backoff_ms:
            raise ValueError("postgres_tx_max_backoff_ms must be >= postgres_tx_initial_backoff_ms")
        if self.market_data_mode == "LIVE_PAPER" and not self.market_live_enabled:
            raise ValueError("LIVE_PAPER mode requires MARKET_LIVE_ENABLED=true")
        if self.market_live_enabled and self.market_data_mode != "LIVE_PAPER":
            raise ValueError("MARKET_LIVE_ENABLED requires MARKET_DATA_MODE=LIVE_PAPER")
        if self.market_data_mode == "LIVE_PAPER" and not self.market_live_shadow_mode:
            raise ValueError("LIVE_PAPER non-shadow mode is disabled in this project phase")
        if not self.market_live_shadow_mode:
            raise ValueError("MARKET_LIVE_SHADOW_MODE must remain true in this project phase")
        if not self.market_live_provider.strip():
            raise ValueError("market_live_provider must not be empty")
        if self.market_live_batch_size > self.market_live_max_symbols_per_request:
            raise ValueError("market_live_batch_size must not exceed market_live_max_symbols_per_request")
        if self.market_live_provider.strip().lower() not in {"fixture", "recorded", "live_paper", "live-paper"}:
            raise ValueError("market_live_provider is unsupported")
        risk_ratios = [
            self.risk_per_trade_policy,
            self.risk_stop_loss_pct,
            self.risk_max_symbol_weight,
            self.risk_max_portfolio_weight,
            self.risk_max_industry_weight,
            self.risk_max_daily_loss_pct,
            self.risk_reduce_drawdown,
            self.risk_off_drawdown,
            self.risk_reduced_max_portfolio_weight,
        ]
        if any(value < 0 or value > 1 for value in risk_ratios):
            raise ValueError("risk ratios must be between 0 and 1")
        if self.risk_per_trade_policy >= self.risk_max_symbol_weight:
            raise ValueError("risk_per_trade_policy must be less than risk_max_symbol_weight")
        if self.risk_max_symbol_weight > self.risk_max_portfolio_weight:
            raise ValueError("risk_max_symbol_weight must not exceed risk_max_portfolio_weight")
        if self.risk_reduced_max_portfolio_weight > self.risk_max_portfolio_weight:
            raise ValueError("risk_reduced_max_portfolio_weight must not exceed risk_max_portfolio_weight")
        if self.risk_off_drawdown <= self.risk_reduce_drawdown:
            raise ValueError("risk_off_drawdown must be greater than risk_reduce_drawdown")
        if self.paper_task_heartbeat_seconds >= self.paper_task_lease_seconds:
            raise ValueError("paper_task_heartbeat_seconds must be less than paper_task_lease_seconds")
        if not self.paper_runtime_instance_id.strip():
            raise ValueError("paper_runtime_instance_id must not be empty")
        if self.enable_live_order:
            raise ValueError("ENABLE_LIVE_ORDER must remain false in this project phase")
        if self.paper_blocked_risk_policy not in {"keep_open", "reject"}:
            raise ValueError("paper_blocked_risk_policy must be keep_open or reject")
        if self.paper_stale_valuation_policy not in {"fail_closed", "allow_suspended_last_price"}:
            raise ValueError("paper_stale_valuation_policy is invalid")
        if self.paper_valuation_adjust not in {"", "raw", "qfq", "hfq"}:
            raise ValueError("paper_valuation_adjust is invalid")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path
