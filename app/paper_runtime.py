from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .data_provider import LocalTradingCalendar
from .db import (
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperLedgerEntryRecord,
    PaperOrderMarketEventRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    RuntimeRecoveryIssueRecord,
    RuntimeRecoveryRunRecord,
    ScheduledTaskRunRecord,
    SessionLocal,
    TaskLeaseRecord,
    engine,
    init_db,
)
from .paper import Clock, PaperAccountStatus, ScheduledTaskType, SystemClock, TestClock
from .paper_monitor import PaperMarketMonitorService, PaperMonitorConfig, PaperSettlementService
from .realtime_quotes import latest_provider_status
from .repositories import SqlAlchemyRepositoryFactory, TaskLeaseRepository
from .risk import decimal_to_str, stable_id, stable_json
from .schema import assert_schema_ready_for_writes, schema_status
from .service import market_data_config_from_settings


logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    SKIPPED = "SKIPPED"
    RECOVERED = "RECOVERED"


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    RETRYABLE = "RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class CurrentSessionState(StrEnum):
    PRE_MARKET = "PRE_MARKET"
    MORNING_SESSION = "MORNING_SESSION"
    MIDDAY_BREAK = "MIDDAY_BREAK"
    AFTERNOON_SESSION = "AFTERNOON_SESSION"
    AFTER_CLOSE = "AFTER_CLOSE"
    NON_TRADING_DAY = "NON_TRADING_DAY"


TRADING_TASKS = {
    ScheduledTaskType.SESSION_START.value,
    ScheduledTaskType.PRE_MARKET_SCAN.value,
    ScheduledTaskType.MARKET_MONITOR.value,
    ScheduledTaskType.MIDDAY_CHECK.value,
    ScheduledTaskType.PRE_CLOSE_CHECK.value,
    ScheduledTaskType.SESSION_CLOSE.value,
    ScheduledTaskType.DAILY_SETTLEMENT.value,
    ScheduledTaskType.DAILY_REPORT.value,
}


@dataclass(frozen=True)
class PaperRuntimeConfig:
    enabled: bool = False
    instance_id: str = "paper-runtime-local"
    manual_confirm_required: bool = True
    poll_seconds: float = 30.0
    lease_seconds: float = 120.0
    heartbeat_seconds: float = 30.0
    max_attempts: int = 3
    recovery_on_startup: bool = True
    notification_worker_enabled: bool = False
    allow_manual_task_trigger: bool = False
    market_monitor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0 or self.lease_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("paper runtime timing values must be positive")
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("paper heartbeat must be shorter than lease")
        if self.max_attempts <= 0:
            raise ValueError("paper max attempts must be positive")
        if not self.instance_id.strip():
            raise ValueError("paper instance_id must not be empty")

    @classmethod
    def from_settings(cls, settings) -> "PaperRuntimeConfig":
        return cls(
            enabled=getattr(settings, "paper_runtime_enabled", False),
            instance_id=getattr(settings, "paper_runtime_instance_id", "paper-runtime-local"),
            manual_confirm_required=getattr(settings, "paper_manual_confirm_required", True),
            poll_seconds=getattr(settings, "paper_scheduler_poll_seconds", 30.0),
            lease_seconds=getattr(settings, "paper_task_lease_seconds", 120.0),
            heartbeat_seconds=getattr(settings, "paper_task_heartbeat_seconds", 30.0),
            max_attempts=getattr(settings, "paper_task_max_attempts", 3),
            recovery_on_startup=getattr(settings, "paper_recovery_on_startup", True),
            notification_worker_enabled=getattr(settings, "paper_notification_worker_enabled", False),
            allow_manual_task_trigger=getattr(settings, "paper_allow_manual_task_trigger", False),
            market_monitor_enabled=getattr(settings, "paper_market_monitor_enabled", False),
        )


