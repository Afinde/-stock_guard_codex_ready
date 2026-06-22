from __future__ import annotations

import json
from io import StringIO
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data_provider import (
    AKShareMarketDataProvider,
    CacheConfig,
    CachedMarketDataProvider,
    LocalTradingCalendar,
    MarketDataError,
    MarketDataValidationConfig,
    RateLimitError,
    RateLimiter,
    RecoverableProviderError,
    RetryConfig,
    build_cache_key,
    market_data_checksum,
    normalize_and_validate_daily_bars,
)


TZ = ZoneInfo("Asia/Shanghai")


def calendar() -> LocalTradingCalendar:
    days = {
        date(2026, 4, 30),
        date(2026, 5, 6),
        date(2026, 6, 15),
        date(2026, 6, 16),
        date(2026, 6, 17),
        date(2026, 6, 18),
        date(2026, 6, 19),
        date(2026, 6, 22),
    }
    return LocalTradingCalendar(
        source="test",
        trading_day_set=frozenset(days),
        start_date=date(2026, 4, 30),
        end_date=date(2026, 6, 22),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        timezone="Asia/Shanghai",
        version="test-calendar-v1",
    )


def make_raw_bars(periods: int = 90, end: str = "2026-06-18") -> pd.DataFrame:
    dates = pd.date_range(end=end, periods=periods, freq="D")
    close = [10 + index * 0.01 for index in range(periods)]
    return pd.DataFrame(
        {
            "日期": dates.strftime("%Y-%m-%d"),
            "开盘": close,
            "最高": [price + 0.2 for price in close],
            "最低": [price - 0.2 for price in close],
            "收盘": [price + 0.05 for price in close],
            "成交量": [1000 + index for index in range(periods)],
        }
    )


def config(*, min_history_bars: int = 80, max_stale_days: int = 0) -> MarketDataValidationConfig:
    return MarketDataValidationConfig(
        adjust="qfq",
        min_history_bars=min_history_bars,
        max_stale_days=max_stale_days,
        timezone="Asia/Shanghai",
        calendar=calendar(),
    )


def validate(raw: pd.DataFrame, *, min_history_bars: int = 80, max_stale_days: int = 0):
    return normalize_and_validate_daily_bars(
        raw,
        symbol="600519",
        provider="test",
        lookback_days=240,
        config=config(min_history_bars=min_history_bars, max_stale_days=max_stale_days),
        fetched_at=datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
    )


def test_calendar_weekend_latest_completed_trading_day():
    assert calendar().latest_completed_trading_day(datetime(2026, 6, 20, 10, 0, tzinfo=TZ)) == date(2026, 6, 19)


def test_calendar_holiday_latest_completed_trading_day():
    assert calendar().latest_completed_trading_day(datetime(2026, 5, 1, 10, 0, tzinfo=TZ)) == date(2026, 4, 30)


def test_calendar_before_close_returns_previous_trading_day():
    assert calendar().latest_completed_trading_day(datetime(2026, 6, 18, 14, 59, tzinfo=TZ)) == date(2026, 6, 17)


def test_calendar_after_close_returns_current_trading_day():
    assert calendar().latest_completed_trading_day(datetime(2026, 6, 18, 15, 1, tzinfo=TZ)) == date(2026, 6, 18)


def test_calendar_rejects_naive_datetime():
    with pytest.raises(MarketDataError, match="timezone"):
        calendar().latest_completed_trading_day(datetime(2026, 6, 18, 15, 1))


def test_calendar_coverage_gap_fails_closed():
    with pytest.raises(MarketDataError, match="coverage missing"):
        calendar().latest_completed_trading_day(datetime(2026, 7, 1, 10, 0, tzinfo=TZ))


