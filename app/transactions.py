from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError


logger = logging.getLogger(__name__)
T = TypeVar("T")


class Sleeper(Protocol):
    def __call__(self, seconds: float) -> None:
        ...


class RandomSource(Protocol):
    def __call__(self) -> float:
        ...


class TransactionRetryExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class TransactionRetryConfig:
    max_attempts: int = 3
    initial_backoff_ms: int = 50
    max_backoff_ms: int = 500
    jitter_ms: int = 30
    retry_lock_not_available: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("transaction max_attempts must be >= 1")
        if self.initial_backoff_ms < 0:
            raise ValueError("transaction initial_backoff_ms must not be negative")
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("transaction max_backoff_ms must be >= initial_backoff_ms")
        if self.jitter_ms < 0:
            raise ValueError("transaction jitter_ms must not be negative")


@dataclass(frozen=True)
class DatabaseExceptionClassifier:
    retry_lock_not_available: bool = False

    def sqlstate(self, exc: BaseException) -> str | None:
        current: BaseException | None = exc
        while current is not None:
            code = getattr(current, "sqlstate", None)
            if code:
                return str(code)
            orig = getattr(current, "orig", None)
            if orig is not None and orig is not current:
                current = orig
                continue
            cause = getattr(current, "__cause__", None)
            current = cause if cause is not current else None
        return None

    def is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, IntegrityError):
            return False
        if not isinstance(exc, DBAPIError):
            return False
        code = self.sqlstate(exc)
        if code in {"40P01", "40001"}:
            return True
        if code == "55P03":
            return self.retry_lock_not_available
        return False


class TransactionRunner:
    def __init__(
        self,
        *,
        session_factory,
        config: TransactionRetryConfig | None = None,
        classifier: DatabaseExceptionClassifier | None = None,
        sleep: Sleeper | None = None,
        random_source: RandomSource | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config or TransactionRetryConfig()
        self.classifier = classifier or DatabaseExceptionClassifier(
            retry_lock_not_available=self.config.retry_lock_not_available
        )
        self.sleep = sleep or time.sleep
        self.random_source = random_source or random.random

    def run(self, fn: Callable[[Any, int], T]) -> T:
        last_exc: BaseException | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            with self.session_factory() as session:
                try:
                    result = fn(session, attempt)
                    session.commit()
                    return result
                except BaseException as exc:
                    session.rollback()
                    last_exc = exc
                    retryable = self.classifier.is_retryable(exc)
                    logger.warning(
                        "transaction_attempt_failed",
                        extra={
                            "attempt": attempt,
                            "sqlstate": self.classifier.sqlstate(exc),
                            "exception_type": type(exc).__name__,
                            "retryable": retryable,
                        },
                    )
                    if not retryable or attempt >= self.config.max_attempts:
                        raise
            self.sleep(self._backoff_seconds(attempt))
        raise TransactionRetryExhausted(f"transaction failed after {self.config.max_attempts} attempts") from last_exc

    def _backoff_seconds(self, attempt: int) -> float:
        base = min(self.config.max_backoff_ms, self.config.initial_backoff_ms * (2 ** (attempt - 1)))
        jitter = self.random_source() * self.config.jitter_ms if self.config.jitter_ms else 0.0
        return (base + jitter) / 1000


def retry_config_from_settings(settings) -> TransactionRetryConfig:
    return TransactionRetryConfig(
        max_attempts=getattr(settings, "postgres_tx_max_attempts", 3),
        initial_backoff_ms=getattr(settings, "postgres_tx_initial_backoff_ms", 50),
        max_backoff_ms=getattr(settings, "postgres_tx_max_backoff_ms", 500),
        jitter_ms=getattr(settings, "postgres_tx_jitter_ms", 30),
        retry_lock_not_available=getattr(settings, "postgres_retry_lock_not_available", False),
    )