class TaskLeaseStore:
    def __init__(self, *, clock: Clock, lease_seconds: float, owner_id: str, repository: TaskLeaseRepository | None = None) -> None:
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.owner_id = owner_id
        self.repository = repository

    def acquire(self, session: Session, lease_key: str) -> bool:
        now = self._now()
        repository = self.repository or SqlAlchemyRepositoryFactory.from_session(session).task_leases()
        return repository.acquire(
            session,
            lease_key,
            owner_id=self.owner_id,
            now=now,
            lease_seconds=self.lease_seconds,
        )

    def heartbeat(self, session: Session, lease_key: str) -> bool:
        now = self._now()
        repository = self.repository or SqlAlchemyRepositoryFactory.from_session(session).task_leases()
        return repository.heartbeat(
            session,
            lease_key,
            owner_id=self.owner_id,
            now=now,
            lease_seconds=self.lease_seconds,
        )

    def release(self, session: Session, lease_key: str) -> None:
        repository = self.repository or SqlAlchemyRepositoryFactory.from_session(session).task_leases()
        repository.release(session, lease_key, owner_id=self.owner_id)

    def active_count(self, session: Session) -> int:
        now = self._now()
        repository = self.repository or SqlAlchemyRepositoryFactory.from_session(session).task_leases()
        return repository.active_count(session, now=now)

    def _now(self) -> datetime:
        return self.clock.now().astimezone(TZ)


class ScheduledTaskStore:
    def __init__(self, *, clock: Clock, lease_store: TaskLeaseStore, max_attempts: int) -> None:
        self.clock = clock
        self.lease_store = lease_store
        self.max_attempts = max_attempts

    def run(
        self,
        session: Session,
        *,
        task_type: str,
        trading_date: date,
        account_id: str | None,
        calendar: LocalTradingCalendar,
        fn: Callable[[], dict] | None = None,
        allow_non_trading: bool = False,
    ) -> ScheduledTaskRunRecord:
        now = self.clock.now().astimezone(TZ)
        task_key = stable_id("paper-task", account_id or "global", task_type, trading_date.isoformat())
        idempotency_key = task_key
        existing = session.scalars(
            select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.idempotency_key == idempotency_key)
        ).first()
        if existing is not None and existing.status in {TaskStatus.SUCCEEDED.value, TaskStatus.SKIPPED.value}:
            return existing
        try:
            is_trading_day = calendar.is_trading_day(trading_date)
        except Exception:
            is_trading_day = False
        if task_type in TRADING_TASKS and not allow_non_trading and not is_trading_day:
            return self._upsert_task(
                session,
                existing=existing,
                task_key=task_key,
                task_type=task_type,
                trading_date=trading_date,
                account_id=account_id,
                now=now,
                status=TaskStatus.SKIPPED.value,
                error_type="NON_TRADING_DAY",
                error_message="non trading day",
            )
        lease_key = f"lease:{task_key}"
        if not self.lease_store.acquire(session, lease_key):
            return self._upsert_task(
                session,
                existing=existing,
                task_key=task_key,
                task_type=task_type,
                trading_date=trading_date,
                account_id=account_id,
                now=now,
                status=TaskStatus.RUNNING.value,
                error_type="LEASE_HELD",
                error_message="another paper runtime owns this lease",
            )
        run = self._upsert_task(
            session,
            existing=existing,
            task_key=task_key,
            task_type=task_type,
            trading_date=trading_date,
            account_id=account_id,
            now=now,
            status=TaskStatus.RUNNING.value,
            lease_owner=self.lease_store.owner_id,
        )
        try:
            if fn is not None:
                fn()
            run.status = TaskStatus.SUCCEEDED.value
            run.completed_at = self.clock.now().astimezone(TZ)
            run.error_type = ""
            run.error_message = ""
            self.lease_store.release(session, lease_key)
            session.flush()
            return run
        except Exception as exc:
            logger.exception("PAPER_TRADING task failed: %s %s", task_type, task_key)
            run.attempt = (run.attempt or 0) + 1
            run.status = (
                TaskStatus.FAILED_FINAL.value
                if run.attempt >= self.max_attempts
                else TaskStatus.FAILED_RETRYABLE.value
            )
            run.error_type = type(exc).__name__
            run.error_message = str(exc)
            run.completed_at = self.clock.now().astimezone(TZ)
            self.lease_store.release(session, lease_key)
            session.flush()
            return run

    def recover_timed_out_running(self, session: Session) -> int:
        now = self.clock.now().astimezone(TZ)
        rows = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.status == TaskStatus.RUNNING.value)).all()
        recovered = 0
        for row in rows:
            if row.started_at and _aware(row.started_at) + timedelta(seconds=self.lease_store.lease_seconds) < now:
                row.status = TaskStatus.RECOVERED.value
                row.completed_at = now
                row.error_type = "RUNNING_TIMEOUT"
                row.error_message = "running task exceeded lease window and requires replay decision"
                recovered += 1
        session.flush()
        return recovered

    def _upsert_task(
        self,
        session: Session,
        *,
        existing: ScheduledTaskRunRecord | None,
        task_key: str,
        task_type: str,
        trading_date: date,
        account_id: str | None,
        now: datetime,
        status: str,
        lease_owner: str | None = None,
        error_type: str = "",
        error_message: str = "",
    ) -> ScheduledTaskRunRecord:
        row = existing
        if row is None:
            row = ScheduledTaskRunRecord(
                task_run_id=stable_id("paper-task-run", task_key),
                task_key=task_key,
                idempotency_key=task_key,
                task_type=task_type,
                account_id=account_id,
                session_date=trading_date.isoformat(),
                trading_date=trading_date.isoformat(),
                scheduled_at=now,
                started_at=now,
                status=status,
                attempt=1,
                lease_owner=lease_owner,
                error_type=error_type,
                error_message=error_message,
            )
            session.add(row)
        else:
            row.status = status
            row.started_at = now
            row.trading_date = trading_date.isoformat()
            row.scheduled_at = row.scheduled_at or now
            row.lease_owner = lease_owner or row.lease_owner
            row.error_type = error_type
            row.error_message = error_message
        if status in {TaskStatus.SKIPPED.value, TaskStatus.RECOVERED.value}:
            row.completed_at = now
        session.flush()
        return row


