from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .data_provider import (
    CacheConfig,
    LocalTradingCalendar,
    MarketDataError,
    MarketDataValidationConfig,
    RateLimiter,
    RetryConfig,
    fetch_daily_history,
    utc_now,
)
from .db import SessionLocal, SignalRecord
from .risk import signal_identity
from .strategy import Signal, generate_signal, strategy_config_from_settings

logger = logging.getLogger(__name__)


def scan_watchlist() -> list[dict]:
    settings = get_settings()
    data_config = market_data_config_from_settings(settings)
    cache_config = cache_config_from_settings(settings)
    retry_config = retry_config_from_settings(settings)
    rate_limiter = rate_limiter_from_settings(settings)
    strategy_config = strategy_config_from_settings(settings)
    results: list[dict] = []

    for symbol in settings.watchlist:
        try:
            snapshot = fetch_daily_history(
                symbol,
                config=data_config,
                cache_config=cache_config,
                retry_config=retry_config,
                rate_limiter=rate_limiter,
            )
            signal = generate_signal(
                symbol=symbol,
                market_data=snapshot,
                account_equity=settings.account_equity,
                risk_per_trade=settings.risk_per_trade,
                max_single_position_pct=settings.max_single_position_pct,
                config=strategy_config,
            )
            save_signal(signal)
            results.append(signal.to_dict())
        except MarketDataError as exc:
            results.append({"symbol": symbol, "action": "DATA_ERROR", "reason": str(exc)})
        except Exception:
            logger.exception("Unexpected scan failure for symbol %s", symbol)
            raise

    return sorted(results, key=lambda x: x.get("score", -1), reverse=True)


def save_signal(signal: Signal) -> None:
    dedupe_key = signal_dedupe_key(signal)
    with SessionLocal() as session:
        existing = session.scalars(
            select(SignalRecord).where(SignalRecord.dedupe_key == dedupe_key).limit(1)
        ).first()
        if existing is not None:
            return
        record = SignalRecord(
            symbol=signal.symbol,
            generated_at=signal.signal_generated_at,
            signal_generated_at=signal.signal_generated_at,
            db_written_at=utc_now(),
            market_trade_date=signal.market_trade_date.isoformat(),
            market_fetched_at=signal.market_fetched_at,
            strategy_name=signal.strategy_name,
            strategy_version=signal.strategy_version,
            parameter_version=signal.parameter_version,
            parameter_snapshot=signal.parameter_snapshot,
            market_as_of_date=signal.market_as_of_date.isoformat(),
            market_data_source=signal.market_data_source,
            market_data_adjust=signal.market_data_adjust,
            signal_type=signal.signal_type,
            score_breakdown=json.dumps(signal.score_breakdown, ensure_ascii=False, sort_keys=True),
            reasons=json.dumps(signal.reasons, ensure_ascii=False),
            invalidation_conditions=json.dumps(signal.invalidation_conditions, ensure_ascii=False),
            reference_price=signal.reference_price,
            stop_loss_price=signal.stop_loss_price,
            take_profit_1_price=signal.take_profit_1_price,
            take_profit_2_price=signal.take_profit_2_price,
            market_data_checksum=signal.market_data_checksum,
            market_calendar_version=signal.market_calendar_version,
            dedupe_key=dedupe_key,
            action=signal.action,
            score=signal.score,
            price=signal.price,
            stop_price=signal.stop_price,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            suggested_shares=signal.suggested_shares,
            reason=signal.reason,
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def latest_signals(limit: int = 50) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(SignalRecord).order_by(SignalRecord.generated_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "symbol": row.symbol,
                "generated_at": _isoformat_aware(row.generated_at),
                "signal_generated_at": _isoformat_aware(row.signal_generated_at),
                "db_written_at": _isoformat_aware(row.db_written_at),
                "market_trade_date": row.market_trade_date,
                "market_fetched_at": _isoformat_aware(row.market_fetched_at),
                "strategy_name": row.strategy_name,
                "strategy_version": row.strategy_version,
                "parameter_version": row.parameter_version,
                "parameter_snapshot": _json_loads(row.parameter_snapshot, default={}),
                "market_as_of_date": row.market_as_of_date or row.market_trade_date,
                "market_data_source": row.market_data_source,
                "market_data_adjust": row.market_data_adjust,
                "signal_type": row.signal_type or row.action,
                "score_breakdown": _json_loads(row.score_breakdown, default={}),
                "reasons": _json_loads(row.reasons, default=[]),
                "invalidation_conditions": _json_loads(row.invalidation_conditions, default=[]),
                "reference_price": row.reference_price if row.reference_price is not None else row.price,
                "stop_loss_price": row.stop_loss_price if row.stop_loss_price is not None else row.stop_price,
                "take_profit_1_price": row.take_profit_1_price if row.take_profit_1_price is not None else row.take_profit_1,
                "take_profit_2_price": row.take_profit_2_price if row.take_profit_2_price is not None else row.take_profit_2,
                "market_data_checksum": row.market_data_checksum,
                "market_calendar_version": row.market_calendar_version,
                "action": row.action,
                "score": row.score,
                "price": row.price,
                "stop_price": row.stop_price,
                "take_profit_1": row.take_profit_1,
                "take_profit_2": row.take_profit_2,
                "suggested_shares": row.suggested_shares,
                "reason": row.reason,
            }
            for row in rows
        ]


