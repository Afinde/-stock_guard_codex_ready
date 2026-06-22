from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import tempfile
import threading
import time as time_module
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


class RecoverableProviderError(ProviderError):
    pass


class RateLimitError(ProviderError):
    pass


class CalendarDataProvider(Protocol):
    def load(self) -> "LocalTradingCalendar":
        """Load a local trading calendar."""


class TradingCalendar(Protocol):
    version: str
    source: str

    def is_trading_day(self, day: date) -> bool:
        ...

    def previous_trading_day(self, day: date) -> date:
        ...

    def next_trading_day(self, day: date) -> date:
        ...

    def trading_days(self, start: date, end: date) -> list[date]:
        ...

    def latest_completed_trading_day(self, now: datetime) -> date:
        ...


class MarketDataProvider(Protocol):
    name: str

    def fetch_daily_history(self, symbol: str, lookback_days: int = 240) -> "MarketDataSnapshot":
        """Return a validated daily-bar snapshot with canonical columns and metadata."""


@dataclass(frozen=True)
class LocalTradingCalendar:
    source: str
    trading_day_set: frozenset[date]
    start_date: date
    end_date: date
    updated_at: datetime
    close_time: time = time(15, 0)
    timezone: str = "Asia/Shanghai"
    version: str = ""

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            raise MarketDataError("Trading calendar updated_at must include timezone")
        if self.start_date > self.end_date:
            raise MarketDataError("Trading calendar start_date must not be after end_date")
        if not self.trading_day_set:
            raise MarketDataError("Trading calendar must include at least one trading day")
        if min(self.trading_day_set) < self.start_date or max(self.trading_day_set) > self.end_date:
            raise MarketDataError("Trading calendar days must be inside coverage range")
        if not self.version:
            object.__setattr__(self, "version", self._compute_version())

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        close_time: time = time(15, 0),
        timezone: str = "Asia/Shanghai",
    ) -> "LocalTradingCalendar":
        calendar_path = Path(path)
        if not calendar_path.exists():
            raise MarketDataError(f"Trading calendar file does not exist: {calendar_path}")
        try:
            payload = json.loads(calendar_path.read_text(encoding="utf-8"))
            days = frozenset(date.fromisoformat(value) for value in payload["trading_days"])
            updated_at = datetime.fromisoformat(payload["updated_at"])
            return cls(
                source=str(payload.get("source", calendar_path.name)),
                trading_day_set=days,
                start_date=date.fromisoformat(payload["start_date"]),
                end_date=date.fromisoformat(payload["end_date"]),
                updated_at=updated_at,
                close_time=close_time,
                timezone=timezone,
                version=str(payload.get("version", "")),
            )
        except KeyError as exc:
            raise MarketDataError(f"Trading calendar missing field: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, MarketDataError):
                raise
            raise MarketDataError(f"Failed to load trading calendar: {exc}") from exc

    def save(self, path: str | Path) -> None:
        payload = {
            "source": self.source,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
            "trading_days": [day.isoformat() for day in sorted(self.trading_day_set)],
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    def is_trading_day(self, day: date) -> bool:
        self._require_coverage(day)
        return day in self.trading_day_set

    def previous_trading_day(self, day: date) -> date:
        self._require_coverage(day)
        candidate = day - timedelta(days=1)
        while candidate >= self.start_date:
            if candidate in self.trading_day_set:
                return candidate
            candidate -= timedelta(days=1)
        raise MarketDataError(f"Trading calendar coverage missing before {day}")

    def next_trading_day(self, day: date) -> date:
        self._require_coverage(day)
        candidate = day + timedelta(days=1)
        while candidate <= self.end_date:
            if candidate in self.trading_day_set:
                return candidate
            candidate += timedelta(days=1)
        raise MarketDataError(f"Trading calendar coverage missing after {day}")

    def trading_days(self, start: date, end: date) -> list[date]:
        if start > end:
            raise MarketDataError("trading_days start must not be after end")
        self._require_coverage(start)
        self._require_coverage(end)
        return [day for day in sorted(self.trading_day_set) if start <= day <= end]

    def latest_completed_trading_day(self, now: datetime) -> date:
        current = _ensure_business_datetime(now, self.timezone, reject_naive=True)
        candidate = current.date()
        self._require_coverage(candidate)
        if candidate not in self.trading_day_set or current.time() < self.close_time:
            return self.previous_trading_day(candidate)
        return candidate

    def _require_coverage(self, day: date) -> None:
        if day < self.start_date or day > self.end_date:
            raise MarketDataError(
                f"Trading calendar coverage missing for {day}; "
                f"coverage is {self.start_date} to {self.end_date}"
            )

    def _compute_version(self) -> str:
        payload = {
            "source": self.source,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "trading_days": [day.isoformat() for day in sorted(self.trading_day_set)],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]


@dataclass(frozen=True)
class MarketDataValidationConfig:
    adjust: str = "qfq"
    min_history_bars: int = 80
    max_stale_days: int = 0
    timezone: str = "Asia/Shanghai"
    market_close_time: time = time(15, 0)
    calendar: TradingCalendar | None = None
    cache_schema_version: str = "daily-v1"

    def __post_init__(self) -> None:
        if self.min_history_bars <= 0:
            raise ValueError("min_history_bars must be positive")
        if self.max_stale_days < 0:
            raise ValueError("max_stale_days must not be negative")


@dataclass(frozen=True)
class MarketDataSnapshot:
    bars: pd.DataFrame
    provider: str
    symbol: str
    adjust: str
    first_date: date
    last_date: date
    row_count: int
    fetched_at: datetime
    validated_at: datetime
    data_version: str
    calendar_version: str = ""
    data_checksum: str = ""
    cache_key: str | None = None
    cache_hit: bool = False
    provider_version: str | None = None
    expected_market_date: date | None = None
    actual_market_date: date | None = None


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    jitter_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must not be negative")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least initial_backoff_seconds")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must not be negative")


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    cache_dir: Path = Path(".cache/market_data")
    schema_version: str = "daily-v1"
    refresh_latest: bool = False


FetchCallable = Callable[..., pd.DataFrame]
ClockCallable = Callable[[], datetime]
SleepCallable = Callable[[float], None]
RandomCallable = Callable[[], float]


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().lower().replace("sh", "").replace("sz", "")
    if not (value.isdigit() and len(value) == 6):
        raise ValueError(f"Invalid A-share symbol: {symbol}")
    return value


class RateLimiter:
    def __init__(
        self,
        *,
        requests_per_second: int = 2,
        requests_per_minute: int = 60,
        max_concurrency: int = 2,
        clock: Callable[[], float] | None = None,
        sleep: SleepCallable | None = None,
    ) -> None:
        if requests_per_second <= 0 or requests_per_minute <= 0 or max_concurrency <= 0:
            raise ValueError("rate limiter limits must be positive")
        self.requests_per_second = requests_per_second
        self.requests_per_minute = requests_per_minute
        self.max_concurrency = max_concurrency
        self._clock = clock or time_module.monotonic
        self._sleep = sleep or time_module.sleep
        self._lock = threading.Lock()
        self._second_events: list[float] = []
        self._minute_events: list[float] = []
        self._active = 0

    def acquire(self, *, provider: str, symbol: str) -> None:
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._second_events = [event for event in self._second_events if now - event < 1.0]
                self._minute_events = [event for event in self._minute_events if now - event < 60.0]
                if self._active >= self.max_concurrency:
                    raise RateLimitError(f"Provider concurrency limit reached for {provider}")
                wait_for = self._wait_seconds(now)
                if wait_for <= 0:
                    self._active += 1
                    self._second_events.append(now)
                    self._minute_events.append(now)
                    if waited > 0:
                        logger.info(
                            "provider_rate_limit_wait",
                            extra={"provider": provider, "symbol": symbol, "wait_seconds": waited},
                        )
                    return
            waited += wait_for
            self._sleep(wait_for)

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _wait_seconds(self, now: float) -> float:
        waits: list[float] = []
        if len(self._second_events) >= self.requests_per_second:
            waits.append(1.0 - (now - self._second_events[0]))
        if len(self._minute_events) >= self.requests_per_minute:
            waits.append(60.0 - (now - self._minute_events[0]))
        return max([0.0, *waits])


class AKShareMarketDataProvider:
    name = "akshare"

    def __init__(
        self,
        config: MarketDataValidationConfig | None = None,
        fetcher: FetchCallable | None = None,
        clock: ClockCallable | None = None,
        retry_config: RetryConfig | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: SleepCallable | None = None,
        random_fn: RandomCallable | None = None,
    ) -> None:
        self.config = config or MarketDataValidationConfig()
        self._fetcher = fetcher
        self._clock = clock or (lambda: business_now(self.config.timezone))
        self.retry_config = retry_config or RetryConfig()
        self.rate_limiter = rate_limiter
        self._sleep = sleep or time_module.sleep
        self._random = random_fn or random.random
        self.provider_version = _akshare_version() if fetcher is None else "fixture"

    def fetch_daily_history(self, symbol: str, lookback_days: int = 240) -> MarketDataSnapshot:
        normalized_symbol = normalize_symbol(symbol)
        fetched_at = _ensure_business_datetime(self._clock(), self.config.timezone, reject_naive=True)
        expected_trade_date = _latest_completed_trade_date(fetched_at, self.config)
        try:
            raw = self._call_with_retries(normalized_symbol, lookback_days, expected_trade_date)
        except MarketDataError:
            raise
        except ProviderError as exc:
            raise MarketDataError(f"Failed to fetch {symbol}: {exc}") from exc

        return normalize_and_validate_daily_bars(
            raw,
            symbol=normalized_symbol,
            provider=self.name,
            lookback_days=lookback_days,
            config=self.config,
            fetched_at=fetched_at,
            expected_trade_date=expected_trade_date,
            provider_version=self.provider_version,
        )

    def _call_with_retries(self, symbol: str, lookback_days: int, expected_trade_date: date) -> pd.DataFrame:
        attempt = 1
        while True:
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire(provider=self.name, symbol=symbol)
                try:
                    return self._fetch_raw(symbol, lookback_days, expected_trade_date)
                finally:
                    if self.rate_limiter is not None:
                        self.rate_limiter.release()
            except MarketDataError:
                raise
            except (RecoverableProviderError, TimeoutError, ConnectionError) as exc:
                logger.warning(
                    "market_provider_retryable_failure",
                    extra={
                        "attempt": attempt,
                        "provider": self.name,
                        "symbol": symbol,
                        "exception_class": exc.__class__.__name__,
                    },
                )
                if attempt >= self.retry_config.max_attempts:
                    raise ProviderError(f"provider failed after {attempt} attempts: {exc}") from exc
                self._sleep(_backoff_seconds(self.retry_config, attempt, self._random))
                attempt += 1
            except Exception:
                logger.exception(
                    "Unexpected provider failure",
                    extra={"attempt": attempt, "provider": self.name, "symbol": symbol},
                )
                raise

    def _fetch_raw(
        self,
        symbol: str,
        lookback_days: int,
        expected_trade_date: date,
    ) -> pd.DataFrame:
        fetcher = self._fetcher
        if fetcher is None:
            try:
                import akshare as ak
            except Exception as exc:
                raise MarketDataError(f"AKShare is unavailable: {exc}") from exc
            fetcher = ak.stock_zh_a_hist

        start = expected_trade_date - timedelta(days=lookback_days * 2)
        return fetcher(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=expected_trade_date.strftime("%Y%m%d"),
            adjust=self.config.adjust,
        )


class CachedMarketDataProvider:
    name = "cached"

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        config: MarketDataValidationConfig,
        cache_config: CacheConfig,
        clock: ClockCallable | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.cache_config = cache_config
        self._clock = clock or (lambda: business_now(config.timezone))
        self.name = provider.name

    def fetch_daily_history(self, symbol: str, lookback_days: int = 240) -> MarketDataSnapshot:
        if not self.cache_config.enabled:
            return self.provider.fetch_daily_history(symbol, lookback_days=lookback_days)
        normalized_symbol = normalize_symbol(symbol)
        now = _ensure_business_datetime(self._clock(), self.config.timezone, reject_naive=True)
        expected = _latest_completed_trade_date(now, self.config)
        start = expected - timedelta(days=lookback_days * 2)
        cache_key = build_cache_key(
            provider=self.provider.name,
            symbol=normalized_symbol,
            start_date=start,
            end_date=expected,
            frequency="daily",
            adjust=self.config.adjust,
            schema_version=self.cache_config.schema_version,
        )
        path = self._cache_path(cache_key)
        if path.exists() and not self._must_refresh(expected):
            try:
                snapshot = self._read_cache(path, normalized_symbol, lookback_days, cache_key, now, expected)
                logger.info(
                    "market_cache_hit",
                    extra={"provider": self.provider.name, "symbol": normalized_symbol, "cache_key": cache_key},
                )
                return snapshot
            except MarketDataError:
                logger.exception(
                    "market_cache_corrupt",
                    extra={"provider": self.provider.name, "symbol": normalized_symbol, "cache_key": cache_key},
                )
                raise
        logger.info(
            "market_cache_miss",
            extra={"provider": self.provider.name, "symbol": normalized_symbol, "cache_key": cache_key},
        )
        snapshot = self.provider.fetch_daily_history(normalized_symbol, lookback_days=lookback_days)
        snapshot = replace(snapshot, cache_key=cache_key, cache_hit=False)
        self._write_cache(path, snapshot)
        return snapshot

    def _must_refresh(self, expected: date) -> bool:
        return self.cache_config.refresh_latest and expected == _latest_completed_trade_date(
            _ensure_business_datetime(self._clock(), self.config.timezone, reject_naive=True), self.config
        )

    def _cache_path(self, cache_key: str) -> Path:
        return self.cache_config.cache_dir / f"{cache_key}.json"

    def _read_cache(
        self,
        path: Path,
        symbol: str,
        lookback_days: int,
        cache_key: str,
        fetched_at: datetime,
        expected: date,
    ) -> MarketDataSnapshot:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            bars = pd.read_json(StringIO(payload["bars"]), orient="split")
        except Exception as exc:
            raise MarketDataError(f"Corrupt market data cache for {symbol}: {exc}") from exc
        snapshot = normalize_and_validate_daily_bars(
            bars,
            symbol=symbol,
            provider=self.provider.name,
            lookback_days=lookback_days,
            config=self.config,
            fetched_at=fetched_at,
            expected_trade_date=expected,
            provider_version=payload.get("provider_version"),
            cache_key=cache_key,
            cache_hit=True,
        )
        if payload.get("data_checksum") != snapshot.data_checksum:
            raise MarketDataError(f"Corrupt market data cache for {symbol}: checksum mismatch")
        return snapshot

    def _write_cache(self, path: Path, snapshot: MarketDataSnapshot) -> None:
        payload = {
            "created_at": utc_now().isoformat(),
            "provider": snapshot.provider,
            "provider_version": snapshot.provider_version,
            "symbol": snapshot.symbol,
            "adjust": snapshot.adjust,
            "schema_version": self.cache_config.schema_version,
            "data_last_date": snapshot.last_date.isoformat(),
            "row_count": snapshot.row_count,
            "data_checksum": snapshot.data_checksum,
            "bars": snapshot.bars.to_json(orient="split", date_format="iso"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def normalize_and_validate_daily_bars(
    raw: pd.DataFrame | None,
    *,
    symbol: str,
    provider: str,
    lookback_days: int,
    config: MarketDataValidationConfig,
    fetched_at: datetime | None = None,
    expected_trade_date: date | None = None,
    provider_version: str | None = None,
    cache_key: str | None = None,
    cache_hit: bool = False,
) -> MarketDataSnapshot:
    if raw is None or raw.empty:
        raise MarketDataError(f"No market data for {symbol}")
    if lookback_days <= 0:
        raise MarketDataError(f"Invalid lookback_days for {symbol}: {lookback_days}")

    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
    }
    df = raw.rename(columns=rename_map)
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise MarketDataError(f"Missing columns for {symbol}: {missing}")

    df = df[required].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    _validate_required_values(df, symbol)
    _validate_time_order(df, symbol)
    _validate_ohlc(df, symbol)
    fetched_at = _ensure_business_datetime(fetched_at or business_now(config.timezone), config.timezone, reject_naive=True)
    expected_trade_date = expected_trade_date or _latest_completed_trade_date(fetched_at, config)
    _validate_expected_trade_date(df, symbol, expected_trade_date)
    _validate_staleness(df, symbol, config, expected_trade_date=expected_trade_date)

    output = df.tail(lookback_days).reset_index(drop=True)
    if len(output) < config.min_history_bars:
        raise MarketDataError(
            f"Insufficient history for {symbol}; "
            f"need at least {config.min_history_bars} bars, got {len(output)}"
        )

    validated_at = business_now(config.timezone)
    checksum = market_data_checksum(output)
    actual = output["date"].iloc[-1].date()
    calendar_version = config.calendar.version if config.calendar is not None else ""
    return MarketDataSnapshot(
        bars=output,
        provider=provider,
        symbol=symbol,
        adjust=config.adjust,
        first_date=output["date"].iloc[0].date(),
        last_date=actual,
        row_count=len(output),
        fetched_at=fetched_at,
        validated_at=validated_at,
        data_version=f"{provider}:daily:{config.adjust}",
        calendar_version=calendar_version,
        data_checksum=checksum,
        cache_key=cache_key,
        cache_hit=cache_hit,
        provider_version=provider_version,
        expected_market_date=expected_trade_date,
        actual_market_date=actual,
    )


def fetch_daily_history(
    symbol: str,
    lookback_days: int = 240,
    provider: MarketDataProvider | None = None,
    config: MarketDataValidationConfig | None = None,
    cache_config: CacheConfig | None = None,
    retry_config: RetryConfig | None = None,
    rate_limiter: RateLimiter | None = None,
) -> MarketDataSnapshot:
    data_config = config or MarketDataValidationConfig()
    data_provider = provider or AKShareMarketDataProvider(
        config=data_config,
        retry_config=retry_config,
        rate_limiter=rate_limiter,
    )
    if cache_config is not None:
        data_provider = CachedMarketDataProvider(data_provider, config=data_config, cache_config=cache_config)
    return data_provider.fetch_daily_history(symbol, lookback_days=lookback_days)


def build_cache_key(
    *,
    provider: str,
    symbol: str,
    start_date: date,
    end_date: date,
    frequency: str,
    adjust: str,
    schema_version: str,
) -> str:
    payload = {
        "provider": provider,
        "symbol": symbol,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "frequency": frequency,
        "adjust": adjust,
        "schema_version": schema_version,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest[:32]


def market_data_checksum(df: pd.DataFrame) -> str:
    canonical = df[["date", "open", "high", "low", "close", "volume"]].copy()
    canonical["date"] = pd.to_datetime(canonical["date"]).dt.strftime("%Y-%m-%d")
    for column in ["open", "high", "low", "close", "volume"]:
        canonical[column] = canonical[column].astype(float).map(lambda value: f"{value:.10f}")
    rows = canonical.to_dict(orient="records")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_required_values(df: pd.DataFrame, symbol: str) -> None:
    if df.isnull().any().any():
        columns = df.columns[df.isnull().any()].tolist()
        raise MarketDataError(f"Missing or invalid values for {symbol}: {columns}")

    numeric_columns = ["open", "high", "low", "close", "volume"]
    values = df[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise MarketDataError(f"Non-finite market data values for {symbol}")


def _validate_time_order(df: pd.DataFrame, symbol: str) -> None:
    if df["date"].duplicated().any():
        raise MarketDataError(f"Duplicate bars for {symbol}")
    if not df["date"].is_monotonic_increasing:
        raise MarketDataError(f"Bars are not sorted by date for {symbol}")


def _validate_ohlc(df: pd.DataFrame, symbol: str) -> None:
    price_columns = ["open", "high", "low", "close"]
    if (df[price_columns] <= 0).any().any():
        raise MarketDataError(f"OHLC prices must be positive for {symbol}")
    if (df["volume"] < 0).any():
        raise MarketDataError(f"Volume must not be negative for {symbol}")

    high_is_valid = df["high"] >= df[["open", "low", "close"]].max(axis=1)
    low_is_valid = df["low"] <= df[["open", "high", "close"]].min(axis=1)
    if not high_is_valid.all() or not low_is_valid.all():
        raise MarketDataError(f"Invalid OHLC relationship for {symbol}")


def _validate_staleness(
    df: pd.DataFrame,
    symbol: str,
    config: MarketDataValidationConfig,
    *,
    expected_trade_date: date,
) -> None:
    latest_date = df["date"].iloc[-1].date()
    if latest_date == expected_trade_date:
        return
    if latest_date > expected_trade_date:
        return
    if config.calendar is None:
        raise MarketDataError(
            f"Stale market data for {symbol}; actual {latest_date}, expected {expected_trade_date}"
        )
    tolerated_days = config.calendar.trading_days(latest_date, expected_trade_date)
    lag = max(0, len(tolerated_days) - 1)
    if lag > config.max_stale_days:
        raise MarketDataError(
            f"Stale market data for {symbol}; expected_market_date={expected_trade_date}, "
            f"actual_market_date={latest_date}, stale_trading_days={lag}"
        )


def _validate_expected_trade_date(
    df: pd.DataFrame,
    symbol: str,
    expected_trade_date: date,
) -> None:
    latest_date = df["date"].iloc[-1].date()
    if latest_date > expected_trade_date:
        raise MarketDataError(
            f"Future market data for {symbol}: actual_market_date={latest_date}; "
            f"expected_market_date={expected_trade_date}"
        )


def latest_completed_a_share_trading_date(
    now: datetime | None,
    config: MarketDataValidationConfig,
) -> date:
    current = _ensure_business_datetime(now, config.timezone, reject_naive=True)
    return _latest_completed_trade_date(current, config)


def is_a_share_trading_day(day: date, config: MarketDataValidationConfig) -> bool:
    if config.calendar is None:
        raise MarketDataError("Trading calendar is required")
    return config.calendar.is_trading_day(day)


def business_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def utc_now() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def _ensure_business_datetime(value: datetime | None, timezone: str, *, reject_naive: bool = False) -> datetime:
    if value is None:
        return business_now(timezone)
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        if reject_naive:
            raise MarketDataError("Business datetime must include timezone")
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _latest_completed_trade_date(now: datetime, config: MarketDataValidationConfig) -> date:
    if config.calendar is None:
        raise MarketDataError("Trading calendar is required")
    return config.calendar.latest_completed_trading_day(now)


def _backoff_seconds(config: RetryConfig, attempt: int, random_fn: RandomCallable) -> float:
    base = min(
        config.max_backoff_seconds,
        config.initial_backoff_seconds * (2 ** max(0, attempt - 1)),
    )
    return base + (random_fn() * config.jitter_seconds if config.jitter_seconds else 0.0)


def _akshare_version() -> str | None:
    try:
        import akshare as ak
    except Exception:
        return None
    return getattr(ak, "__version__", None)