class RecoveryService:
    def __init__(self, *, clock: Clock) -> None:
        self.clock = clock

    def run(self, session: Session) -> RuntimeRecoveryRunRecord:
        now = self.clock.now().astimezone(TZ)
        sequence = session.query(RuntimeRecoveryRunRecord).count()
        recovery = RuntimeRecoveryRunRecord(
            recovery_run_id=stable_id("paper-recovery", now.isoformat(), str(sequence)),
            started_at=now,
            status="RUNNING",
            summary_json="{}",
        )
        session.add(recovery)
        session.flush()
        issues = 0
        issues += self._recover_running_tasks(session, recovery.recovery_run_id, now)
        for account in session.scalars(select(PaperAccountRecord)).all():
            issues += self._check_account(session, recovery.recovery_run_id, account, now)
        recovery.issue_count = issues
        recovery.status = "SUCCEEDED" if issues == 0 else "ISSUES_FOUND"
        recovery.completed_at = now
        recovery.summary_json = stable_json({"issues": issues})
        session.flush()
        return recovery

    def _recover_running_tasks(self, session: Session, recovery_run_id: str, now: datetime) -> int:
        count = 0
        for row in session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.status == TaskStatus.RUNNING.value)).all():
            row.status = TaskStatus.RECOVERED.value
            row.completed_at = now
            row.error_type = "RECOVERY_ON_STARTUP"
            row.error_message = "runtime restarted while task was RUNNING"
            self._issue(session, recovery_run_id, row.account_id, "RUNNING_TASK_RECOVERED", "WARN", row.task_key, now)
            count += 1
        return count

    def _check_account(self, session: Session, recovery_run_id: str, account: PaperAccountRecord, now: datetime) -> int:
        issues = 0
        active_buy = session.scalars(
            select(PaperOrderRecord).where(
                PaperOrderRecord.account_id == account.account_id,
                PaperOrderRecord.side == "BUY",
                PaperOrderRecord.status.in_(["PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"]),
            )
        ).all()
        active_sell = session.scalars(
            select(PaperOrderRecord).where(
                PaperOrderRecord.account_id == account.account_id,
                PaperOrderRecord.side == "SELL",
                PaperOrderRecord.status.in_(["PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"]),
            )
        ).all()
        if Decimal(account.cash_frozen) > 0 and not active_buy:
            issues += self._pause(session, recovery_run_id, account, "ORPHAN_FROZEN_CASH", now)
        locked_positions = session.scalars(
            select(PaperPositionRecord).where(PaperPositionRecord.account_id == account.account_id, PaperPositionRecord.locked_quantity > 0)
        ).all()
        if locked_positions and not active_sell:
            issues += self._pause(session, recovery_run_id, account, "ORPHAN_FROZEN_POSITION", now)
        initial_entries = session.scalars(
            select(PaperLedgerEntryRecord).where(
                PaperLedgerEntryRecord.account_id == account.account_id,
                PaperLedgerEntryRecord.event_type == "INITIAL_DEPOSIT",
            )
        ).all()
        initial_sum = sum((Decimal(item.amount) for item in initial_entries), Decimal("0"))
        if initial_sum and initial_sum != Decimal(account.initial_cash):
            issues += self._pause(session, recovery_run_id, account, "LEDGER_ACCOUNT_MISMATCH", now)
        return issues

    def _pause(self, session: Session, recovery_run_id: str, account: PaperAccountRecord, issue_type: str, now: datetime) -> int:
        account.status = PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
        self._issue(session, recovery_run_id, account.account_id, issue_type, "ERROR", issue_type, now)
        return 1

    def _issue(self, session: Session, recovery_run_id: str, account_id: str | None, issue_type: str, severity: str, message: str, now: datetime) -> None:
        session.add(
            RuntimeRecoveryIssueRecord(
                issue_id=stable_id("paper-recovery-issue", recovery_run_id, account_id or "global", issue_type, message),
                recovery_run_id=recovery_run_id,
                account_id=account_id,
                issue_type=issue_type,
                severity=severity,
                message=message,
                created_at=now,
            )
        )