def test_akshare_adapter_uses_configured_adjustment_and_returns_metadata():
    calls = []

    def fake_fetcher(**kwargs):
        calls.append(kwargs)
        return make_raw_bars()

    provider = AKShareMarketDataProvider(
        config=MarketDataValidationConfig(adjust="hfq", min_history_bars=80, calendar=calendar()),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        retry_config=RetryConfig(jitter_seconds=0),
    )

    snapshot = provider.fetch_daily_history("sh600519", lookback_days=90)

    assert calls[0]["symbol"] == "600519"
    assert calls[0]["adjust"] == "hfq"
    assert calls[0]["end_date"] == "20260618"
    assert snapshot.provider == "akshare"
    assert snapshot.adjust == "hfq"
    assert snapshot.first_date == date(2026, 3, 21)
    assert snapshot.last_date == date(2026, 6, 18)
    assert snapshot.row_count == 90
    assert snapshot.fetched_at.isoformat() == "2026-06-18T16:00:00+08:00"
    assert snapshot.validated_at.tzinfo is not None
    assert snapshot.calendar_version == "test-calendar-v1"
    assert snapshot.expected_market_date == date(2026, 6, 18)
    assert snapshot.actual_market_date == date(2026, 6, 18)
    assert snapshot.data_checksum
    assert list(snapshot.bars.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_provider_failure_fails_closed_after_retries():
    attempts = []

    def fake_fetcher(**_kwargs):
        attempts.append(1)
        raise TimeoutError("network timeout")

    provider = AKShareMarketDataProvider(
        config=config(),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        retry_config=RetryConfig(max_attempts=2, initial_backoff_seconds=0, max_backoff_seconds=0, jitter_seconds=0),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(MarketDataError, match="Failed to fetch"):
        provider.fetch_daily_history("600519")
    assert len(attempts) == 2


def test_missing_required_column_fails_closed():
    raw = make_raw_bars().drop(columns=["成交量"])

    with pytest.raises(MarketDataError, match="Missing columns"):
        validate(raw)


def test_invalid_numeric_value_fails_closed():
    raw = make_raw_bars()
    raw["收盘"] = raw["收盘"].astype(object)
    raw.loc[0, "收盘"] = "bad-price"

    with pytest.raises(MarketDataError, match="Missing or invalid values"):
        validate(raw)


def test_duplicate_dates_fail_closed():
    raw = make_raw_bars()
    raw.loc[1, "日期"] = raw.loc[0, "日期"]

    with pytest.raises(MarketDataError, match="Duplicate bars"):
        validate(raw)


def test_unsorted_dates_fail_closed():
    raw = make_raw_bars()
    raw.loc[[0, 1], "日期"] = raw.loc[[1, 0], "日期"].to_list()

    with pytest.raises(MarketDataError, match="not sorted"):
        validate(raw)


def test_invalid_ohlc_relationship_fails_closed():
    raw = make_raw_bars()
    raw.loc[10, "最高"] = raw.loc[10, "最低"] - 1

    with pytest.raises(MarketDataError, match="Invalid OHLC"):
        validate(raw)


def test_weekend_does_not_mark_friday_market_data_stale():
    snapshot = normalize_and_validate_daily_bars(
        make_raw_bars(end="2026-06-19"),
        symbol="600519",
        provider="test",
        lookback_days=90,
        config=config(),
        fetched_at=datetime(2026, 6, 20, 10, 0, tzinfo=TZ),
    )

    assert snapshot.last_date == date(2026, 6, 19)


def test_holiday_does_not_mark_previous_market_data_stale():
    snapshot = normalize_and_validate_daily_bars(
        make_raw_bars(end="2026-04-30"),
        symbol="600519",
        provider="test",
        lookback_days=90,
        config=config(),
        fetched_at=datetime(2026, 5, 1, 10, 0, tzinfo=TZ),
    )

    assert snapshot.last_date == date(2026, 4, 30)


def test_stale_market_data_fails_closed():
    raw = make_raw_bars(end="2026-06-17")

    with pytest.raises(MarketDataError, match="Stale market data"):
        validate(raw)


def test_future_market_data_fails_closed():
    raw = make_raw_bars(end="2026-06-19")

    with pytest.raises(MarketDataError, match="Future market data"):
        validate(raw)


def test_insufficient_history_fails_closed():
    raw = make_raw_bars(periods=40)

    with pytest.raises(MarketDataError, match="Insufficient history"):
        validate(raw, min_history_bars=80)


def test_adapter_uses_previous_trade_date_before_market_close():
    calls = []

    def fake_fetcher(**kwargs):
        calls.append(kwargs)
        return make_raw_bars(end="2026-06-17")

    provider = AKShareMarketDataProvider(
        config=config(),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 14, 59, tzinfo=TZ),
        retry_config=RetryConfig(jitter_seconds=0),
    )

    snapshot = provider.fetch_daily_history("600519")

    assert calls[0]["end_date"] == "20260617"
    assert snapshot.last_date == date(2026, 6, 17)


def test_same_data_checksum_is_stable_and_value_change_changes_it():
    first = validate(make_raw_bars()).data_checksum
    second = validate(make_raw_bars()).data_checksum
    changed = make_raw_bars()
    changed.loc[len(changed) - 1, "收盘"] += 0.01

    assert first == second
    assert market_data_checksum(validate(changed).bars) != first


class CountingProvider:
    name = "fixture"

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def fetch_daily_history(self, symbol: str, lookback_days: int = 240):
        self.calls += 1
        return self.snapshot


def cache_provider(tmp_path: Path, *, adjust: str = "qfq", refresh_latest: bool = False):
    snapshot = validate(make_raw_bars())
    cfg = replace_config(adjust=adjust)
    provider = CountingProvider(snapshot)
    cached = CachedMarketDataProvider(
        provider,
        config=cfg,
        cache_config=CacheConfig(
            enabled=True,
            cache_dir=tmp_path,
            schema_version="daily-v1",
            refresh_latest=refresh_latest,
        ),
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
    )
    return cached, provider


def replace_config(**kwargs):
    values = {
        "adjust": "qfq",
        "min_history_bars": 80,
        "max_stale_days": 0,
        "timezone": "Asia/Shanghai",
        "calendar": calendar(),
    }
    values.update(kwargs)
    return MarketDataValidationConfig(**values)


def test_same_cache_request_hits(tmp_path):
    cached, provider = cache_provider(tmp_path)

    first = cached.fetch_daily_history("600519", lookback_days=90)
    second = cached.fetch_daily_history("600519", lookback_days=90)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert provider.calls == 1


def test_different_adjust_and_date_range_have_different_cache_keys():
    base = build_cache_key(
        provider="fixture",
        symbol="600519",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 18),
        frequency="daily",
        adjust="qfq",
        schema_version="daily-v1",
    )
    other_adjust = build_cache_key(
        provider="fixture",
        symbol="600519",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 18),
        frequency="daily",
        adjust="hfq",
        schema_version="daily-v1",
    )
    other_range = build_cache_key(
        provider="fixture",
        symbol="600519",
        start_date=date(2026, 1, 2),
        end_date=date(2026, 6, 18),
        frequency="daily",
        adjust="qfq",
        schema_version="daily-v1",
    )

    assert base != other_adjust
    assert base != other_range