def _isoformat_aware(value: datetime) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.isoformat()


def signal_dedupe_key(signal: Signal) -> str:
    return signal_identity(signal)


def _json_loads(value: str | None, *, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def market_data_config_from_settings(settings) -> MarketDataValidationConfig:
    calendar = LocalTradingCalendar.from_file(
        getattr(settings, "market_calendar_resolved_path", getattr(settings, "market_calendar_path", "app/resources/a_share_calendar.json")),
        close_time=getattr(settings, "market_close_time_value", None)
        or _parse_time(getattr(settings, "market_close_time", "15:00")),
        timezone=settings.timezone,
    )
    return MarketDataValidationConfig(
        adjust=settings.market_data_adjust,
        min_history_bars=settings.market_data_min_history_bars,
        max_stale_days=settings.market_data_max_stale_days,
        timezone=settings.timezone,
        market_close_time=calendar.close_time,
        calendar=calendar,
        cache_schema_version=getattr(settings, "market_cache_schema_version", "daily-v1"),
    )


def cache_config_from_settings(settings) -> CacheConfig:
    cache_dir = getattr(settings, "market_cache_resolved_dir", None) or getattr(
        settings, "market_cache_dir", ".cache/market_data"
    )
    return CacheConfig(
        enabled=getattr(settings, "market_cache_enabled", False),
        cache_dir=Path(cache_dir),
        schema_version=getattr(settings, "market_cache_schema_version", "daily-v1"),
        refresh_latest=getattr(settings, "market_cache_refresh_latest", False),
    )


def retry_config_from_settings(settings) -> RetryConfig:
    return RetryConfig(
        max_attempts=getattr(settings, "market_provider_max_attempts", 3),
        initial_backoff_seconds=getattr(settings, "market_provider_initial_backoff_seconds", 1.0),
        max_backoff_seconds=getattr(settings, "market_provider_max_backoff_seconds", 8.0),
        jitter_seconds=getattr(settings, "market_provider_jitter_seconds", 0.1),
    )


@lru_cache
def _shared_rate_limiter(
    requests_per_second: int,
    requests_per_minute: int,
    max_concurrency: int,
) -> RateLimiter:
    return RateLimiter(
        requests_per_second=requests_per_second,
        requests_per_minute=requests_per_minute,
        max_concurrency=max_concurrency,
    )


def rate_limiter_from_settings(settings) -> RateLimiter:
    return _shared_rate_limiter(
        getattr(settings, "market_provider_requests_per_second", 2),
        getattr(settings, "market_provider_requests_per_minute", 60),
        getattr(settings, "market_provider_max_concurrency", 2),
    )


def _parse_time(value: str):
    from datetime import time

    return time.fromisoformat(value)