class NotificationWorker:
    def __init__(self, *, clock: Clock, sender: Callable[[dict], None], max_attempts: int) -> None:
        self.clock = clock
        self.sender = sender
        self.max_attempts = max_attempts

    def run_once(self, session: Session, *, limit: int = 50) -> int:
        session.flush()
        rows = session.scalars(
            select(NotificationOutboxRecord)
            .where(NotificationOutboxRecord.status.in_([NotificationStatus.PENDING.value, NotificationStatus.RETRYABLE.value, "FAILED"]))
            .order_by(NotificationOutboxRecord.created_at.asc())
            .limit(limit)
        ).all()
        sent = 0
        now = self.clock.now().astimezone(TZ)
        for row in rows:
            payload = json.loads(row.payload_json or "{}")
            payload["environment"] = "PAPER_TRADING"
            try:
                self.sender(payload)
                row.status = NotificationStatus.SENT.value
                row.last_error = ""
                sent += 1
            except Exception as exc:
                row.retry_count += 1
                row.last_error = str(exc)
                row.status = (
                    NotificationStatus.FAILED_FINAL.value
                    if row.retry_count >= self.max_attempts
                    else NotificationStatus.RETRYABLE.value
                )
            row.updated_at = now
        session.flush()
        return sent


class PaperRuntime:
    def __init__(
        self,
        *,
        config: PaperRuntimeConfig,
        calendar: LocalTradingCalendar,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        session_factory=SessionLocal,
        notification_sender: Callable[[dict], None] | None = None,
        monitor_config: PaperMonitorConfig | None = None,
        database_dialect: str = "unknown",
    ) -> None:
        self.config = config
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.sleep = sleep or (lambda _seconds: None)
        self.session_factory = session_factory
        self.lease_store = TaskLeaseStore(clock=self.clock, lease_seconds=config.lease_seconds, owner_id=config.instance_id)
        self.task_store = ScheduledTaskStore(clock=self.clock, lease_store=self.lease_store, max_attempts=config.max_attempts)
        self.recovery = RecoveryService(clock=self.clock)
        self.notification_worker = NotificationWorker(
            clock=self.clock,
            sender=notification_sender or (lambda _payload: None),
            max_attempts=config.max_attempts,
        )
        self.monitor_config = monitor_config or PaperMonitorConfig()
        self.database_dialect = database_dialect

    def startup(self) -> dict:
        logger.info(
            "Starting PAPER_TRADING runtime instance=%s enabled=%s market_data_mode=%s shadow_mode=%s",
            self.config.instance_id,
            self.config.enabled,
            self.monitor_config.market_data_mode,
            self.monitor_config.shadow_mode,
        )
        if not self.config.enabled:
            return {"environment": "PAPER_TRADING", "runtime_enabled": False, "status": "DISABLED"}
        assert_schema_ready_for_writes(engine)
        if self.config.recovery_on_startup:
            with self.session_factory() as session:
                recovery = self.recovery.run(session)
                session.commit()
                return {"environment": "PAPER_TRADING", "runtime_enabled": True, "recovery_status": recovery.status}
        return {"environment": "PAPER_TRADING", "runtime_enabled": True, "recovery_status": "SKIPPED"}

    def run_task(self, *, task_type: str, trading_date: date, account_id: str | None = None, allow_non_trading: bool = False) -> ScheduledTaskRunRecord:
        assert_schema_ready_for_writes(engine)
        with self.session_factory() as session:
            run = self.task_store.run(
                session,
                task_type=task_type,
                trading_date=trading_date,
                account_id=account_id,
                calendar=self.calendar,
                allow_non_trading=allow_non_trading,
                fn=lambda: self._execute_task(session, task_type, trading_date, account_id),
            )
            session.commit()
            return run

    def run_once(self) -> dict:
        now = self.clock.now().astimezone(TZ)
        trading_date = self.calendar.latest_completed_trading_day(now)
        result = self.run_task(task_type=ScheduledTaskType.RECOVERY_CHECK.value, trading_date=trading_date, allow_non_trading=True)
        return {"environment": "PAPER_TRADING", "task_run_id": result.task_run_id, "status": result.status}

    def recover_only(self) -> dict:
        assert_schema_ready_for_writes(engine)
        with self.session_factory() as session:
            recovery = self.recovery.run(session)
            session.commit()
            return {"environment": "PAPER_TRADING", "recovery_status": recovery.status, "issues": recovery.issue_count}

    def status(self) -> dict:
        now = self.clock.now().astimezone(TZ)
        with self.session_factory() as session:
            failed = len(session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.status.in_([TaskStatus.FAILED_FINAL.value, TaskStatus.FAILED_RETRYABLE.value]))).all())
            pending_orders = len(session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.status.in_(["PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"]))).all())
            pending_notifications = len(session.scalars(select(NotificationOutboxRecord).where(NotificationOutboxRecord.status.in_([NotificationStatus.PENDING.value, NotificationStatus.RETRYABLE.value, "FAILED"]))).all())
            paused_accounts = len(session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.status.in_([PaperAccountStatus.PAUSED.value, PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value, PaperAccountStatus.RISK_OFF.value]))).all())
            last = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.status == TaskStatus.SUCCEEDED.value).order_by(ScheduledTaskRunRecord.completed_at.desc()).limit(1)).first()
            active_leases = self.lease_store.active_count(session)
            last_monitor = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_type == ScheduledTaskType.MARKET_MONITOR.value).order_by(ScheduledTaskRunRecord.completed_at.desc()).limit(1)).first()
            monitor_failed = len(session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_type == ScheduledTaskType.MARKET_MONITOR.value, ScheduledTaskRunRecord.status.in_([TaskStatus.FAILED_FINAL.value, TaskStatus.FAILED_RETRYABLE.value]))).all())
            executable_orders = len(session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.status.in_(["PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"]), PaperOrderRecord.remaining_quantity > 0)).all())
            blocked_rows = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.status.like("BLOCKED%"))).all()
            blocked_by_reason: dict[str, int] = {}
            for order in blocked_rows:
                blocked_by_reason[order.status] = blocked_by_reason.get(order.status, 0) + 1
            last_snapshot = session.scalars(select(PaperAccountSnapshotRecord).order_by(PaperAccountSnapshotRecord.created_at.desc()).limit(1)).first()
            settlement_failed_accounts = len(session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value)).all())
            provider_status = latest_provider_status(session)
            last_quote_received_at = None
            last_valid_quote_market_time = None
            stale_symbol_count = 0
            invalid_quote_count = 0
            for status in provider_status.values():
                last_quote_received_at = status.get("last_success_at") or last_quote_received_at
                last_valid_quote_market_time = status.get("last_quote_market_time") or last_valid_quote_market_time
                stale_symbol_count += int(status.get("stale_symbol_count") or 0)
                invalid_quote_count += int(status.get("invalid_quote_count") or 0)
        return {
            "environment": "PAPER_TRADING",
            "runtime_enabled": self.config.enabled,
            "instance_id": self.config.instance_id,
            "current_time": now.isoformat(),
            "timezone": "Asia/Shanghai",
            "is_trading_day": self._safe_is_trading_day(now.date()),
            "current_session_state": current_session_state(now, self.calendar).value,
            "last_successful_task": None if last is None else last.task_key,
            "failed_task_count": failed,
            "active_leases": active_leases,
            "pending_orders": pending_orders,
            "pending_notifications": pending_notifications,
            "paused_accounts": paused_accounts,
            "recovery_status": "UNKNOWN",
            "market_monitor_enabled": self.config.market_monitor_enabled,
            "last_market_monitor_run": None if last_monitor is None else last_monitor.task_key,
            "market_monitor_failed_count": monitor_failed,
            "pending_executable_orders": executable_orders,
            "blocked_orders_by_reason": blocked_by_reason,
            "last_settlement_date": None if last_snapshot is None else (last_snapshot.trading_date or last_snapshot.session_date),
            "settlement_failed_accounts": settlement_failed_accounts,
            "stale_valuation_accounts": 0,
            "market_data_mode": self.monitor_config.market_data_mode,
            "live_market_enabled": self.monitor_config.market_data_mode == "LIVE_PAPER",
            "shadow_mode": self.monitor_config.shadow_mode,
            "provider_status": provider_status,
            "last_quote_received_at": last_quote_received_at,
            "last_valid_quote_market_time": last_valid_quote_market_time,
            "stale_symbol_count": stale_symbol_count,
            "invalid_quote_count": invalid_quote_count,
            "postgres_test_status": "separate-script",
            "database_dialect": self.database_dialect,
        }

    def _execute_task(self, session: Session, task_type: str, trading_date: date, account_id: str | None) -> dict:
        now = self.clock.now().astimezone(TZ)
        state = current_session_state(now, self.calendar)
        if task_type == ScheduledTaskType.MARKET_MONITOR.value and state == CurrentSessionState.MIDDAY_BREAK:
            raise RuntimeError("midday break blocks paper matching")
        if task_type == ScheduledTaskType.MARKET_MONITOR.value:
            if not self.config.market_monitor_enabled:
                return {"market_monitor_enabled": False, "environment": "PAPER_TRADING"}
            return PaperMarketMonitorService(
                session_factory=self.session_factory,
                calendar=self.calendar,
                clock=self.clock,
                config=self.monitor_config,
                instance_id=self.config.instance_id,
            ).run_once(trading_date=trading_date, account_id=account_id)
        if task_type == ScheduledTaskType.NOTIFICATION_DELIVERY.value:
            sent = self.notification_worker.run_once(session)
            return {"sent": sent}
        if task_type == ScheduledTaskType.RECOVERY_CHECK.value:
            recovery = self.recovery.run(session)
            return {"recovery_status": recovery.status}
        if task_type == ScheduledTaskType.DAILY_REPORT.value:
            _daily_report(session, self.clock, trading_date, account_id)
        if task_type == ScheduledTaskType.DAILY_SETTLEMENT.value:
            return PaperSettlementService(
                session_factory=self.session_factory,
                calendar=self.calendar,
                clock=self.clock,
                config=self.monitor_config,
            ).settle(trading_date=trading_date, account_id=account_id)
        return {"task_type": task_type, "trading_date": trading_date.isoformat(), "environment": "PAPER_TRADING"}

    def _safe_is_trading_day(self, value: date) -> bool:
        try:
            return self.calendar.is_trading_day(value)
        except Exception:
            return False


