from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .data_provider import MarketDataError, RateLimiter, RecoverableProviderError
from .db import (
    MarketDataProviderStatusRecord,
    MarketQuoteSnapshotRecord,
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    PaperShadowDecisionRecord,
    QuoteComparisonRecord,
    MarketDataQualityDailyRecord,
    MarketDataAdmissionHistoryRecord,
    MarketDataAdmissionResultRecord,
    MarketDataDegradationEventRecord,
    MarketDataShadowDailyReportRecord,
    ProviderConnectivityTestRecord,
    ProviderShadowRunRecord,
    RecordedQuoteFileRecord,
    SessionLocal,
    engine,
)
from .paper import Clock, SystemClock, TestClock
from .risk import decimal_to_str, stable_id, stable_json
from .repositories import SqlAlchemyRepositoryFactory
from .schema import assert_schema_ready_for_writes


logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")


class QuoteQualityStatus(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DUPLICATE = "DUPLICATE"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_TIME = "INVALID_TIME"
    INCOMPLETE = "INCOMPLETE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CALENDAR_MISMATCH = "CALENDAR_MISMATCH"
    PRICE_CONFLICT = "PRICE_CONFLICT"
    SUSPENSION_UNKNOWN = "SUSPENSION_UNKNOWN"
    LIMIT_RULE_UNKNOWN = "LIMIT_RULE_UNKNOWN"


class SuspensionStatus(StrEnum):
    TRADING = "TRADING"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


class ProviderHealthStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    MAINTENANCE = "MAINTENANCE"
    DISABLED = "DISABLED"


class ProviderErrorType(StrEnum):
    CONNECTION_ERROR = "CONNECTION_ERROR"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    READ_TIMEOUT = "READ_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TEMPORARY_SERVER_ERROR = "TEMPORARY_SERVER_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    INVALID_QUOTE = "INVALID_QUOTE"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    MARKET_CLOSED = "MARKET_CLOSED"
    PROVIDER_MAINTENANCE = "PROVIDER_MAINTENANCE"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


RETRYABLE_PROVIDER_ERRORS = {
    ProviderErrorType.CONNECTION_ERROR,
    ProviderErrorType.CONNECT_TIMEOUT,
    ProviderErrorType.READ_TIMEOUT,
    ProviderErrorType.RATE_LIMITED,
    ProviderErrorType.QUOTA_EXCEEDED,
    ProviderErrorType.TEMPORARY_SERVER_ERROR,
}


class AdmissionStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    OBSERVING = "OBSERVING"
    INELIGIBLE = "INELIGIBLE"
    ELIGIBLE_FOR_REVIEW = "ELIGIBLE_FOR_REVIEW"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class ShadowRunResult(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    ACCOUNT_MUTATION_DETECTED = "ACCOUNT_MUTATION_DETECTED"
    FILL_CREATED_DETECTED = "FILL_CREATED_DETECTED"


class SecretRedactor:
    SENSITIVE_KEYS = {
        "authorization",
        "api-key",
        "apikey",
        "api_key",
        "token",
        "access_token",
        "secret",
        "api_secret",
        "password",
        "cookie",
        "set-cookie",
    }

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = [secret for secret in (secrets or []) if secret]

    def redact_text(self, value: Any) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "***REDACTED***")
        return text

    def redact_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in mapping.items():
            lower = key.lower()
            if any(sensitive in lower for sensitive in self.SENSITIVE_KEYS):
                redacted[key] = "***REDACTED***"
            elif isinstance(value, dict):
                redacted[key] = self.redact_mapping(value)
            else:
                redacted[key] = self.redact_text(value)
        return redacted


FIELD_CONTRACT_VERSION = "live-paper-fields-v1"


class QuoteProviderError(RuntimeError):
    def __init__(self, message: str, *, error_type: ProviderErrorType = ProviderErrorType.UNKNOWN_PROVIDER_ERROR) -> None:
        super().__init__(message)
        self.error_type = error_type

    @property
    def retryable(self) -> bool:
        return self.error_type in RETRYABLE_PROVIDER_ERRORS


class QuoteSchemaError(QuoteProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_type=ProviderErrorType.SCHEMA_CHANGED)


class RealTimeMarketDataProvider(Protocol):
    provider_name: str
    provider_version: str

    def fetch_quotes(self, symbols: list[str], as_of: datetime) -> list[dict[str, Any]]:
        ...

    def health_check(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class RealTimeQuoteSnapshot:
    quote_id: str
    provider: str
    provider_version: str | None
    symbol: str
    exchange: str
    trading_date: date
    market_time: datetime
    received_at: datetime
    validated_at: datetime
    sequence: str | None
    open: Decimal
    high: Decimal
    low: Decimal
    last_price: Decimal
    previous_close: Decimal | None
    volume: int
    amount: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    suspension_status: str
    price_limit_up: Decimal | None
    price_limit_down: Decimal | None
    data_checksum: str
    calendar_version: str
    raw_schema_version: str
    quality_status: str
    quality_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid_for_matching(self) -> bool:
        return self.quality_status == QuoteQualityStatus.VALID.value and self.suspension_status == SuspensionStatus.TRADING.value


@dataclass(frozen=True)
class RealTimeQuoteConfig:
    max_age_seconds: float = 30.0
    clock_skew_seconds: float = 2.0
    fail_closed: bool = True
    raw_schema_version: str = "quote-v1"
    provider_priority: tuple[str, ...] = ("fixture", "recorded", "live_paper")
    provider_conflict_pct: Decimal = Decimal("0.03")
    provider_failure_threshold: int = 3
    provider_recovery_success_count: int = 2

    def __post_init__(self) -> None:
        if self.max_age_seconds <= 0:
            raise ValueError("quote max age must be positive")
        if self.clock_skew_seconds < 0:
            raise ValueError("quote clock skew must not be negative")
        if self.provider_conflict_pct < 0:
            raise ValueError("provider conflict pct must not be negative")
        if self.provider_failure_threshold <= 0 or self.provider_recovery_success_count <= 0:
            raise ValueError("provider health thresholds must be positive")


@dataclass(frozen=True)
class ProviderFieldContract:
    version: str = FIELD_CONTRACT_VERSION
    provider_symbol: str = "symbol"
    standard_symbol: str = "symbol"
    exchange: str = "exchange"
    market_time: str = "market_time"
    trading_date: str = "trading_date"
    open: str = "open"
    high: str = "high"
    low: str = "low"
    last_price: str = "last_price"
    previous_close: str = "previous_close"
    volume: str = "volume"
    amount: str = "amount"
    bid_price: str = "bid_price"
    ask_price: str = "ask_price"
    suspension_status: str = "suspension_status"
    price_limit_up: str = "price_limit_up"
    price_limit_down: str = "price_limit_down"
    sequence: str = "sequence"
    provider_error_code: str = "error_code"

    def map_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        mapped = {
            "symbol": payload.get(self.provider_symbol) or payload.get(self.standard_symbol),
            "exchange": payload.get(self.exchange),
            "trading_date": payload.get(self.trading_date),
            "market_time": payload.get(self.market_time),
            "open": payload.get(self.open),
            "high": payload.get(self.high),
            "low": payload.get(self.low),
            "last_price": payload.get(self.last_price),
            "previous_close": payload.get(self.previous_close),
            "volume": payload.get(self.volume),
            "amount": payload.get(self.amount),
            "bid_price": payload.get(self.bid_price),
            "ask_price": payload.get(self.ask_price),
            "suspension_status": payload.get(self.suspension_status),
            "price_limit_up": payload.get(self.price_limit_up),
            "price_limit_down": payload.get(self.price_limit_down),
            "sequence": payload.get(self.sequence),
        }
        if not mapped["symbol"] or not mapped["market_time"] or not mapped["last_price"]:
            raise QuoteSchemaError("provider payload missing required quote fields")
        return mapped


@dataclass(frozen=True)
class MarketDataAdmissionPolicy:
    minimum_complete_trading_days: int = 10
    minimum_provider_availability: Decimal = Decimal("0.99")
    minimum_symbol_coverage: Decimal = Decimal("0.99")
    maximum_p95_latency_seconds: Decimal = Decimal("5")
    maximum_missing_symbol_rate: Decimal = Decimal("0.005")
    maximum_invalid_quote_rate: Decimal = Decimal("0.001")
    maximum_out_of_order_rate: Decimal = Decimal("0.001")
    maximum_schema_error_count: int = 0
    maximum_unknown_suspension_rate: Decimal = Decimal("0.001")
    maximum_unknown_limit_rule_rate: Decimal = Decimal("0.001")
    minimum_replay_consistency: Decimal = Decimal("1.0")
    minimum_account_immutability: Decimal = Decimal("1.0")
    maximum_shadow_fill_count: int = 0
    maximum_unhandled_error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            key: decimal_to_str(value) if isinstance(value, Decimal) else value
            for key, value in self.__dict__.items()
        }

    @classmethod
    def from_settings(cls, settings) -> "MarketDataAdmissionPolicy":
        return cls(
            minimum_complete_trading_days=settings.market_admission_minimum_complete_trading_days,
            minimum_provider_availability=Decimal(str(settings.market_admission_minimum_provider_availability)),
            minimum_symbol_coverage=Decimal(str(settings.market_admission_minimum_symbol_coverage)),
            maximum_p95_latency_seconds=Decimal(str(settings.market_admission_maximum_p95_latency_seconds)),
            maximum_missing_symbol_rate=Decimal(str(settings.market_admission_maximum_missing_symbol_rate)),
            maximum_invalid_quote_rate=Decimal(str(settings.market_admission_maximum_invalid_quote_rate)),
            maximum_out_of_order_rate=Decimal(str(settings.market_admission_maximum_out_of_order_rate)),
            maximum_schema_error_count=settings.market_admission_maximum_schema_error_count,
            maximum_unknown_suspension_rate=Decimal(str(settings.market_admission_maximum_unknown_suspension_rate)),
            maximum_unknown_limit_rule_rate=Decimal(str(settings.market_admission_maximum_unknown_limit_rule_rate)),
            minimum_replay_consistency=Decimal(str(settings.market_admission_minimum_replay_consistency)),
            minimum_account_immutability=Decimal(str(settings.market_admission_minimum_account_immutability)),
            maximum_shadow_fill_count=settings.market_admission_maximum_shadow_fill_count,
            maximum_unhandled_error_count=settings.market_admission_maximum_unhandled_error_count,
        )


@dataclass(frozen=True)
class CompleteShadowTradingDayPolicy:
    morning_start: str = "09:30"
    morning_end: str = "11:30"
    afternoon_start: str = "13:00"
    afternoon_end: str = "15:00"
    minimum_symbol_coverage: Decimal = Decimal("0.99")

    def evaluate(self, *, trading_calendar: Any, trading_date: date, runs: list[ProviderShadowRunRecord], replay_consistency: Decimal = Decimal("1.0")) -> dict[str, Any]:
        failure_reasons: list[str] = []
        if not trading_calendar.is_trading_day(trading_date):
            failure_reasons.append("date is not a trading day")
        if not runs:
            failure_reasons.append("no shadow runs recorded")
        morning_complete = _session_complete(runs, trading_date, self.morning_start, self.morning_end)
        afternoon_complete = _session_complete(runs, trading_date, self.afternoon_start, self.afternoon_end)
        if not morning_complete:
            failure_reasons.append("morning session coverage incomplete")
        if not afternoon_complete:
            failure_reasons.append("afternoon session coverage incomplete")
        if replay_consistency < Decimal("1.0"):
            failure_reasons.append("recorded replay consistency below 100%")
        fills_created = sum(max(0, row.fills_after_count - row.fills_before_count) for row in runs)
        if fills_created:
            failure_reasons.append("PaperFill count increased")
        if any(row.account_state_before_checksum != row.account_state_after_checksum for row in runs):
            failure_reasons.append("paper account/order/position/ledger checksum changed")
        if any(row.schema_error_count > 0 for row in runs):
            failure_reasons.append("schema errors occurred")
        if any(row.network_error_count > 0 for row in runs):
            failure_reasons.append("network errors occurred")
        if any(row.result != ShadowRunResult.PASSED.value for row in runs):
            failure_reasons.append("one or more shadow runs failed")
        covered = max((row.valid_quote_count for row in runs), default=0)
        configured = max((row.configured_symbol_count for row in runs), default=0)
        coverage = Decimal("0") if configured == 0 else Decimal(covered) / Decimal(configured)
        if coverage < self.minimum_symbol_coverage:
            failure_reasons.append("symbol coverage below threshold")
        return {
            "day_status": "COMPLETE" if not failure_reasons else "INCOMPLETE",
            "morning_session_complete": morning_complete,
            "afternoon_session_complete": afternoon_complete,
            "failure_reasons": failure_reasons,
            "symbol_coverage": decimal_to_str(coverage),
            "covered_symbols": covered,
            "configured_symbols": configured,
            "replay_consistency": decimal_to_str(replay_consistency),
            "fills_created": fills_created,
            "account_immutability": "1.000000" if all(row.account_state_before_checksum == row.account_state_after_checksum for row in runs) else "0.000000",
        }


class FixtureQuoteProvider:
    provider_name = "fixture"
    provider_version = "fixture-v1"

    def __init__(self, quotes: list[dict[str, Any]] | None = None) -> None:
        self.quotes = quotes or []

    def fetch_quotes(self, symbols: list[str], as_of: datetime) -> list[dict[str, Any]]:
        return [quote for quote in self.quotes if quote.get("symbol") in set(symbols)] if self.quotes else [
            {
                "symbol": symbol,
                "exchange": _exchange(symbol),
                "trading_date": as_of.astimezone(TZ).date().isoformat(),
                "market_time": as_of.astimezone(TZ).isoformat(),
                "open": "10.00",
                "high": "10.10",
                "low": "9.90",
                "last_price": "10.00",
                "previous_close": "10.00",
                "volume": 10000,
                "suspension_status": SuspensionStatus.TRADING.value,
            }
            for symbol in symbols
        ]

    def health_check(self) -> dict[str, Any]:
        return {"status": ProviderHealthStatus.HEALTHY.value}

    def close(self) -> None:
        return None


class RecordedQuoteProvider(FixtureQuoteProvider):
    provider_name = "recorded"
    provider_version = "recorded-v1"


class LivePaperQuoteProvider:
    provider_name = "live_paper"
    provider_version = "live-paper-boundary-v1"

    def __init__(
        self,
        fetcher: Callable[[list[str], datetime], list[dict[str, Any]]] | None = None,
        *,
        api_base_url: str = "",
        api_key: str = "",
        api_secret: str = "",
        account_id: str = "",
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 5.0,
        max_symbols_per_request: int = 50,
        client: httpx.Client | None = None,
        field_contract: ProviderFieldContract | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._fetcher = fetcher
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_id = account_id
        self.max_symbols_per_request = max_symbols_per_request
        self.field_contract = field_contract or ProviderFieldContract()
        self.redactor = redactor or SecretRedactor([api_key, api_secret])
        timeout = httpx.Timeout(connect=connect_timeout_seconds, read=read_timeout_seconds, write=connect_timeout_seconds, pool=connect_timeout_seconds)
        self._client = client or httpx.Client(timeout=timeout)
        if max_symbols_per_request <= 0:
            raise ValueError("max_symbols_per_request must be positive")

    def fetch_quotes(self, symbols: list[str], as_of: datetime) -> list[dict[str, Any]]:
        request_id = stable_id("live-paper-request", str(uuid.uuid4()))
        logger.info(
            "live_paper_quote_request",
            extra={"request_id": request_id, "provider": self.provider_name, "symbol_count": len(symbols)},
        )
        if len(symbols) > self.max_symbols_per_request:
            raise QuoteProviderError("too many symbols in live quote request", error_type=ProviderErrorType.INVALID_RESPONSE)
        if self._fetcher is None:
            if not self.api_base_url:
                raise QuoteProviderError("LIVE_PAPER provider is disabled: missing API base URL", error_type=ProviderErrorType.PROVIDER_DISABLED)
            return self._fetch_http(symbols, as_of, request_id=request_id)
        try:
            return [self.field_contract.map_quote(item) for item in self._fetcher(symbols, as_of)]
        except QuoteProviderError:
            raise
        except TimeoutError as exc:
            raise QuoteProviderError("live quote fetch timed out", error_type=ProviderErrorType.READ_TIMEOUT) from exc
        except ConnectionError as exc:
            raise QuoteProviderError("live quote connection failed", error_type=ProviderErrorType.CONNECTION_ERROR) from exc

    def _fetch_http(self, symbols: list[str], as_of: datetime, *, request_id: str) -> list[dict[str, Any]]:
        headers = {"Accept": "application/json", "X-Request-Id": request_id}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.api_secret:
            headers["X-API-Secret"] = self.api_secret
        if self.account_id:
            headers["X-Provider-Account"] = self.account_id
        try:
            response = self._client.get(
                f"{self.api_base_url}/quotes",
                params={"symbols": ",".join(symbols), "as_of": as_of.astimezone(TZ).isoformat()},
                headers=headers,
            )
        except httpx.ConnectTimeout as exc:
            raise QuoteProviderError("live quote connect timeout", error_type=ProviderErrorType.CONNECT_TIMEOUT) from exc
        except httpx.ReadTimeout as exc:
            raise QuoteProviderError("live quote read timeout", error_type=ProviderErrorType.READ_TIMEOUT) from exc
        except httpx.ConnectError as exc:
            raise QuoteProviderError("live quote connection error", error_type=ProviderErrorType.CONNECTION_ERROR) from exc
        except httpx.HTTPError as exc:
            raise QuoteProviderError("live quote HTTP error", error_type=ProviderErrorType.UNKNOWN_PROVIDER_ERROR) from exc
        if response.status_code in {401, 403}:
            error_type = ProviderErrorType.AUTHENTICATION_ERROR if response.status_code == 401 else ProviderErrorType.PERMISSION_DENIED
            raise QuoteProviderError("live quote authentication or permission failed", error_type=error_type)
        if response.status_code == 429:
            error_type = ProviderErrorType.QUOTA_EXCEEDED if response.headers.get("X-Quota-Exceeded") == "true" else ProviderErrorType.RATE_LIMITED
            raise QuoteProviderError("live quote rate limited", error_type=error_type)
        if response.status_code == 404:
            raise QuoteProviderError("live quote symbol not found", error_type=ProviderErrorType.SYMBOL_NOT_FOUND)
        if response.status_code == 503:
            raise QuoteProviderError("live quote provider maintenance", error_type=ProviderErrorType.PROVIDER_MAINTENANCE)
        if 500 <= response.status_code < 600:
            raise QuoteProviderError("live quote temporary server error", error_type=ProviderErrorType.TEMPORARY_SERVER_ERROR)
        if response.status_code >= 400:
            raise QuoteProviderError("live quote provider rejected request", error_type=ProviderErrorType.INVALID_RESPONSE)
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuoteProviderError("live quote response is not JSON", error_type=ProviderErrorType.INVALID_RESPONSE) from exc
        quotes = payload.get("quotes") if isinstance(payload, dict) else payload
        if not isinstance(quotes, list):
            raise QuoteSchemaError("live quote response missing quotes list")
        return [self.field_contract.map_quote(item) for item in quotes]

    def health_check(self) -> dict[str, Any]:
        if self._fetcher is None and not self.api_base_url:
            return {"status": ProviderHealthStatus.NOT_CONFIGURED.value, "configured": False}
        return {"status": ProviderHealthStatus.STARTING.value, "configured": True}

    def close(self) -> None:
        self._client.close()


class RetryingQuoteProvider:
    def __init__(
        self,
        provider: RealTimeMarketDataProvider,
        *,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 8.0,
        jitter_seconds: float = 0.1,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.provider = provider
        self.provider_name = provider.provider_name
        self.provider_version = provider.provider_version
        self.max_attempts = max_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.jitter_seconds = jitter_seconds
        self.sleep = sleep or time_module.sleep
        self.random_fn = random_fn or random.random
        self.rate_limiter = rate_limiter
        if self.max_attempts <= 0:
            raise ValueError("quote max_attempts must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("quote max backoff must be >= initial backoff")

    def fetch_quotes(self, symbols: list[str], as_of: datetime) -> list[dict[str, Any]]:
        attempt = 1
        while True:
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire(provider=self.provider_name, symbol=",".join(symbols))
                try:
                    return self.provider.fetch_quotes(symbols, as_of)
                finally:
                    if self.rate_limiter is not None:
                        self.rate_limiter.release()
            except QuoteProviderError as exc:
                logger.warning(
                    "quote_provider_failure",
                    extra={
                        "attempt": attempt,
                        "provider": self.provider_name,
                        "error_type": exc.error_type.value,
                        "retryable": exc.retryable,
                    },
                )
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                self.sleep(_backoff(attempt, self.initial_backoff_seconds, self.max_backoff_seconds, self.jitter_seconds, self.random_fn))
                attempt += 1
            except (RecoverableProviderError, TimeoutError, ConnectionError) as exc:
                logger.warning(
                    "quote_provider_retryable_failure",
                    extra={"attempt": attempt, "provider": self.provider_name, "exception_class": exc.__class__.__name__},
                )
                if attempt >= self.max_attempts:
                    raise QuoteProviderError(
                        f"quote provider failed after {attempt} attempts",
                        error_type=ProviderErrorType.CONNECTION_ERROR,
                    ) from exc
                self.sleep(_backoff(attempt, self.initial_backoff_seconds, self.max_backoff_seconds, self.jitter_seconds, self.random_fn))
                attempt += 1
            except (QuoteSchemaError, MarketDataError):
                raise

    def health_check(self) -> dict[str, Any]:
        return self.provider.health_check()

    def close(self) -> None:
        return self.provider.close()


def normalize_quote(
    raw: dict[str, Any],
    *,
    provider: str,
    provider_version: str | None,
    received_at: datetime,
    validated_at: datetime,
    calendar_version: str,
    now: datetime,
    config: RealTimeQuoteConfig | None = None,
    trading_calendar: Any | None = None,
) -> RealTimeQuoteSnapshot:
    config = config or RealTimeQuoteConfig()
    received = _aware(received_at)
    validated = _aware(validated_at)
    current = _aware(now)
    required = ["symbol", "trading_date", "market_time", "open", "high", "low", "last_price", "volume"]
    missing = [field for field in required if field not in raw or raw[field] in (None, "")]
    if missing:
        return _invalid(
            raw,
            provider,
            provider_version,
            received,
            validated,
            calendar_version,
            config.raw_schema_version,
            QuoteQualityStatus.INCOMPLETE,
            [f"missing required fields: {','.join(missing)}"],
        )
    try:
        symbol = _normalize_symbol(str(raw["symbol"]).strip())
        trading_date = date.fromisoformat(str(raw["trading_date"]))
        market_time = _aware(datetime.fromisoformat(str(raw["market_time"])))
        open_price = _dec(raw["open"])
        high = _dec(raw["high"])
        low = _dec(raw["low"])
        last = _dec(raw["last_price"])
        previous_close = None if raw.get("previous_close") in (None, "") else _dec(raw.get("previous_close"))
        volume = int(raw["volume"])
        amount = None if raw.get("amount") in (None, "") else _dec(raw.get("amount"))
        bid = None if raw.get("bid_price") in (None, "") else _dec(raw.get("bid_price"))
        ask = None if raw.get("ask_price") in (None, "") else _dec(raw.get("ask_price"))
        limit_up = None if raw.get("price_limit_up") in (None, "") else _dec(raw.get("price_limit_up"))
        limit_down = None if raw.get("price_limit_down") in (None, "") else _dec(raw.get("price_limit_down"))
    except Exception as exc:
        raise QuoteSchemaError(f"quote schema error: {exc}") from exc
    exchange = str(raw.get("exchange") or _exchange(symbol))
    suspension = str(raw.get("suspension_status") or SuspensionStatus.UNKNOWN.value).upper()
    sequence = None if raw.get("sequence") in (None, "") else str(raw.get("sequence"))
    quality = QuoteQualityStatus.VALID
    reasons: list[str] = []
    if min(open_price, high, low, last) <= 0 or volume < 0 or (amount is not None and amount < 0):
        quality = QuoteQualityStatus.INVALID_PRICE
        reasons.append("price, volume, or amount is invalid")
    elif high < max(open_price, low, last) or low > min(open_price, high, last):
        quality = QuoteQualityStatus.INVALID_PRICE
        reasons.append("OHLC relationship is invalid")
    elif market_time > received + timedelta(seconds=config.clock_skew_seconds) or market_time > current + timedelta(seconds=config.clock_skew_seconds):
        quality = QuoteQualityStatus.INVALID_TIME
        reasons.append("market_time is later than received_at or current clock")
    elif current - market_time > timedelta(seconds=config.max_age_seconds):
        quality = QuoteQualityStatus.STALE
        reasons.append("quote is stale")
    elif trading_calendar is not None:
        try:
            calendar_ok = trading_calendar.is_trading_day(trading_date) and trading_date == market_time.date()
        except Exception:
            calendar_ok = False
        if not calendar_ok:
            quality = QuoteQualityStatus.CALENDAR_MISMATCH
            reasons.append("trading_date does not match trading calendar")
    if quality == QuoteQualityStatus.VALID and previous_close is None:
        quality = QuoteQualityStatus.INCOMPLETE
        reasons.append("previous_close is missing")
    elif quality == QuoteQualityStatus.VALID and suspension == SuspensionStatus.UNKNOWN.value:
        quality = QuoteQualityStatus.SUSPENSION_UNKNOWN
        reasons.append("suspension status is unknown")
    elif quality == QuoteQualityStatus.VALID and (limit_up is None or limit_down is None):
        quality = QuoteQualityStatus.LIMIT_RULE_UNKNOWN
        reasons.append("price limit fields are missing")
    payload = _checksum_payload(
        symbol=symbol,
        exchange=exchange,
        trading_date=trading_date,
        market_time=market_time,
        sequence=sequence,
        open_price=open_price,
        high=high,
        low=low,
        last=last,
        previous_close=previous_close,
        volume=volume,
        amount=amount,
        bid=bid,
        ask=ask,
        suspension=suspension,
        limit_up=limit_up,
        limit_down=limit_down,
        calendar_version=calendar_version,
        raw_schema_version=config.raw_schema_version,
    )
    checksum = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    quote_id = stable_id("quote", provider, symbol, market_time.isoformat(), checksum)
    return RealTimeQuoteSnapshot(
        quote_id=quote_id,
        provider=provider,
        provider_version=provider_version,
        symbol=symbol,
        exchange=exchange,
        trading_date=trading_date,
        market_time=market_time,
        received_at=received,
        validated_at=validated,
        sequence=sequence,
        open=open_price,
        high=high,
        low=low,
        last_price=last,
        previous_close=previous_close,
        volume=volume,
        amount=amount,
        bid_price=bid,
        ask_price=ask,
        suspension_status=suspension,
        price_limit_up=limit_up,
        price_limit_down=limit_down,
        data_checksum=checksum,
        calendar_version=calendar_version,
        raw_schema_version=config.raw_schema_version,
        quality_status=quality.value,
        quality_reasons=tuple(reasons),
    )


def save_quote_snapshot(session: Session, snapshot: RealTimeQuoteSnapshot) -> MarketQuoteSnapshotRecord:
    existing = session.scalars(select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.quote_id == snapshot.quote_id)).first()
    if existing is not None:
        return existing
    row = MarketQuoteSnapshotRecord(
        quote_id=snapshot.quote_id,
        provider=snapshot.provider,
        provider_version=snapshot.provider_version,
        symbol=snapshot.symbol,
        exchange=snapshot.exchange,
        trading_date=snapshot.trading_date.isoformat(),
        market_time=snapshot.market_time,
        received_at=snapshot.received_at,
        validated_at=snapshot.validated_at,
        sequence=snapshot.sequence,
        open_price=decimal_to_str(snapshot.open),
        high_price=decimal_to_str(snapshot.high),
        low_price=decimal_to_str(snapshot.low),
        last_price=decimal_to_str(snapshot.last_price),
        previous_close=None if snapshot.previous_close is None else decimal_to_str(snapshot.previous_close),
        volume=snapshot.volume,
        amount=None if snapshot.amount is None else decimal_to_str(snapshot.amount),
        bid_price=None if snapshot.bid_price is None else decimal_to_str(snapshot.bid_price),
        ask_price=None if snapshot.ask_price is None else decimal_to_str(snapshot.ask_price),
        suspension_status=snapshot.suspension_status,
        price_limit_up=None if snapshot.price_limit_up is None else decimal_to_str(snapshot.price_limit_up),
        price_limit_down=None if snapshot.price_limit_down is None else decimal_to_str(snapshot.price_limit_down),
        data_checksum=snapshot.data_checksum,
        calendar_version=snapshot.calendar_version,
        raw_schema_version=snapshot.raw_schema_version,
        quality_status=snapshot.quality_status,
        quality_reasons_json=stable_json(list(snapshot.quality_reasons)),
        payload_json=stable_json(quote_payload(snapshot)),
        created_at=snapshot.validated_at,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        with session.begin():
            found = session.scalars(select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.quote_id == snapshot.quote_id)).first()
            if found is not None:
                return found
        raise
    return row


class QuoteSelectionService:
    def __init__(
        self,
        *,
        session: Session,
        clock: Clock,
        config: RealTimeQuoteConfig,
        expected_calendar_version: str,
    ) -> None:
        self.session = session
        self.clock = clock
        self.config = config
        self.expected_calendar_version = expected_calendar_version

    def select_for_matching(self, symbol: str, trading_date: date) -> MarketQuoteSnapshotRecord:
        now = self.clock.now().astimezone(TZ)
        normalized_symbol = _normalize_symbol(symbol)
        rows = [
            row
            for row in SqlAlchemyRepositoryFactory.from_session(self.session)
            .market_quotes()
            .latest_valid_quotes(self.session, symbol=normalized_symbol, trading_date=trading_date.isoformat(), now=now, limit=20)
            if row.calendar_version == self.expected_calendar_version
        ]
        fresh = [row for row in rows if now - _db_aware(row.market_time) <= timedelta(seconds=self.config.max_age_seconds)]
        if not fresh:
            raise MarketDataError(f"missing valid realtime quote for {symbol} {trading_date}")
        latest_time = max(_db_aware(row.market_time) for row in fresh)
        candidates = [row for row in fresh if _db_aware(row.market_time) == latest_time]
        self._assert_no_provider_conflict(candidates)
        priority = {provider: idx for idx, provider in enumerate(self.config.provider_priority)}
        return sorted(candidates, key=lambda row: (priority.get(row.provider, 999), row.provider, row.quote_id))[0]

    def _assert_no_provider_conflict(self, candidates: list[MarketQuoteSnapshotRecord]) -> None:
        if len({row.provider for row in candidates}) <= 1:
            return
        prices = [Decimal(row.last_price) for row in candidates]
        low = min(prices)
        high = max(prices)
        if low > 0 and (high - low) / low > self.config.provider_conflict_pct:
            raise MarketDataError("provider quote conflict exceeds threshold")


class MarketDataGateway:
    def __init__(
        self,
        *,
        session_factory,
        provider: RealTimeMarketDataProvider,
        calendar_version: str,
        clock: Clock | None = None,
        config: RealTimeQuoteConfig | None = None,
        instance_id: str = "market-data-runtime-local",
        recorder: QuoteRecorder | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.calendar_version = calendar_version
        self.clock = clock or SystemClock()
        self.config = config or RealTimeQuoteConfig()
        self.instance_id = instance_id
        self.recorder = recorder

    def run_once(self, symbols: list[str]) -> dict[str, Any]:
        as_of = self.clock.now().astimezone(TZ)
        run_id = stable_id("provider-shadow-run", self.provider.provider_name, as_of.isoformat(), stable_json(sorted(symbols)))
        with self.session_factory() as session:
            before_checksum = _paper_state_checksum(session)
            fills_before = session.query(PaperFillRecord).count()
        request_started = time_module.monotonic()
        try:
            raw_quotes = self.provider.fetch_quotes(symbols, as_of)
        except Exception as exc:
            with self.session_factory() as session:
                _provider_failure(session, self.provider.provider_name, self.instance_id, as_of, exc, self.config)
                after_checksum = _paper_state_checksum(session)
                fills_after = session.query(PaperFillRecord).count()
                _save_provider_shadow_run(
                    session,
                    run_id=run_id,
                    provider=self.provider.provider_name,
                    provider_version=self.provider.provider_version,
                    started_at=as_of,
                    ended_at=self.clock.now().astimezone(TZ),
                    symbols=symbols,
                    result=ShadowRunResult.PROVIDER_NOT_CONFIGURED.value
                    if isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.PROVIDER_DISABLED
                    else ShadowRunResult.FAILED.value,
                    failure_reasons=[_safe_error_type(exc)],
                    before_checksum=before_checksum,
                    after_checksum=after_checksum,
                    fills_before=fills_before,
                    fills_after=fills_after,
                    metrics={"network_error_count": 1, "rate_limit_count": 1 if _safe_error_type(exc) in {ProviderErrorType.RATE_LIMITED.value, ProviderErrorType.QUOTA_EXCEEDED.value} else 0},
                )
                session.commit()
            raise
        latency_ms = (time_module.monotonic() - request_started) * 1000
        saved = 0
        invalid = 0
        duplicate = 0
        out_of_order = 0
        snapshots: list[RealTimeQuoteSnapshot] = []
        for raw in raw_quotes:
            try:
                snapshot = normalize_quote(
                    raw,
                    provider=self.provider.provider_name,
                    provider_version=self.provider.provider_version,
                    received_at=as_of,
                    validated_at=self.clock.now().astimezone(TZ),
                    calendar_version=self.calendar_version,
                    now=self.clock.now().astimezone(TZ),
                    config=self.config,
                )
            except QuoteProviderError:
                raise
            except Exception as exc:
                logger.exception("quote_normalization_failed", extra={"provider": self.provider.provider_name})
                snapshot = _invalid(
                    raw if isinstance(raw, dict) else {"raw": str(raw)},
                    self.provider.provider_name,
                    self.provider.provider_version,
                    as_of,
                    self.clock.now().astimezone(TZ),
                    self.calendar_version,
                    self.config.raw_schema_version,
                    QuoteQualityStatus.PROVIDER_ERROR,
                    [f"normalization failed: {type(exc).__name__}"],
                )
            snapshots.append(snapshot)
        with self.session_factory() as session:
            for snapshot in snapshots:
                if _is_duplicate_quote(session, snapshot):
                    duplicate += 1
                if _is_out_of_order_quote(session, snapshot):
                    out_of_order += 1
                    snapshot = _with_quality(snapshot, QuoteQualityStatus.OUT_OF_ORDER, "market_time is older than latest saved quote")
                save_quote_snapshot(session, snapshot)
                if self.recorder is not None:
                    self.recorder.record(session, request_id=stable_id("quote-request", self.provider.provider_name, as_of.isoformat()), snapshot=snapshot)
                saved += 1
                invalid += 0 if snapshot.quality_status == QuoteQualityStatus.VALID.value else 1
            _provider_success(session, self.provider.provider_name, self.instance_id, as_of, snapshots, latency_ms, invalid, duplicate, out_of_order, self.config)
            after_checksum = _paper_state_checksum(session)
            fills_after = session.query(PaperFillRecord).count()
            result = ShadowRunResult.PASSED.value
            failure_reasons: list[str] = []
            if after_checksum != before_checksum:
                result = ShadowRunResult.ACCOUNT_MUTATION_DETECTED.value
                failure_reasons.append("account/order/position/ledger checksum changed")
            if fills_after > fills_before:
                result = ShadowRunResult.FILL_CREATED_DETECTED.value
                failure_reasons.append("PaperFill count increased")
            _save_provider_shadow_run(
                session,
                run_id=run_id,
                provider=self.provider.provider_name,
                provider_version=self.provider.provider_version,
                started_at=as_of,
                ended_at=self.clock.now().astimezone(TZ),
                symbols=symbols,
                result=result,
                failure_reasons=failure_reasons,
                before_checksum=before_checksum,
                after_checksum=after_checksum,
                fills_before=fills_before,
                fills_after=fills_after,
                metrics={
                    "quote_received_count": len(snapshots),
                    "valid_quote_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.VALID.value]),
                    "invalid_quote_count": invalid,
                    "stale_quote_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.STALE.value]),
                    "duplicate_quote_count": duplicate,
                    "out_of_order_count": out_of_order,
                    "schema_error_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.PROVIDER_ERROR.value]),
                    "availability": decimal_to_str(Decimal(len([item for item in snapshots if item.quality_status == QuoteQualityStatus.VALID.value])) / Decimal(len(snapshots) or 1)),
                    "average_latency_ms": latency_ms,
                    "p50_latency_ms": latency_ms,
                    "p95_latency_ms": latency_ms,
                    "p99_latency_ms": latency_ms,
                    "missing_symbol_rate": decimal_to_str(Decimal(max(0, len(set(symbols)) - len({item.symbol for item in snapshots}))) / Decimal(len(set(symbols)) or 1)),
                },
            )
            session.commit()
        return {"saved": saved, "invalid": invalid, "duplicate": duplicate, "out_of_order": out_of_order, "provider": self.provider.provider_name}


class QuoteRecorder:
    schema_version = "recorded-quote-v1"

    def __init__(self, directory: Path, *, record_raw: bool = False) -> None:
        self.directory = directory
        self.record_raw = record_raw

    def record(self, session: Session, *, request_id: str, snapshot: RealTimeQuoteSnapshot, raw: dict[str, Any] | None = None) -> Path:
        day_dir = self.directory / snapshot.trading_date.isoformat() / snapshot.symbol
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{snapshot.market_time.strftime('%H%M%S')}-{snapshot.data_checksum[:12]}.json"
        if path.exists():
            raise FileExistsError(f"recorded quote already exists: {path.name}")
        payload = {
            "schema_version": self.schema_version,
            "provider": snapshot.provider,
            "provider_version": snapshot.provider_version,
            "request_id": request_id,
            "received_at": snapshot.received_at.astimezone(TZ).isoformat(),
            "symbol": snapshot.symbol,
            "market_time": snapshot.market_time.astimezone(TZ).isoformat(),
            "normalized_quote": quote_payload(snapshot),
            "data_checksum": snapshot.data_checksum,
            "quality_status": snapshot.quality_status,
        }
        if self.record_raw and raw is not None:
            payload["raw_response"] = _redact_raw(raw)
        path.write_text(stable_json(payload), encoding="utf-8")
        recording_id = stable_id("recorded-quote", snapshot.provider, request_id, snapshot.symbol, snapshot.data_checksum)
        session.add(
            RecordedQuoteFileRecord(
                recording_id=recording_id,
                provider=snapshot.provider,
                provider_version=snapshot.provider_version,
                request_id=request_id,
                trading_date=snapshot.trading_date.isoformat(),
                symbol=snapshot.symbol,
                market_time=snapshot.market_time,
                received_at=snapshot.received_at,
                data_checksum=snapshot.data_checksum,
                quality_status=snapshot.quality_status,
                schema_version=self.schema_version,
                file_path=str(path),
                created_at=snapshot.validated_at,
            )
        )
        return path


class RecordedQuoteFileProvider:
    provider_name = "recorded"
    provider_version = "recorded-file-v1"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def fetch_quotes(self, symbols: list[str], as_of: datetime) -> list[dict[str, Any]]:
        wanted = {_normalize_symbol(symbol) for symbol in symbols}
        rows: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*/*/*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            quote = payload.get("normalized_quote")
            if not isinstance(quote, dict):
                raise QuoteProviderError("recorded quote file is corrupt", error_type=ProviderErrorType.INVALID_RESPONSE)
            if quote.get("symbol") in wanted:
                rows.append(_raw_from_recorded_payload(quote))
        rows.sort(key=lambda item: (item["market_time"], item["symbol"]))
        return rows

    def health_check(self) -> dict[str, Any]:
        return {"status": ProviderHealthStatus.HEALTHY.value if self.directory.exists() else ProviderHealthStatus.DISABLED.value}

    def close(self) -> None:
        return None


class QuoteComparisonService:
    def __init__(self, *, conflict_bps: int) -> None:
        self.conflict_bps = conflict_bps

    def compare(self, live: RealTimeQuoteSnapshot, reference: RealTimeQuoteSnapshot, *, created_at: datetime) -> dict[str, Any]:
        if live.symbol != reference.symbol:
            raise ValueError("quote comparison requires the same symbol")
        diff_bps = Decimal("0") if reference.last_price == 0 else ((live.last_price - reference.last_price).copy_abs() / reference.last_price * Decimal("10000")).quantize(Decimal("0.0001"))
        quality = QuoteQualityStatus.PRICE_CONFLICT.value if diff_bps > Decimal(self.conflict_bps) else QuoteQualityStatus.VALID.value
        return {
            "comparison_id": stable_id("quote-comparison", live.quote_id, reference.quote_id),
            "trading_date": live.trading_date.isoformat(),
            "symbol": live.symbol,
            "live_provider": live.provider,
            "reference_provider": reference.provider,
            "live_quote_id": live.quote_id,
            "reference_quote_id": reference.quote_id,
            "price_diff_bps": decimal_to_str(diff_bps),
            "latency_ms": max(0.0, (live.received_at - reference.received_at).total_seconds() * 1000),
            "quality_status": quality,
            "created_at": created_at,
        }

    def save(self, session: Session, result: dict[str, Any]) -> QuoteComparisonRecord:
        existing = session.scalars(select(QuoteComparisonRecord).where(QuoteComparisonRecord.comparison_id == result["comparison_id"])).first()
        if existing is not None:
            return existing
        row = QuoteComparisonRecord(
            comparison_id=result["comparison_id"],
            trading_date=result["trading_date"],
            symbol=result["symbol"],
            live_provider=result["live_provider"],
            reference_provider=result["reference_provider"],
            live_quote_id=result["live_quote_id"],
            reference_quote_id=result["reference_quote_id"],
            price_diff_bps=result["price_diff_bps"],
            latency_ms=result["latency_ms"],
            quality_status=result["quality_status"],
            payload_json=stable_json({**result, "created_at": result["created_at"].astimezone(TZ).isoformat()}),
            created_at=result["created_at"],
        )
        session.add(row)
        session.flush()
        return row


def compute_quality_metrics(snapshots: list[RealTimeQuoteSnapshot], *, expected_symbols: set[str] | None = None) -> dict[str, Any]:
    if not snapshots:
        return {
            "quote_received_count": 0,
            "valid_quote_count": 0,
            "provider_availability": None,
            "reason": "NO_DATA",
        }
    total = len(snapshots)
    valid = len([item for item in snapshots if item.quality_status == QuoteQualityStatus.VALID.value])
    seen = {item.symbol for item in snapshots}
    expected = expected_symbols or seen
    missing = len(expected - seen)
    return {
        "quote_received_count": total,
        "valid_quote_count": valid,
        "stale_quote_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.STALE.value]),
        "invalid_quote_count": total - valid,
        "duplicate_rate": "0.000000",
        "out_of_order_rate": decimal_to_str(Decimal(len([item for item in snapshots if item.quality_status == QuoteQualityStatus.OUT_OF_ORDER.value])) / Decimal(total)),
        "missing_symbol_rate": decimal_to_str(Decimal(missing) / Decimal(len(expected) or 1)),
        "provider_availability": decimal_to_str(Decimal(valid) / Decimal(total)),
        "schema_error_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.PROVIDER_ERROR.value]),
        "price_conflict_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.PRICE_CONFLICT.value]),
        "suspension_unknown_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.SUSPENSION_UNKNOWN.value]),
        "limit_rule_unknown_count": len([item for item in snapshots if item.quality_status == QuoteQualityStatus.LIMIT_RULE_UNKNOWN.value]),
    }


class MarketDataAdmissionService:
    def __init__(self, policy: MarketDataAdmissionPolicy | None = None) -> None:
        self.policy = policy or MarketDataAdmissionPolicy()

    def evaluate(self, session: Session, *, provider: str, now: datetime) -> MarketDataAdmissionResultRecord:
        provider_status = session.scalars(
            select(MarketDataProviderStatusRecord)
            .where(MarketDataProviderStatusRecord.provider == provider)
            .order_by(MarketDataProviderStatusRecord.updated_at.desc())
        ).first()
        configured = provider == "live_paper" and provider_status is not None and provider_status.status not in {
            ProviderHealthStatus.NOT_CONFIGURED.value,
            ProviderHealthStatus.DISABLED.value,
        }
        progress = admission_status_summary(session, provider=provider, policy=self.policy, provider_configured=configured)
        if progress["uses_daily_reports"]:
            result = MarketDataAdmissionResultRecord(
                provider=provider,
                evaluated_at=now,
                status=progress["admission_status"],
                complete_trading_days=progress["completed_qualified_days"],
                failure_reasons_json=stable_json(progress["current_blockers"]),
                metrics_json=stable_json(progress),
                policy_snapshot_json=stable_json(self.policy.to_dict()),
            )
            session.add(result)
            session.add(
                MarketDataAdmissionHistoryRecord(
                    provider=provider,
                    from_status="",
                    to_status=progress["admission_status"],
                    reason=";".join(progress["current_blockers"]),
                    changed_at=now,
                )
            )
            session.flush()
            return result

        rows = session.scalars(
            select(ProviderShadowRunRecord)
            .where(ProviderShadowRunRecord.provider == provider, ProviderShadowRunRecord.result == ShadowRunResult.PASSED.value)
            .order_by(ProviderShadowRunRecord.trading_date.desc())
        ).all()
        complete_days = len({row.trading_date for row in rows})
        failure_reasons: list[str] = []
        if not provider or provider == "live_paper_not_configured" or (provider_status is None and not rows):
            status = AdmissionStatus.NOT_CONFIGURED.value
            failure_reasons.append("provider is not configured")
        elif provider_status is not None and provider_status.status in {ProviderHealthStatus.NOT_CONFIGURED.value, ProviderHealthStatus.DISABLED.value} and not rows:
            status = AdmissionStatus.NOT_CONFIGURED.value
            failure_reasons.append(f"provider status is {provider_status.status}")
        elif complete_days < self.policy.minimum_complete_trading_days:
            status = AdmissionStatus.OBSERVING.value
            failure_reasons.append("insufficient complete trading days")
        else:
            status = AdmissionStatus.ELIGIBLE_FOR_REVIEW.value
            checked = rows[: self.policy.minimum_complete_trading_days]
            availability = min((Decimal(row.availability or "0") for row in checked), default=Decimal("0"))
            missing = max((Decimal(row.missing_symbol_rate or "1") for row in checked), default=Decimal("1"))
            p95 = max((Decimal(str(row.p95_latency_ms or 0)) / Decimal("1000") for row in checked), default=Decimal("0"))
            invalid_rate = max(
                (
                    Decimal(row.invalid_quote_count) / Decimal(max(1, row.quote_received_count))
                    for row in checked
                ),
                default=Decimal("1"),
            )
            if availability < self.policy.minimum_provider_availability:
                failure_reasons.append("provider availability below threshold")
            if missing > self.policy.maximum_missing_symbol_rate:
                failure_reasons.append("missing symbol rate above threshold")
            if p95 > self.policy.maximum_p95_latency_seconds:
                failure_reasons.append("p95 latency above threshold")
            if invalid_rate > self.policy.maximum_invalid_quote_rate:
                failure_reasons.append("invalid quote rate above threshold")
            if any(row.schema_error_count > self.policy.maximum_schema_error_count for row in checked):
                failure_reasons.append("schema error count above threshold")
            if any(row.fills_after_count - row.fills_before_count > self.policy.maximum_shadow_fill_count for row in checked):
                failure_reasons.append("shadow fill count above threshold")
            if any(row.account_state_before_checksum != row.account_state_after_checksum for row in checked):
                failure_reasons.append("account immutability check failed")
            if failure_reasons:
                status = AdmissionStatus.INELIGIBLE.value
        result = MarketDataAdmissionResultRecord(
            provider=provider,
            evaluated_at=now,
            status=status,
            complete_trading_days=complete_days,
            failure_reasons_json=stable_json(failure_reasons),
            metrics_json=stable_json({"complete_trading_days": complete_days}),
            policy_snapshot_json=stable_json(self.policy.to_dict()),
        )
        session.add(result)
        session.add(
            MarketDataAdmissionHistoryRecord(
                provider=provider,
                from_status="",
                to_status=status,
                reason=";".join(failure_reasons),
                changed_at=now,
            )
        )
        session.flush()
        return result


def admission_status_summary(
    session: Session,
    *,
    provider: str,
    policy: MarketDataAdmissionPolicy,
    provider_configured: bool,
) -> dict[str, Any]:
    session.flush()
    reports = session.scalars(
        select(MarketDataShadowDailyReportRecord)
        .where(MarketDataShadowDailyReportRecord.provider == provider)
        .order_by(MarketDataShadowDailyReportRecord.trading_date.asc())
    ).all()
    if not reports:
        return {
            "provider_configured": provider_configured,
            "connectivity_status": "NOT_CONFIGURED" if not provider_configured else "UNKNOWN",
            "admission_status": AdmissionStatus.NOT_CONFIGURED.value if not provider_configured else AdmissionStatus.OBSERVING.value,
            "required_complete_days": policy.minimum_complete_trading_days,
            "completed_qualified_days": 0,
            "incomplete_days": 0,
            "failed_days": 0,
            "first_observation_date": "",
            "latest_observation_date": "",
            "failed_metrics": [],
            "current_blockers": ["provider is not configured"] if not provider_configured else ["no complete shadow trading days recorded"],
            "uses_daily_reports": False,
        }
    payloads = [_safe_json(row.report_json) for row in reports]
    complete = [item for row, item in zip(reports, payloads) if row.provider == "live_paper" and row.status == "COMPLETE" and item.get("day_status") == "COMPLETE"]
    incomplete = [row for row in reports if row.status == "INCOMPLETE"]
    failed = [row for row in reports if row.status == "FAILED"]
    blockers: list[str] = []
    failed_metrics: list[str] = []
    status = AdmissionStatus.OBSERVING.value
    if not provider_configured:
        status = AdmissionStatus.NOT_CONFIGURED.value
        blockers.append("provider is not configured")
    elif len(complete) < policy.minimum_complete_trading_days:
        blockers.append("insufficient complete shadow trading days")
    else:
        checked = complete[-policy.minimum_complete_trading_days :]
        status = AdmissionStatus.ELIGIBLE_FOR_REVIEW.value
        for item in checked:
            if Decimal(str(item.get("provider_availability") or "0")) < policy.minimum_provider_availability:
                failed_metrics.append("provider_availability")
            if Decimal(str(item.get("symbol_coverage") or "0")) < policy.minimum_symbol_coverage:
                failed_metrics.append("symbol_coverage")
            if Decimal(str(item.get("p95_latency_ms") or "0")) / Decimal("1000") > policy.maximum_p95_latency_seconds:
                failed_metrics.append("p95_latency")
            if Decimal(str(item.get("missing_symbol_rate") or "0")) > policy.maximum_missing_symbol_rate:
                failed_metrics.append("missing_symbol_rate")
            if Decimal(str(item.get("invalid_quote_rate") or "0")) > policy.maximum_invalid_quote_rate:
                failed_metrics.append("invalid_quote_rate")
            if int(item.get("schema_error_count") or 0) > policy.maximum_schema_error_count:
                failed_metrics.append("schema_error_count")
            if int(item.get("fills_created") or 0) > policy.maximum_shadow_fill_count:
                failed_metrics.append("fills_created")
            if Decimal(str(item.get("account_immutability") or "0")) < policy.minimum_account_immutability:
                failed_metrics.append("account_immutability")
        failed_metrics = sorted(set(failed_metrics))
        if failed_metrics:
            status = AdmissionStatus.INELIGIBLE.value
            blockers.extend([f"{metric} failed threshold" for metric in failed_metrics])
    if provider != "live_paper":
        blockers.append("fixture or recorded providers do not count as real provider observation")
    return {
        "provider_configured": provider_configured,
        "connectivity_status": _latest_connectivity_status(session, provider),
        "admission_status": status,
        "required_complete_days": policy.minimum_complete_trading_days,
        "completed_qualified_days": len(complete),
        "incomplete_days": len(incomplete),
        "failed_days": len(failed),
        "first_observation_date": reports[0].trading_date if reports else "",
        "latest_observation_date": reports[-1].trading_date if reports else "",
        "failed_metrics": failed_metrics,
        "current_blockers": blockers,
        "uses_daily_reports": True,
    }


def _latest_connectivity_status(session: Session, provider: str) -> str:
    row = session.scalars(
        select(ProviderConnectivityTestRecord)
        .where(ProviderConnectivityTestRecord.provider == provider)
        .order_by(ProviderConnectivityTestRecord.started_at.desc())
    ).first()
    return "UNKNOWN" if row is None else row.status


def _safe_json(value: str | None, default: Any | None = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {} if default is None else default


def _session_complete(runs: list[ProviderShadowRunRecord], trading_date: date, start_text: str, end_text: str) -> bool:
    start_hour, start_minute = [int(part) for part in start_text.split(":")[:2]]
    end_hour, end_minute = [int(part) for part in end_text.split(":")[:2]]
    start_dt = datetime(trading_date.year, trading_date.month, trading_date.day, start_hour, start_minute, tzinfo=TZ)
    end_dt = datetime(trading_date.year, trading_date.month, trading_date.day, end_hour, end_minute, tzinfo=TZ)
    valid_runs = [
        row
        for row in runs
        if row.result == ShadowRunResult.PASSED.value
        and row.valid_quote_count > 0
        and row.ended_at is not None
        and start_dt <= _db_aware(row.started_at) <= end_dt
    ]
    return bool(valid_runs)


class MarketDataDegradationService:
    def evaluate_and_record(self, session: Session, *, provider: str, now: datetime, status: str, reason: str) -> MarketDataDegradationEventRecord | None:
        degrade_statuses = {
            ProviderHealthStatus.AUTH_FAILED.value,
            ProviderHealthStatus.PERMISSION_DENIED.value,
            ProviderHealthStatus.SCHEMA_CHANGED.value,
            ProviderHealthStatus.UNAVAILABLE.value,
            ProviderHealthStatus.RATE_LIMITED.value,
            ProviderHealthStatus.MAINTENANCE.value,
        }
        if status not in degrade_statuses:
            return None
        event_id = stable_id("market-data-degrade", provider, status, now.date().isoformat(), reason)
        existing = session.scalars(select(MarketDataDegradationEventRecord).where(MarketDataDegradationEventRecord.event_id == event_id)).first()
        if existing is not None:
            return existing
        event = MarketDataDegradationEventRecord(
            event_id=event_id,
            provider=provider,
            event_type=status,
            severity="P0" if status in {ProviderHealthStatus.AUTH_FAILED.value, ProviderHealthStatus.SCHEMA_CHANGED.value} else "P1",
            reason=reason,
            mode_from="LIVE_PAPER",
            mode_to="RECORDED",
            requires_manual_review=status in {ProviderHealthStatus.AUTH_FAILED.value, ProviderHealthStatus.SCHEMA_CHANGED.value},
            created_at=now,
            payload_json=stable_json({"environment": "PAPER_TRADING", "mode": "SHADOW", "provider": provider, "status": status}),
        )
        session.add(event)
        _notify_provider_change(session, provider, "degradation", "", status, now)
        session.flush()
        return event


def create_shadow_daily_report(
    session: Session,
    *,
    provider: str,
    trading_date: date,
    now: datetime,
    admission_status: str,
    trading_calendar: Any | None = None,
    policy: CompleteShadowTradingDayPolicy | None = None,
    replay_consistency: Decimal = Decimal("1.0"),
) -> MarketDataShadowDailyReportRecord:
    session.flush()
    runs = session.scalars(
        select(ProviderShadowRunRecord).where(ProviderShadowRunRecord.provider == provider, ProviderShadowRunRecord.trading_date == trading_date.isoformat())
    ).all()
    policy = policy or CompleteShadowTradingDayPolicy()
    day_eval = (
        policy.evaluate(trading_calendar=trading_calendar, trading_date=trading_date, runs=runs, replay_consistency=replay_consistency)
        if trading_calendar is not None
        else {
            "day_status": "INCOMPLETE" if not runs else "COMPLETE",
            "morning_session_complete": bool(runs),
            "afternoon_session_complete": bool(runs),
            "failure_reasons": [] if runs else ["no shadow runs recorded"],
            "symbol_coverage": "0.000000",
            "covered_symbols": 0,
            "configured_symbols": max((row.configured_symbol_count for row in runs), default=0),
            "replay_consistency": decimal_to_str(replay_consistency),
            "fills_created": sum(max(0, row.fills_after_count - row.fills_before_count) for row in runs),
            "account_immutability": "1.000000" if all(row.account_state_before_checksum == row.account_state_after_checksum for row in runs) else "0.000000",
        }
    )
    quote_received = sum(row.quote_received_count for row in runs)
    valid = sum(row.valid_quote_count for row in runs)
    invalid = sum(row.invalid_quote_count for row in runs)
    shadow_decisions = session.query(PaperShadowDecisionRecord).filter(PaperShadowDecisionRecord.provider == provider).count()
    paper_fills_created = sum(max(0, row.fills_after_count - row.fills_before_count) for row in runs)
    run_start = min((_db_aware(row.started_at) for row in runs), default=None)
    run_end = max((_db_aware(row.ended_at) for row in runs if row.ended_at is not None), default=None)
    latencies = sorted([Decimal(str(row.p95_latency_ms or 0)) for row in runs])
    provider_version = next((row.provider_version for row in runs if row.provider_version), "")
    payload = {
        "notice": "本报告仅用于在线行情质量和Shadow决策验证，不产生真实成交、模拟成交或账户收益；不代表真实成交或模拟账户收益。",
        "provider": provider,
        "provider_version": provider_version,
        "trading_date": trading_date.isoformat(),
        "run_start": "" if run_start is None else run_start.isoformat(),
        "run_end": "" if run_end is None else run_end.isoformat(),
        "morning_session_complete": day_eval["morning_session_complete"],
        "afternoon_session_complete": day_eval["afternoon_session_complete"],
        "configured_symbols": day_eval["configured_symbols"],
        "covered_symbols": day_eval["covered_symbols"],
        "symbol_coverage": day_eval["symbol_coverage"],
        "quote_received_count": quote_received,
        "valid_quote_count": valid,
        "invalid_quote_count": invalid,
        "stale_quote_count": sum(row.stale_quote_count for row in runs),
        "duplicate_quote_count": sum(row.duplicate_quote_count for row in runs),
        "out_of_order_count": sum(row.out_of_order_count for row in runs),
        "schema_error_count": sum(row.schema_error_count for row in runs),
        "network_error_count": sum(row.network_error_count for row in runs),
        "auth_error_count": 0,
        "rate_limit_count": sum(row.rate_limit_count for row in runs),
        "provider_availability": None if quote_received == 0 else decimal_to_str(Decimal(valid) / Decimal(quote_received)),
        "average_latency_ms": None if not runs else decimal_to_str(sum(Decimal(str(row.average_latency_ms or 0)) for row in runs) / Decimal(len(runs))),
        "p50_latency_ms": None if not latencies else decimal_to_str(latencies[len(latencies) // 2]),
        "p95_latency_ms": None if not runs else decimal_to_str(max(Decimal(str(row.p95_latency_ms or 0)) for row in runs)),
        "p99_latency_ms": None if not runs else decimal_to_str(max(Decimal(str(row.p99_latency_ms or 0)) for row in runs)),
        "missing_symbol_rate": max([row.missing_symbol_rate for row in runs], default=""),
        "invalid_quote_rate": None if quote_received == 0 else decimal_to_str(Decimal(invalid) / Decimal(quote_received)),
        "unknown_suspension_rate": "0.000000",
        "unknown_limit_rule_rate": "0.000000",
        "replay_consistency": day_eval["replay_consistency"],
        "account_immutability": day_eval["account_immutability"],
        "fills_created": paper_fills_created,
        "orders_modified": 0 if day_eval["account_immutability"] == "1.000000" else 1,
        "accounts_modified": 0 if day_eval["account_immutability"] == "1.000000" else 1,
        "positions_modified": 0 if day_eval["account_immutability"] == "1.000000" else 1,
        "ledger_entries_created": 0 if day_eval["account_immutability"] == "1.000000" else 1,
        "day_status": day_eval["day_status"],
        "failure_reasons": day_eval["failure_reasons"],
        "shadow_decision_count": shadow_decisions,
        "paper_fills_created_count": paper_fills_created,
        "account_state_unchanged": all(row.account_state_before_checksum == row.account_state_after_checksum for row in runs),
        "fill_count_unchanged": paper_fills_created == 0,
        "admission_status": admission_status,
        "unresolved_issues": [reason for row in runs for reason in json.loads(row.failure_reasons_json or "[]")],
    }
    existing = session.scalars(
        select(MarketDataShadowDailyReportRecord).where(
            MarketDataShadowDailyReportRecord.provider == provider,
            MarketDataShadowDailyReportRecord.trading_date == trading_date.isoformat(),
        )
    ).first()
    if existing is not None:
        existing.status = day_eval["day_status"]
        existing.report_json = stable_json(payload)
        existing.created_at = now
        return existing
    row = MarketDataShadowDailyReportRecord(
        provider=provider,
        trading_date=trading_date.isoformat(),
        status=day_eval["day_status"],
        report_json=stable_json(payload),
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def generate_admission_review_package(
    session: Session,
    *,
    provider: str,
    policy: MarketDataAdmissionPolicy,
    provider_configured: bool,
    output_dir: Path,
    now: datetime,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    progress = admission_status_summary(session, provider=provider, policy=policy, provider_configured=provider_configured)
    reports = session.scalars(
        select(MarketDataShadowDailyReportRecord)
        .where(MarketDataShadowDailyReportRecord.provider == provider)
        .order_by(MarketDataShadowDailyReportRecord.trading_date.asc())
    ).all()
    connectivity = session.scalars(
        select(ProviderConnectivityTestRecord)
        .where(ProviderConnectivityTestRecord.provider == provider)
        .order_by(ProviderConnectivityTestRecord.started_at.desc())
    ).all()
    degradations = session.scalars(
        select(MarketDataDegradationEventRecord)
        .where(MarketDataDegradationEventRecord.provider == provider)
        .order_by(MarketDataDegradationEventRecord.created_at.desc())
    ).all()
    shadow_decisions = session.query(PaperShadowDecisionRecord).filter(PaperShadowDecisionRecord.provider == provider).count()
    payload = {
        "notice": "This package is for manual review only. It contains no provider secrets and does not approve any live or paper execution mode.",
        "provider": provider,
        "provider_version": "live-paper-boundary-v1",
        "usage_authorization": "TO_BE_FILLED_BY_HUMAN_REVIEWER",
        "field_contract_version": FIELD_CONTRACT_VERSION,
        "generated_at": now.astimezone(TZ).isoformat(),
        "connectivity_results": [_connectivity_payload(row) for row in connectivity],
        "complete_trading_day_count": progress["completed_qualified_days"],
        "daily_reports": [_safe_json(row.report_json) for row in reports],
        "summary_quality_metrics": _aggregate_daily_report_metrics([_safe_json(row.report_json) for row in reports]),
        "threshold_checks": _threshold_checks(progress),
        "degradation_events": [_degradation_payload(row) for row in degradations],
        "provider_outage_events": [row.event_id for row in degradations if row.event_type in {ProviderHealthStatus.UNAVAILABLE.value, ProviderHealthStatus.MAINTENANCE.value}],
        "schema_change_events": [row.event_id for row in degradations if row.event_type == ProviderHealthStatus.SCHEMA_CHANGED.value],
        "recording_integrity": "review recorded_quote_files and daily report replay_consistency",
        "replay_consistency": "see daily_reports[].replay_consistency",
        "shadow_decision_count": shadow_decisions,
        "account_immutability": "see daily_reports[].account_immutability",
        "fill_immutability": "see daily_reports[].fills_created",
        "current_admission_status": progress["admission_status"],
        "unresolved_issues": progress["current_blockers"],
        "suggested_review_conclusion": "CONTINUE_OBSERVING" if progress["admission_status"] != AdmissionStatus.ELIGIBLE_FOR_REVIEW.value else "ELIGIBLE_FOR_HUMAN_REVIEW",
        "human_review_conclusion": "TO_BE_FILLED_BY_HUMAN_REVIEWER: REJECTED | CONTINUE_OBSERVING | APPROVED_FOR_LIMITED_PAPER_REVIEW",
    }
    stem = f"{provider}-{now.astimezone(TZ).strftime('%Y%m%d-%H%M%S')}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(stable_json(payload), encoding="utf-8")
    md_path.write_text(_review_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def _aggregate_daily_report_metrics(reports: list[dict[str, Any]]) -> dict[str, Any]:
    quote_received = sum(int(item.get("quote_received_count") or 0) for item in reports)
    valid = sum(int(item.get("valid_quote_count") or 0) for item in reports)
    return {
        "report_count": len(reports),
        "quote_received_count": quote_received,
        "valid_quote_count": valid,
        "provider_availability": None if quote_received == 0 else decimal_to_str(Decimal(valid) / Decimal(quote_received)),
        "fills_created": sum(int(item.get("fills_created") or 0) for item in reports),
        "day_statuses": {item.get("trading_date", ""): item.get("day_status", "") for item in reports},
    }


def _threshold_checks(progress: dict[str, Any]) -> dict[str, Any]:
    return {
        "required_complete_days": progress["required_complete_days"],
        "completed_qualified_days": progress["completed_qualified_days"],
        "failed_metrics": progress["failed_metrics"],
        "current_blockers": progress["current_blockers"],
        "admission_status": progress["admission_status"],
    }


def _connectivity_payload(row: ProviderConnectivityTestRecord) -> dict[str, Any]:
    return {
        "test_id": row.test_id,
        "provider": row.provider,
        "started_at": _db_aware(row.started_at).isoformat(),
        "ended_at": "" if row.ended_at is None else _db_aware(row.ended_at).isoformat(),
        "status": row.status,
        "error_type": row.error_type,
        "symbol_count": row.symbol_count,
        "quote_received_count": row.quote_received_count,
        "payload": _safe_json(row.payload_json),
    }


def _degradation_payload(row: MarketDataDegradationEventRecord) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "provider": row.provider,
        "event_type": row.event_type,
        "severity": row.severity,
        "reason": row.reason,
        "created_at": _db_aware(row.created_at).isoformat(),
    }


def _review_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Market Data Admission Review",
            "",
            "本审核包仅用于在线行情质量和Shadow决策验证，不产生真实成交、模拟成交或账户收益。",
            "",
            f"- Provider: {payload['provider']}",
            f"- Provider version: {payload['provider_version']}",
            f"- Field contract: {payload['field_contract_version']}",
            f"- Generated at: {payload['generated_at']}",
            f"- Current admission status: {payload['current_admission_status']}",
            f"- Complete trading days: {payload['complete_trading_day_count']}",
            f"- Shadow decisions: {payload['shadow_decision_count']}",
            "",
            "## Threshold Checks",
            "",
            "```json",
            stable_json(payload["threshold_checks"]),
            "```",
            "",
            "## Unresolved Issues",
            "",
            "\n".join(f"- {item}" for item in payload["unresolved_issues"]) or "- None",
            "",
            "## Human Review",
            "",
            "Conclusion: TO_BE_FILLED_BY_HUMAN_REVIEWER",
            "",
            "Allowed values: REJECTED | CONTINUE_OBSERVING | APPROVED_FOR_LIMITED_PAPER_REVIEW",
            "",
        ]
    )


def quote_from_record(row: MarketQuoteSnapshotRecord) -> RealTimeQuoteSnapshot:
    return RealTimeQuoteSnapshot(
        quote_id=row.quote_id,
        provider=row.provider,
        provider_version=row.provider_version,
        symbol=row.symbol,
        exchange=row.exchange,
        trading_date=date.fromisoformat(row.trading_date),
        market_time=_db_aware(row.market_time),
        received_at=_db_aware(row.received_at),
        validated_at=_db_aware(row.validated_at),
        sequence=row.sequence,
        open=Decimal(row.open_price),
        high=Decimal(row.high_price),
        low=Decimal(row.low_price),
        last_price=Decimal(row.last_price),
        previous_close=None if row.previous_close is None else Decimal(row.previous_close),
        volume=row.volume,
        amount=None if row.amount is None else Decimal(row.amount),
        bid_price=None if row.bid_price is None else Decimal(row.bid_price),
        ask_price=None if row.ask_price is None else Decimal(row.ask_price),
        suspension_status=row.suspension_status,
        price_limit_up=None if row.price_limit_up is None else Decimal(row.price_limit_up),
        price_limit_down=None if row.price_limit_down is None else Decimal(row.price_limit_down),
        data_checksum=row.data_checksum,
        calendar_version=row.calendar_version,
        raw_schema_version=row.raw_schema_version,
        quality_status=row.quality_status,
        quality_reasons=tuple(json.loads(getattr(row, "quality_reasons_json", "[]") or "[]")),
    )


def quote_payload(snapshot: RealTimeQuoteSnapshot) -> dict[str, Any]:
    return {
        "quote_id": snapshot.quote_id,
        "provider": snapshot.provider,
        "provider_version": snapshot.provider_version,
        "symbol": snapshot.symbol,
        "exchange": snapshot.exchange,
        "trading_date": snapshot.trading_date.isoformat(),
        "market_time": snapshot.market_time.astimezone(TZ).isoformat(),
        "received_at": snapshot.received_at.astimezone(TZ).isoformat(),
        "validated_at": snapshot.validated_at.astimezone(TZ).isoformat(),
        "open": decimal_to_str(snapshot.open),
        "high": decimal_to_str(snapshot.high),
        "low": decimal_to_str(snapshot.low),
        "last_price": decimal_to_str(snapshot.last_price),
        "previous_close": None if snapshot.previous_close is None else decimal_to_str(snapshot.previous_close),
        "volume": snapshot.volume,
        "amount": None if snapshot.amount is None else decimal_to_str(snapshot.amount),
        "bid_price": None if snapshot.bid_price is None else decimal_to_str(snapshot.bid_price),
        "ask_price": None if snapshot.ask_price is None else decimal_to_str(snapshot.ask_price),
        "suspension_status": snapshot.suspension_status,
        "price_limit_up": None if snapshot.price_limit_up is None else decimal_to_str(snapshot.price_limit_up),
        "price_limit_down": None if snapshot.price_limit_down is None else decimal_to_str(snapshot.price_limit_down),
        "quality_status": snapshot.quality_status,
        "quality_reasons": list(snapshot.quality_reasons),
        "data_checksum": snapshot.data_checksum,
    }


def latest_provider_status(session: Session) -> dict[str, Any]:
    rows = session.scalars(select(MarketDataProviderStatusRecord).order_by(MarketDataProviderStatusRecord.updated_at.desc())).all()
    if not rows:
        return {}
    return {
        row.provider: {
            "status": row.status,
            "last_request_at": None if row.last_request_at is None else _db_aware(row.last_request_at).isoformat(),
            "last_success_at": None if row.last_success_at is None else _db_aware(row.last_success_at).isoformat(),
            "last_quote_market_time": None if row.last_quote_market_time is None else _db_aware(row.last_quote_market_time).isoformat(),
            "consecutive_failures": row.consecutive_failures,
            "consecutive_successes": row.consecutive_successes,
            "request_count": row.request_count,
            "success_count": row.success_count,
            "failure_count": row.failure_count,
            "stale_symbol_count": row.stale_symbol_count,
            "invalid_quote_count": row.invalid_quote_count,
            "duplicate_quote_count": row.duplicate_quote_count,
            "out_of_order_count": row.out_of_order_count,
            "average_latency_ms": row.average_latency_ms,
            "p95_latency_ms": row.p95_latency_ms,
            "last_error_type": row.last_error_type,
        }
        for row in rows
    }


def runtime_provider_from_settings(settings) -> RealTimeMarketDataProvider:
    provider = settings.market_live_provider.strip().lower()
    if provider == "fixture":
        return FixtureQuoteProvider()
    if provider == "recorded":
        return RecordedQuoteFileProvider(settings.market_live_record_resolved_dir)
    if provider in {"live_paper", "live-paper"}:
        rate_limiter = RateLimiter(
            requests_per_second=settings.market_live_requests_per_second,
            requests_per_minute=settings.market_live_requests_per_minute,
            max_concurrency=settings.market_live_max_concurrency,
        )
        return RetryingQuoteProvider(
            LivePaperQuoteProvider(
                api_base_url=settings.market_live_api_base_url,
                api_key=settings.market_live_api_key,
                api_secret=settings.market_live_api_secret,
                account_id=settings.market_live_account_id,
                connect_timeout_seconds=settings.market_live_connect_timeout_seconds,
                read_timeout_seconds=settings.market_live_read_timeout_seconds,
                max_symbols_per_request=settings.market_live_max_symbols_per_request,
            ),
            max_attempts=settings.market_live_max_attempts,
            initial_backoff_seconds=settings.market_live_initial_backoff_seconds,
            max_backoff_seconds=settings.market_live_max_backoff_seconds,
            jitter_seconds=settings.market_live_jitter_seconds,
            rate_limiter=rate_limiter,
        )
    raise ValueError(f"unsupported realtime quote provider: {settings.market_live_provider}")


def live_provider_configured(settings) -> bool:
    return (
        settings.market_data_mode == "LIVE_PAPER"
        and settings.market_live_enabled
        and settings.market_live_provider.strip().lower() in {"live_paper", "live-paper"}
        and bool(settings.market_live_api_base_url.strip())
        and settings.market_live_shadow_mode
    )


def run_connectivity_check(
    *,
    session_factory,
    provider: RealTimeMarketDataProvider,
    settings,
    calendar_version: str,
    clock: Clock,
    symbols: list[str],
) -> tuple[dict[str, Any], int]:
    now = clock.now().astimezone(TZ)
    provider_configured = live_provider_configured(settings)
    test_id = stable_id("provider-connectivity", provider.provider_name, now.isoformat(), stable_json(symbols))
    summary = {
        "status": "PROVIDER_NOT_CONFIGURED" if not provider_configured else "UNKNOWN",
        "environment": "PAPER_TRADING",
        "mode": "SHADOW",
        "provider_configured": provider_configured,
        "network_connectivity": "NOT_CONFIGURED" if not provider_configured else "UNKNOWN",
        "authentication": "NOT_CONFIGURED" if not provider_configured else "UNKNOWN",
        "quotes_received": 0,
        "valid_quotes": 0,
        "fills_created": 0,
        "orders_modified": 0,
        "accounts_modified": 0,
        "positions_modified": 0,
        "ledger_entries_created": 0,
    }
    if not provider_configured:
        try:
            with session_factory() as session:
                _save_connectivity_record(session, test_id=test_id, provider=provider.provider_name, started_at=now, ended_at=now, status="PROVIDER_NOT_CONFIGURED", error_type=ProviderErrorType.PROVIDER_DISABLED.value, message="provider is not configured", symbols=symbols, quotes_received=0, payload=summary)
                session.commit()
        except Exception:
            logger.warning("connectivity_not_configured_record_skipped", exc_info=True)
        return summary, 2
    with session_factory() as session:
        before_checksum = _paper_state_checksum(session)
        before_counts = _paper_state_counts(session)
    gateway = MarketDataGateway(
        session_factory=session_factory,
        provider=provider,
        calendar_version=calendar_version,
        clock=clock,
        config=RealTimeQuoteConfig(
            max_age_seconds=settings.market_live_max_age_seconds,
            clock_skew_seconds=settings.market_live_clock_skew_seconds,
            provider_failure_threshold=settings.market_live_provider_failure_threshold,
            provider_recovery_success_count=settings.market_live_provider_recovery_success_count,
        ),
        recorder=QuoteRecorder(settings.market_live_record_resolved_dir, record_raw=settings.market_live_record_raw_responses),
    )
    status = "FAILED"
    error_type = ""
    message = ""
    try:
        result = gateway.run_once(symbols)
        summary["quotes_received"] = int(result["saved"])
        with session_factory() as session:
            valid_quotes = session.query(MarketQuoteSnapshotRecord).filter(
                MarketQuoteSnapshotRecord.provider == provider.provider_name,
                MarketQuoteSnapshotRecord.quality_status == QuoteQualityStatus.VALID.value,
            ).count()
            after_checksum = _paper_state_checksum(session)
            after_counts = _paper_state_counts(session)
        summary["valid_quotes"] = valid_quotes
        summary["network_connectivity"] = "PASSED"
        summary["authentication"] = "PASSED"
        summary["status"] = "PASSED"
        status = "PASSED"
    except Exception as exc:
        error_type = _safe_error_type(exc)
        message = str(exc)
        if error_type in {ProviderErrorType.AUTHENTICATION_ERROR.value, ProviderErrorType.PERMISSION_DENIED.value}:
            summary["network_connectivity"] = "PASSED"
            summary["authentication"] = error_type
        else:
            summary["network_connectivity"] = error_type
            summary["authentication"] = "UNKNOWN"
        summary["status"] = "FAILED"
        with session_factory() as session:
            after_checksum = _paper_state_checksum(session)
            after_counts = _paper_state_counts(session)
    summary["fills_created"] = max(0, after_counts["fills"] - before_counts["fills"])
    state_changed = before_checksum != after_checksum
    summary["orders_modified"] = 1 if state_changed else 0
    summary["accounts_modified"] = 1 if state_changed else 0
    summary["positions_modified"] = 1 if state_changed else 0
    summary["ledger_entries_created"] = max(0, after_counts["ledger"] - before_counts["ledger"])
    if state_changed or summary["fills_created"] or summary["ledger_entries_created"]:
        status = ShadowRunResult.ACCOUNT_MUTATION_DETECTED.value
        summary["status"] = status
        error_type = status
        message = "LIVE_PAPER connectivity mutated paper trading state"
    with session_factory() as session:
        _save_connectivity_record(
            session,
            test_id=test_id,
            provider=provider.provider_name,
            started_at=now,
            ended_at=clock.now().astimezone(TZ),
            status=status,
            error_type=error_type,
            message=message,
            symbols=symbols,
            quotes_received=int(summary["quotes_received"]),
            payload=summary,
        )
        session.commit()
    return summary, 0 if status == "PASSED" else 1


def _save_connectivity_record(
    session: Session,
    *,
    test_id: str,
    provider: str,
    started_at: datetime,
    ended_at: datetime,
    status: str,
    error_type: str,
    message: str,
    symbols: list[str],
    quotes_received: int,
    payload: dict[str, Any],
) -> ProviderConnectivityTestRecord:
    existing = session.scalars(select(ProviderConnectivityTestRecord).where(ProviderConnectivityTestRecord.test_id == test_id)).first()
    if existing is not None:
        return existing
    row = ProviderConnectivityTestRecord(
        test_id=test_id,
        provider=provider,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        error_type=error_type,
        message=message[:500],
        symbol_count=len(set(symbols)),
        quote_received_count=quotes_received,
        payload_json=stable_json(payload),
    )
    session.add(row)
    session.flush()
    return row


def main(argv: list[str] | None = None) -> int:
    from .config import get_settings
    from .service import market_data_config_from_settings

    parser = argparse.ArgumentParser(description="PAPER_TRADING quote ingestion runtime. Does not execute orders.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--provider")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--symbols")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--connectivity-test", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--admission-status", action="store_true")
    parser.add_argument("--generate-admission-review", action="store_true")
    parser.add_argument("--output-dir", default="data/admission_reviews")
    parser.add_argument("--test-now")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.shadow and not settings.market_live_shadow_mode:
        raise RuntimeError("LIVE_PAPER Shadow mode is required and cannot be disabled")
    clock = TestClock(datetime.fromisoformat(args.test_now)) if args.test_now else SystemClock(settings.timezone)
    provider = runtime_provider_from_settings(settings)
    if args.provider:
        override = args.provider.strip().lower()
        provider = {
            "fixture": FixtureQuoteProvider,
            "recorded": lambda: RecordedQuoteFileProvider(settings.market_live_record_resolved_dir),
            "live-paper": lambda: RetryingQuoteProvider(
                LivePaperQuoteProvider(
                    api_base_url=settings.market_live_api_base_url,
                    api_key=settings.market_live_api_key,
                    api_secret=settings.market_live_api_secret,
                    account_id=settings.market_live_account_id,
                    connect_timeout_seconds=settings.market_live_connect_timeout_seconds,
                    read_timeout_seconds=settings.market_live_read_timeout_seconds,
                    max_symbols_per_request=settings.market_live_max_symbols_per_request,
                ),
                max_attempts=settings.market_live_max_attempts,
                initial_backoff_seconds=settings.market_live_initial_backoff_seconds,
                max_backoff_seconds=settings.market_live_max_backoff_seconds,
                jitter_seconds=settings.market_live_jitter_seconds,
                rate_limiter=RateLimiter(
                    requests_per_second=settings.market_live_requests_per_second,
                    requests_per_minute=settings.market_live_requests_per_minute,
                    max_concurrency=settings.market_live_max_concurrency,
                ),
            ),
            "live_paper": lambda: RetryingQuoteProvider(
                LivePaperQuoteProvider(
                    api_base_url=settings.market_live_api_base_url,
                    api_key=settings.market_live_api_key,
                    api_secret=settings.market_live_api_secret,
                    account_id=settings.market_live_account_id,
                    connect_timeout_seconds=settings.market_live_connect_timeout_seconds,
                    read_timeout_seconds=settings.market_live_read_timeout_seconds,
                    max_symbols_per_request=settings.market_live_max_symbols_per_request,
                ),
                max_attempts=settings.market_live_max_attempts,
                initial_backoff_seconds=settings.market_live_initial_backoff_seconds,
                max_backoff_seconds=settings.market_live_max_backoff_seconds,
                jitter_seconds=settings.market_live_jitter_seconds,
                rate_limiter=RateLimiter(
                    requests_per_second=settings.market_live_requests_per_second,
                    requests_per_minute=settings.market_live_requests_per_minute,
                    max_concurrency=settings.market_live_max_concurrency,
                ),
            ),
        }[override]()
    if args.health_check:
        print(json.dumps(provider.health_check(), ensure_ascii=False, sort_keys=True))
        return 0
    calendar = market_data_config_from_settings(settings).calendar
    if calendar is None:
        raise RuntimeError("market data runtime requires local calendar")
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] if args.symbols else []
    if args.connectivity_test:
        if live_provider_configured(settings):
            assert_schema_ready_for_writes(engine)
        if not symbols:
            symbols = settings.watchlist[: min(2, len(settings.watchlist))]
        summary, exit_code = run_connectivity_check(
            session_factory=SessionLocal,
            provider=provider,
            settings=settings,
            calendar_version=calendar.version,
            clock=clock,
            symbols=symbols,
        )
        for key in [
            "environment",
            "status",
            "mode",
            "provider_configured",
            "network_connectivity",
            "authentication",
            "quotes_received",
            "valid_quotes",
            "fills_created",
            "orders_modified",
            "accounts_modified",
            "positions_modified",
            "ledger_entries_created",
        ]:
            print(f"{key}={str(summary[key]).lower() if isinstance(summary[key], bool) else summary[key]}")
        return exit_code
    if args.admission_status:
        with SessionLocal() as session:
            progress = admission_status_summary(
                session,
                provider="live_paper",
                policy=MarketDataAdmissionPolicy.from_settings(settings),
                provider_configured=live_provider_configured(settings),
            )
        print(json.dumps({k: v for k, v in progress.items() if k != "uses_daily_reports"}, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.report_only:
        assert_schema_ready_for_writes(engine)
        now = clock.now().astimezone(TZ)
        trading_date = calendar.latest_completed_trading_day(now)
        with SessionLocal() as session:
            progress = admission_status_summary(session, provider="live_paper", policy=MarketDataAdmissionPolicy.from_settings(settings), provider_configured=live_provider_configured(settings))
            report = create_shadow_daily_report(
                session,
                provider="live_paper",
                trading_date=trading_date,
                now=now,
                admission_status=progress["admission_status"],
                trading_calendar=calendar,
            )
            session.commit()
            print(report.report_json)
        return 0
    if args.generate_admission_review:
        assert_schema_ready_for_writes(engine)
        with SessionLocal() as session:
            paths = generate_admission_review_package(
                session,
                provider="live_paper",
                policy=MarketDataAdmissionPolicy.from_settings(settings),
                provider_configured=live_provider_configured(settings),
                output_dir=Path(args.output_dir),
                now=clock.now().astimezone(TZ),
            )
        print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False, sort_keys=True))
        return 0
    gateway = MarketDataGateway(
        session_factory=SessionLocal,
        provider=provider,
        calendar_version=calendar.version,
        clock=clock,
        config=RealTimeQuoteConfig(
            max_age_seconds=settings.market_live_max_age_seconds,
            clock_skew_seconds=settings.market_live_clock_skew_seconds,
            provider_failure_threshold=settings.market_live_provider_failure_threshold,
            provider_recovery_success_count=settings.market_live_provider_recovery_success_count,
        ),
        recorder=QuoteRecorder(settings.market_live_record_resolved_dir, record_raw=settings.market_live_record_raw_responses)
        if args.record or settings.market_live_record_quotes
        else None,
    )
    if args.once:
        assert_schema_ready_for_writes(engine)
        if not symbols:
            with SessionLocal() as session:
                symbols = sorted({row.symbol for row in session.scalars(select(PaperOrderRecord)).all()})
        print(json.dumps(gateway.run_once(symbols), ensure_ascii=False, sort_keys=True))
        return 0
    while True:
        assert_schema_ready_for_writes(engine)
        with SessionLocal() as session:
            symbols = sorted({row.symbol for row in session.scalars(select(PaperOrderRecord)).all()})
        gateway.run_once(symbols)
        time_module.sleep(settings.market_live_poll_seconds)


def _provider_success(
    session: Session,
    provider: str,
    instance_id: str,
    now: datetime,
    snapshots: list[RealTimeQuoteSnapshot],
    latency_ms: float,
    invalid_count: int,
    duplicate_count: int,
    out_of_order_count: int,
    config: RealTimeQuoteConfig,
) -> None:
    row = _provider_status(session, provider, instance_id)
    previous_status = row.status
    row.last_request_at = now
    row.last_success_at = now
    row.last_quote_market_time = max((snap.market_time for snap in snapshots), default=None)
    row.consecutive_failures = 0
    row.consecutive_successes += 1
    row.request_count += 1
    row.success_count += 1
    row.average_latency_ms = latency_ms if row.average_latency_ms == 0 else (row.average_latency_ms + latency_ms) / 2
    row.p95_latency_ms = max(row.p95_latency_ms or 0.0, latency_ms)
    row.invalid_quote_count = invalid_count
    row.duplicate_quote_count += duplicate_count
    row.out_of_order_count += out_of_order_count
    row.stale_symbol_count = len([snap for snap in snapshots if snap.quality_status == QuoteQualityStatus.STALE.value])
    if invalid_count == 0 and row.consecutive_successes >= config.provider_recovery_success_count:
        row.status = ProviderHealthStatus.HEALTHY.value
    elif invalid_count == 0 and previous_status in {ProviderHealthStatus.UNAVAILABLE.value, ProviderHealthStatus.AUTH_FAILED.value, ProviderHealthStatus.SCHEMA_CHANGED.value}:
        row.status = ProviderHealthStatus.DEGRADED.value
    else:
        row.status = ProviderHealthStatus.HEALTHY.value if invalid_count == 0 else ProviderHealthStatus.DEGRADED.value
    row.last_error_type = ""
    row.last_error_message = ""
    row.updated_at = now
    _notify_provider_change(session, provider, instance_id, previous_status, row.status, now)


def _provider_failure(session: Session, provider: str, instance_id: str, now: datetime, exc: Exception, config: RealTimeQuoteConfig) -> None:
    row = _provider_status(session, provider, instance_id)
    previous_status = row.status
    row.last_request_at = now
    row.consecutive_failures += 1
    row.consecutive_successes = 0
    row.request_count += 1
    row.failure_count += 1
    error_type = exc.error_type.value if isinstance(exc, QuoteProviderError) else type(exc).__name__
    if isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.PROVIDER_DISABLED:
        row.status = ProviderHealthStatus.NOT_CONFIGURED.value
    elif isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.AUTHENTICATION_ERROR:
        row.status = ProviderHealthStatus.AUTH_FAILED.value
    elif isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.PERMISSION_DENIED:
        row.status = ProviderHealthStatus.PERMISSION_DENIED.value
    elif isinstance(exc, QuoteProviderError) and exc.error_type in {ProviderErrorType.RATE_LIMITED, ProviderErrorType.QUOTA_EXCEEDED}:
        row.status = ProviderHealthStatus.RATE_LIMITED.value
    elif isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.PROVIDER_MAINTENANCE:
        row.status = ProviderHealthStatus.MAINTENANCE.value
    elif isinstance(exc, QuoteProviderError) and exc.error_type == ProviderErrorType.SCHEMA_CHANGED:
        row.status = ProviderHealthStatus.SCHEMA_CHANGED.value
    else:
        row.status = ProviderHealthStatus.UNAVAILABLE.value if row.consecutive_failures >= config.provider_failure_threshold else ProviderHealthStatus.DEGRADED.value
    row.last_error_type = error_type
    row.last_error_message = str(exc)[:500]
    row.updated_at = now
    _notify_provider_change(session, provider, instance_id, previous_status, row.status, now)


def _provider_status(session: Session, provider: str, instance_id: str) -> MarketDataProviderStatusRecord:
    row = session.scalars(
        select(MarketDataProviderStatusRecord).where(
            MarketDataProviderStatusRecord.provider == provider,
            MarketDataProviderStatusRecord.instance_id == instance_id,
        )
    ).first()
    if row is None:
        row = MarketDataProviderStatusRecord(
            provider=provider,
            instance_id=instance_id,
            status=ProviderHealthStatus.DISABLED.value,
            updated_at=datetime(1970, 1, 1, tzinfo=TZ),
        )
        session.add(row)
        session.flush()
    return row


def _paper_state_checksum(session: Session) -> str:
    payload = {
        "accounts": [
            {
                "account_id": row.account_id,
                "status": row.status,
                "cash_available": row.cash_available,
                "cash_frozen": row.cash_frozen,
                "market_value": row.market_value,
                "total_equity": row.total_equity,
            }
            for row in session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.account_id.asc())).all()
        ],
        "orders": [
            {
                "paper_order_id": row.paper_order_id,
                "status": row.status,
                "remaining_quantity": row.remaining_quantity,
                "rejection_reason": row.rejection_reason,
            }
            for row in session.scalars(select(PaperOrderRecord).order_by(PaperOrderRecord.paper_order_id.asc())).all()
        ],
        "positions": [
            {
                "account_id": row.account_id,
                "symbol": row.symbol,
                "total_quantity": row.total_quantity,
                "available_quantity": row.available_quantity,
                "locked_quantity": row.locked_quantity,
            }
            for row in session.scalars(select(PaperPositionRecord).order_by(PaperPositionRecord.account_id.asc(), PaperPositionRecord.symbol.asc())).all()
        ],
        "ledger": [row.entry_id for row in session.scalars(select(PaperLedgerEntryRecord).order_by(PaperLedgerEntryRecord.entry_id.asc())).all()],
    }
    return stable_id("paper-shadow-state", stable_json(payload))


def _paper_state_counts(session: Session) -> dict[str, int]:
    return {
        "accounts": session.query(PaperAccountRecord).count(),
        "orders": session.query(PaperOrderRecord).count(),
        "positions": session.query(PaperPositionRecord).count(),
        "fills": session.query(PaperFillRecord).count(),
        "ledger": session.query(PaperLedgerEntryRecord).count(),
    }


def _save_provider_shadow_run(
    session: Session,
    *,
    run_id: str,
    provider: str,
    provider_version: str | None,
    started_at: datetime,
    ended_at: datetime,
    symbols: list[str],
    result: str,
    failure_reasons: list[str],
    before_checksum: str,
    after_checksum: str,
    fills_before: int,
    fills_after: int,
    metrics: dict[str, Any],
) -> ProviderShadowRunRecord:
    existing = session.scalars(select(ProviderShadowRunRecord).where(ProviderShadowRunRecord.run_id == run_id)).first()
    if existing is not None:
        return existing
    status = "COMPLETED" if result == ShadowRunResult.PASSED.value else "FAILED"
    row = ProviderShadowRunRecord(
        run_id=run_id,
        provider=provider,
        provider_version=provider_version,
        started_at=started_at,
        ended_at=ended_at,
        trading_date=started_at.astimezone(TZ).date().isoformat(),
        symbol_universe_version=stable_id("symbol-universe", stable_json(sorted(symbols))),
        configured_symbol_count=len(set(symbols)),
        status=status,
        quote_received_count=int(metrics.get("quote_received_count", 0)),
        valid_quote_count=int(metrics.get("valid_quote_count", 0)),
        invalid_quote_count=int(metrics.get("invalid_quote_count", 0)),
        stale_quote_count=int(metrics.get("stale_quote_count", 0)),
        duplicate_quote_count=int(metrics.get("duplicate_quote_count", 0)),
        out_of_order_count=int(metrics.get("out_of_order_count", 0)),
        schema_error_count=int(metrics.get("schema_error_count", 0)),
        network_error_count=int(metrics.get("network_error_count", 0)),
        rate_limit_count=int(metrics.get("rate_limit_count", 0)),
        availability=str(metrics.get("availability", "")),
        average_latency_ms=metrics.get("average_latency_ms"),
        p50_latency_ms=metrics.get("p50_latency_ms"),
        p95_latency_ms=metrics.get("p95_latency_ms"),
        p99_latency_ms=metrics.get("p99_latency_ms"),
        missing_symbol_rate=str(metrics.get("missing_symbol_rate", "")),
        account_state_before_checksum=before_checksum,
        account_state_after_checksum=after_checksum,
        fills_before_count=fills_before,
        fills_after_count=fills_after,
        result=result,
        failure_reasons_json=stable_json(failure_reasons),
        payload_json=stable_json({k: v for k, v in metrics.items() if not isinstance(v, datetime)}),
    )
    session.add(row)
    session.flush()
    return row


def _safe_error_type(exc: Exception) -> str:
    if isinstance(exc, QuoteProviderError):
        return exc.error_type.value
    return type(exc).__name__


def _is_duplicate_quote(session: Session, snapshot: RealTimeQuoteSnapshot) -> bool:
    return (
        session.scalars(
            select(MarketQuoteSnapshotRecord).where(
                MarketQuoteSnapshotRecord.provider == snapshot.provider,
                MarketQuoteSnapshotRecord.symbol == snapshot.symbol,
                MarketQuoteSnapshotRecord.market_time == snapshot.market_time,
                MarketQuoteSnapshotRecord.data_checksum == snapshot.data_checksum,
            )
        ).first()
        is not None
    )


def _is_out_of_order_quote(session: Session, snapshot: RealTimeQuoteSnapshot) -> bool:
    latest = session.scalars(
        select(MarketQuoteSnapshotRecord)
        .where(
            MarketQuoteSnapshotRecord.provider == snapshot.provider,
            MarketQuoteSnapshotRecord.symbol == snapshot.symbol,
            MarketQuoteSnapshotRecord.quality_status == QuoteQualityStatus.VALID.value,
        )
        .order_by(MarketQuoteSnapshotRecord.market_time.desc())
        .limit(1)
    ).first()
    return latest is not None and _db_aware(latest.market_time) > snapshot.market_time


def _with_quality(snapshot: RealTimeQuoteSnapshot, quality: QuoteQualityStatus, reason: str) -> RealTimeQuoteSnapshot:
    return RealTimeQuoteSnapshot(
        **{
            **snapshot.__dict__,
            "quality_status": quality.value,
            "quality_reasons": tuple([*snapshot.quality_reasons, reason]),
        }
    )


def _notify_provider_change(session: Session, provider: str, instance_id: str, previous: str, current: str, now: datetime) -> None:
    if previous == current:
        return
    dedupe = stable_id("provider-status", provider, instance_id, current, now.date().isoformat())
    if session.scalars(select(NotificationOutboxRecord).where(NotificationOutboxRecord.dedupe_key == dedupe)).first():
        return
    session.add(
        NotificationOutboxRecord(
            message_id=stable_id("provider-status-message", dedupe),
            dedupe_key=dedupe,
            account_id="system",
            notification_type="MARKET_DATA_PROVIDER_STATUS",
            payload_json=stable_json({"environment": "PAPER_TRADING", "provider": provider, "status": current}),
            status="PENDING",
            retry_count=0,
            last_error="",
            created_at=now,
            updated_at=now,
        )
    )


def _invalid(
    raw: dict[str, Any],
    provider: str,
    provider_version: str | None,
    received: datetime,
    validated: datetime,
    calendar_version: str,
    raw_schema_version: str,
    quality: QuoteQualityStatus,
    reasons: list[str] | None = None,
) -> RealTimeQuoteSnapshot:
    symbol = _normalize_symbol(str(raw.get("symbol") or "UNKNOWN"))
    trading_date = date.fromisoformat(str(raw.get("trading_date") or received.date().isoformat()))
    market_time = received
    checksum = hashlib.sha256(stable_json({"raw": str(raw), "quality": quality.value}).encode("utf-8")).hexdigest()
    return RealTimeQuoteSnapshot(
        quote_id=stable_id("quote", provider, symbol, market_time.isoformat(), checksum),
        provider=provider,
        provider_version=provider_version,
        symbol=symbol,
        exchange=str(raw.get("exchange") or _exchange(symbol)),
        trading_date=trading_date,
        market_time=market_time,
        received_at=received,
        validated_at=validated,
        sequence=None,
        open=Decimal("0"),
        high=Decimal("0"),
        low=Decimal("0"),
        last_price=Decimal("0"),
        previous_close=None,
        volume=0,
        amount=None,
        bid_price=None,
        ask_price=None,
        suspension_status=SuspensionStatus.UNKNOWN.value,
        price_limit_up=None,
        price_limit_down=None,
        data_checksum=checksum,
        calendar_version=calendar_version,
        raw_schema_version=raw_schema_version,
        quality_status=quality.value,
        quality_reasons=tuple(reasons or [quality.value]),
    )


def _checksum_payload(**kwargs) -> dict[str, Any]:
    return {
        key: (
            value.isoformat()
            if isinstance(value, (datetime, date))
            else decimal_to_str(value)
            if isinstance(value, Decimal)
            else value
        )
        for key, value in kwargs.items()
    }


def _redact_raw(raw: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in raw.items():
        if "key" in key.lower() or "token" in key.lower() or "secret" in key.lower() or "authorization" in key.lower():
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def _raw_from_recorded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": payload["symbol"],
        "exchange": payload["exchange"],
        "trading_date": payload["trading_date"],
        "market_time": payload["market_time"],
        "open": payload.get("open", payload.get("last_price")),
        "high": payload.get("high", payload.get("last_price")),
        "low": payload.get("low", payload.get("last_price")),
        "last_price": payload["last_price"] if "last_price" in payload else payload.get("price", "0"),
        "previous_close": payload.get("previous_close"),
        "volume": payload.get("volume", 0),
        "amount": payload.get("amount"),
        "bid_price": payload.get("bid_price"),
        "ask_price": payload.get("ask_price"),
        "suspension_status": payload.get("suspension_status", SuspensionStatus.UNKNOWN.value),
        "price_limit_up": payload.get("price_limit_up"),
        "price_limit_down": payload.get("price_limit_down"),
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise MarketDataError("realtime quote datetime must include timezone")
    return value.astimezone(TZ)


def _db_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _exchange(symbol: str) -> str:
    symbol = str(symbol).upper()
    if symbol.endswith(".SH") or symbol.startswith("6"):
        return "SSE"
    return "SZSE"


def _normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if "." in value:
        code, suffix = value.split(".", 1)
        if suffix in {"SH", "SSE"}:
            return f"{code}.SH"
        if suffix in {"SZ", "SZSE"}:
            return f"{code}.SZ"
    if value.startswith("6"):
        return f"{value}.SH"
    if value.isdigit():
        return f"{value}.SZ"
    return value


def _backoff(attempt: int, initial: float, maximum: float, jitter: float, random_fn: Callable[[], float]) -> float:
    base = min(maximum, initial * (2 ** (attempt - 1)))
    return base + (random_fn() * jitter if jitter else 0.0)


if __name__ == "__main__":
    raise SystemExit(main())
