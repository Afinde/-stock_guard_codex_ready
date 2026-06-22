from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import (
    MarketQuoteSnapshotRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperOrderRecord,
    ScheduledTaskRunRecord,
    TaskLeaseRecord,
)
from .paper import PaperAccountStatus


class TaskLeaseRepository(Protocol):
    def acquire(self, session: Session, lease_key: str, *, owner_id: str, now: datetime, lease_seconds: float) -> bool:
        ...

    def heartbeat(self, session: Session, lease_key: str, *, owner_id: str, now: datetime, lease_seconds: float) -> bool:
        ...

    def release(self, session: Session, lease_key: str, *, owner_id: str) -> None:
        ...

    def active_count(self, session: Session, *, now: datetime) -> int:
        ...


class ScheduledTaskRepository(Protocol):
    def find_by_idempotency_key(self, session: Session, idempotency_key: str) -> ScheduledTaskRunRecord | None:
        ...


class PaperOrderRepository(Protocol):
    def claim_executable_orders(
        self,
        session: Session,
        *,
        now: datetime,
        owner_id: str,
        batch_size: int,
        account_id: str | None,
        active_statuses: set[str],
    ) -> list[PaperOrderRecord]:
        ...


class MarketQuoteRepository(Protocol):
    def latest_valid_quotes(self, session: Session, *, symbol: str, trading_date: str, now: datetime, limit: int) -> list[MarketQuoteSnapshotRecord]:
        ...


class SettlementRepository(Protocol):
    def existing_snapshot(self, session: Session, *, account_id: str, trading_date: str) -> PaperAccountSnapshotRecord | None:
        ...


@dataclass(frozen=True)
class SqlAlchemyRepositoryFactory:
    dialect_name: str

    @classmethod
    def from_session(cls, session: Session) -> "SqlAlchemyRepositoryFactory":
        bind = session.get_bind()
        return cls(dialect_name=bind.dialect.name)

    @property
    def supports_skip_locked(self) -> bool:
        return self.dialect_name == "postgresql"

    def task_leases(self) -> "SqlAlchemyTaskLeaseRepository":
        return SqlAlchemyTaskLeaseRepository(self)

    def scheduled_tasks(self) -> "SqlAlchemyScheduledTaskRepository":
        return SqlAlchemyScheduledTaskRepository()

    def paper_orders(self) -> "SqlAlchemyPaperOrderRepository":
        return SqlAlchemyPaperOrderRepository(self)

    def market_quotes(self) -> "SqlAlchemyMarketQuoteRepository":
        return SqlAlchemyMarketQuoteRepository()

    def settlements(self) -> "SqlAlchemySettlementRepository":
        return SqlAlchemySettlementRepository()


class SqlAlchemyTaskLeaseRepository:
    def __init__(self, factory: SqlAlchemyRepositoryFactory) -> None:
        self.factory = factory

    def acquire(self, session: Session, lease_key: str, *, owner_id: str, now: datetime, lease_seconds: float) -> bool:
        expires_at = now + timedelta(seconds=lease_seconds)
        statement: Select = select(TaskLeaseRecord).where(TaskLeaseRecord.lease_key == lease_key)
        if self.factory.supports_skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        lease = session.scalars(statement).first()
        if lease is None:
            session.add(
                TaskLeaseRecord(
                    lease_key=lease_key,
                    owner_id=owner_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires_at,
                    status="ACTIVE",
                )
            )
            try:
                session.flush()
                return True
            except IntegrityError:
                session.rollback()
                return False
        if lease.status == "ACTIVE" and _aware(lease.expires_at, now) > now and lease.owner_id != owner_id:
            return False
        lease.owner_id = owner_id
        lease.acquired_at = now
        lease.heartbeat_at = now
        lease.expires_at = expires_at
        lease.status = "ACTIVE"
        session.flush()
        return True

    def heartbeat(self, session: Session, lease_key: str, *, owner_id: str, now: datetime, lease_seconds: float) -> bool:
        statement: Select = select(TaskLeaseRecord).where(TaskLeaseRecord.lease_key == lease_key)
        if self.factory.supports_skip_locked:
            statement = statement.with_for_update()
        lease = session.scalars(statement).first()
        if lease is None or lease.owner_id != owner_id or lease.status != "ACTIVE":
            return False
        lease.heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        session.flush()
        return True

    def release(self, session: Session, lease_key: str, *, owner_id: str) -> None:
        statement: Select = select(TaskLeaseRecord).where(TaskLeaseRecord.lease_key == lease_key)
        if self.factory.supports_skip_locked:
            statement = statement.with_for_update()
        lease = session.scalars(statement).first()
        if lease is not None and lease.owner_id == owner_id:
            lease.status = "RELEASED"
            session.flush()

    def active_count(self, session: Session, *, now: datetime) -> int:
        return len(
            session.scalars(
                select(TaskLeaseRecord).where(
                    TaskLeaseRecord.status == "ACTIVE",
                    TaskLeaseRecord.expires_at > now,
                )
            ).all()
        )