def current_session_state(now: datetime, calendar: LocalTradingCalendar) -> CurrentSessionState:
    now = now.astimezone(TZ)
    try:
        if not calendar.is_trading_day(now.date()):
            return CurrentSessionState.NON_TRADING_DAY
    except Exception:
        return CurrentSessionState.NON_TRADING_DAY
    current = now.time()
    if current < time(9, 30):
        return CurrentSessionState.PRE_MARKET
    if time(9, 30) <= current < time(11, 30):
        return CurrentSessionState.MORNING_SESSION
    if time(11, 30) <= current < time(13, 0):
        return CurrentSessionState.MIDDAY_BREAK
    if time(13, 0) <= current < time(15, 0):
        return CurrentSessionState.AFTERNOON_SESSION
    return CurrentSessionState.AFTER_CLOSE


def runtime_from_settings(settings=None, *, clock: Clock | None = None, session_factory=SessionLocal) -> PaperRuntime:
    settings = settings or get_settings()
    config = PaperRuntimeConfig.from_settings(settings)
    monitor_config = PaperMonitorConfig.from_settings(settings)
    calendar = market_data_config_from_settings(settings).calendar
    if calendar is None:
        raise RuntimeError("paper runtime requires local trading calendar")
    dialect = settings.database_url.split(":", 1)[0]
    return PaperRuntime(config=config, calendar=calendar, clock=clock, session_factory=session_factory, monitor_config=monitor_config, database_dialect=dialect)