def test_cache_hit_does_not_call_online_provider(tmp_path):
    cached, provider = cache_provider(tmp_path)

    cached.fetch_daily_history("600519", lookback_days=90)
    cached.fetch_daily_history("600519", lookback_days=90)

    assert provider.calls == 1


def test_cache_corruption_fails_before_strategy(tmp_path):
    cached, _provider = cache_provider(tmp_path)
    cached.fetch_daily_history("600519", lookback_days=90)
    cache_file = next(tmp_path.glob("*.json"))
    cache_file.write_text("{bad json", encoding="utf-8")

    with pytest.raises(MarketDataError, match="Corrupt market data cache"):
        cached.fetch_daily_history("600519", lookback_days=90)


def test_cache_content_is_validated_on_read(tmp_path):
    cached, _provider = cache_provider(tmp_path)
    cached.fetch_daily_history("600519", lookback_days=90)
    cache_file = next(tmp_path.glob("*.json"))
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    bars = pd.read_json(StringIO(payload["bars"]), orient="split")
    bars.loc[0, "date"] = bars.loc[1, "date"]
    payload["bars"] = bars.to_json(orient="split", date_format="iso")
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MarketDataError):
        cached.fetch_daily_history("600519", lookback_days=90)


def test_network_temporary_error_retries_without_real_sleep():
    attempts = []
    sleeps = []

    def fake_fetcher(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RecoverableProviderError("temporary")
        return make_raw_bars()

    provider = AKShareMarketDataProvider(
        config=config(),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        retry_config=RetryConfig(max_attempts=3, initial_backoff_seconds=1, max_backoff_seconds=2, jitter_seconds=0),
        sleep=lambda seconds: sleeps.append(seconds),
    )

    provider.fetch_daily_history("600519")

    assert len(attempts) == 2
    assert sleeps == [1]


def test_data_validation_error_is_not_retried():
    attempts = []

    def fake_fetcher(**_kwargs):
        attempts.append(1)
        return make_raw_bars(end="2026-06-19")

    provider = AKShareMarketDataProvider(
        config=config(),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        retry_config=RetryConfig(max_attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0, jitter_seconds=0),
    )

    with pytest.raises(MarketDataError, match="Future market data"):
        provider.fetch_daily_history("600519")
    assert len(attempts) == 1


def test_unexpected_exception_is_not_retried_or_masked():
    attempts = []

    def fake_fetcher(**_kwargs):
        attempts.append(1)
        raise RuntimeError("bug")

    provider = AKShareMarketDataProvider(
        config=config(),
        fetcher=fake_fetcher,
        clock=lambda: datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        retry_config=RetryConfig(max_attempts=3),
    )

    with pytest.raises(RuntimeError, match="bug"):
        provider.fetch_daily_history("600519")
    assert len(attempts) == 1


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_limits_per_second_with_controlled_clock():
    fake = FakeClock()
    limiter = RateLimiter(requests_per_second=1, requests_per_minute=60, max_concurrency=1, clock=fake.clock, sleep=fake.sleep)

    limiter.acquire(provider="fixture", symbol="600519")
    limiter.release()
    limiter.acquire(provider="fixture", symbol="600519")

    assert fake.sleeps == [1.0]


def test_rate_limiter_limits_per_minute_with_controlled_clock():
    fake = FakeClock()
    limiter = RateLimiter(requests_per_second=60, requests_per_minute=1, max_concurrency=1, clock=fake.clock, sleep=fake.sleep)

    limiter.acquire(provider="fixture", symbol="600519")
    limiter.release()
    limiter.acquire(provider="fixture", symbol="600519")

    assert fake.sleeps == [60.0]


def test_rate_limiter_limits_max_concurrency():
    fake = FakeClock()
    limiter = RateLimiter(requests_per_second=60, requests_per_minute=60, max_concurrency=1, clock=fake.clock, sleep=fake.sleep)

    limiter.acquire(provider="fixture", symbol="600519")
    with pytest.raises(RateLimitError, match="concurrency"):
        limiter.acquire(provider="fixture", symbol="000001")
    limiter.release()