class SqlAlchemyScheduledTaskRepository:
    def find_by_idempotency_key(self, session: Session, idempotency_key: str) -> ScheduledTaskRunRecord | None:
        return session.scalars(
            select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.idempotency_key == idempotency_key)
        ).first()


class SqlAlchemyPaperOrderRepository:
    def __init__(self, factory: SqlAlchemyRepositoryFactory) -> None:
        self.factory = factory

    def claim_executable_orders(
        self,
        session: Session,
        *,
        now: datetime,
        owner_id: str,
        batch_size: int,
        account_id: str | None,
        active_statuses: set[str],
    ) -> list[PaperOrderRecord]:
        query: Select = (
            select(PaperOrderRecord)
            .join(PaperAccountRecord, PaperAccountRecord.account_id == PaperOrderRecord.account_id)
            .where(
                PaperOrderRecord.status.in_(list(active_statuses)),
                PaperOrderRecord.remaining_quantity > 0,
                PaperOrderRecord.expires_at > now,
                PaperAccountRecord.status == PaperAccountStatus.ACTIVE.value,
            )
            .order_by(
                PaperOrderRecord.account_id.asc(),
                PaperOrderRecord.submitted_at.asc(),
                PaperOrderRecord.created_at.asc(),
                PaperOrderRecord.paper_order_id.asc(),
            )
            .limit(batch_size)
        )
        if account_id:
            query = query.where(PaperOrderRecord.account_id == account_id)
        if self.factory.supports_skip_locked:
            query = query.with_for_update(of=PaperOrderRecord, skip_locked=True)
        rows = session.scalars(query).all()
        claimed = [row for row in rows if row.earliest_execution_at is None or _aware(row.earliest_execution_at, now) <= now]
        for row in claimed:
            row.processing_owner = owner_id
            row.processing_started_at = now
        session.flush()
        return claimed


class SqlAlchemyMarketQuoteRepository:
    def latest_valid_quotes(self, session: Session, *, symbol: str, trading_date: str, now: datetime, limit: int) -> list[MarketQuoteSnapshotRecord]:
        return session.scalars(
            select(MarketQuoteSnapshotRecord)
            .where(
                MarketQuoteSnapshotRecord.symbol == symbol,
                MarketQuoteSnapshotRecord.trading_date == trading_date,
                MarketQuoteSnapshotRecord.quality_status == "VALID",
                MarketQuoteSnapshotRecord.market_time <= now,
            )
            .order_by(
                MarketQuoteSnapshotRecord.market_time.desc(),
                MarketQuoteSnapshotRecord.provider.asc(),
                MarketQuoteSnapshotRecord.quote_id.asc(),
            )
            .limit(limit)
        ).all()


class SqlAlchemySettlementRepository:
    def existing_snapshot(self, session: Session, *, account_id: str, trading_date: str) -> PaperAccountSnapshotRecord | None:
        return session.scalars(
            select(PaperAccountSnapshotRecord).where(
                PaperAccountSnapshotRecord.account_id == account_id,
                PaperAccountSnapshotRecord.trading_date == trading_date,
            )
        ).first()


def _aware(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value.astimezone(reference.tzinfo)