def _daily_report(session: Session, clock: Clock, trading_date: date, account_id: str | None) -> None:
    rows = session.scalars(select(PaperAccountRecord)).all()
    now = clock.now().astimezone(TZ)
    for account in rows:
        if account_id and account.account_id != account_id:
            continue
        dedupe = stable_id("paper-daily-report", account.account_id, trading_date.isoformat())
        existing = session.scalars(select(NotificationOutboxRecord).where(NotificationOutboxRecord.dedupe_key == dedupe)).first()
        if existing is not None:
            continue
        payload = {
            "environment": "PAPER_TRADING",
            "account_id": account.account_id,
            "trading_date": trading_date.isoformat(),
            "total_equity": account.total_equity,
            "notice": "模拟交易，不构成实际委托或收益保证",
        }
        session.add(
            NotificationOutboxRecord(
                message_id=stable_id("paper-message", dedupe),
                dedupe_key=dedupe,
                account_id=account.account_id,
                notification_type="DAILY_REPORT",
                payload_json=stable_json(payload),
                status=NotificationStatus.PENDING.value,
                retry_count=0,
                last_error="",
                created_at=now,
                updated_at=now,
            )
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def _json_default(value):
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON type: {type(value)!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PAPER_TRADING runtime. 模拟交易，不连接券商。")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--task", choices=[item.value for item in ScheduledTaskType])
    parser.add_argument("--account-id")
    parser.add_argument("--date")
    parser.add_argument("--test-now")
    args = parser.parse_args(argv)
    settings = get_settings()
    clock = TestClock(datetime.fromisoformat(args.test_now)) if args.test_now else SystemClock(settings.timezone)
    runtime = runtime_from_settings(settings, clock=clock)
    if args.recover_only:
        print(json.dumps(runtime.recover_only(), ensure_ascii=False, sort_keys=True, default=_json_default))
        return 0
    if args.task:
        trading_date = date.fromisoformat(args.date) if args.date else clock.now().astimezone(TZ).date()
        result = runtime.run_task(task_type=args.task, trading_date=trading_date, account_id=args.account_id, allow_non_trading=False)
        print(json.dumps({"environment": "PAPER_TRADING", "task_run_id": result.task_run_id, "status": result.status}, ensure_ascii=False, sort_keys=True, default=_json_default))
        return 0
    if args.once:
        print(json.dumps(runtime.run_once(), ensure_ascii=False, sort_keys=True, default=_json_default))
        return 0
    print(json.dumps(runtime.startup(), ensure_ascii=False, sort_keys=True, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
