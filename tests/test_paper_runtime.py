from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import (
    Base,
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperLedgerEntryRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    RuntimeRecoveryIssueRecord,
    ScheduledTaskRunRecord,
)
from app.paper import PaperAccountStatus, ScheduledTaskType, TestClock
from app.paper_runtime import (
    CurrentSessionState,
    NotificationStatus,
    NotificationWorker,
    PaperRuntime,
    PaperRuntimeConfig,
    RecoveryService,
    ScheduledTaskStore,
    TaskLeaseStore,
    TaskStatus,
    current_session_state,
    main as runtime_main,
)
from app.data_provider import LocalTradingCalendar


TZ = ZoneInfo("Asia/Shanghai")


def calendar() -> LocalTradingCalendar:
    return LocalTradingCalendar(
        source="runtime-test",
        trading_day_set=frozenset({date(2026, 1, 5), date(2026, 1, 6)}),
        start_date=date(2026, 1, 4),
        end_date=date(2026, 1, 6),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="runtime-test-v1",
    )


@pytest.fixture
def Session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def clock(value: datetime | None = None) -> TestClock:
    return TestClock(value or datetime(2026, 1, 5, 9, 0, tzinfo=TZ))


def runtime(Session, clk: TestClock | None = None, *, instance: str = "rt-1") -> PaperRuntime:
    return PaperRuntime(
        config=PaperRuntimeConfig(enabled=True, instance_id=instance, lease_seconds=60, heartbeat_seconds=10, poll_seconds=1),
        calendar=calendar(),
        clock=clk or clock(),
        session_factory=Session,
    )


def add_account(session, *, account_id: str = "paper-1", frozen: str = "0.00"):
    row = PaperAccountRecord(
        account_id=account_id,
        name="Fixture",
        status=PaperAccountStatus.ACTIVE.value,
        initial_cash="100000.00",
        cash_available="100000.00",
        cash_frozen=frozen,
        market_value="0.00",
        total_equity=str(Decimal("100000.00") + Decimal(frozen)),
        realized_pnl="0.00",
        unrealized_pnl="0.00",
        created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
    )
    session.add(row)
    return row


def test_runtime_default_disabled_and_invalid_config_fails():
    assert PaperRuntimeConfig().enabled is False
    with pytest.raises(ValueError, match="heartbeat"):
        PaperRuntimeConfig(lease_seconds=10, heartbeat_seconds=10)


def test_session_state_blocks_midday_matching():
    assert current_session_state(datetime(2026, 1, 5, 12, 0, tzinfo=TZ), calendar()) == CurrentSessionState.MIDDAY_BREAK


def test_non_trading_day_task_is_skipped(Session):
    rt = runtime(Session, clock(datetime(2026, 1, 5, 9, 0, tzinfo=TZ)))
    with Session() as session:
        row = rt.task_store.run(
            session,
            task_type=ScheduledTaskType.PRE_MARKET_SCAN.value,
            trading_date=date(2026, 1, 4),
            account_id="paper-1",
            calendar=calendar(),
        )
        session.commit()
    assert row.status == TaskStatus.SKIPPED.value


def test_midday_market_monitor_fails_retryable_not_success(Session):
    rt = runtime(Session, clock(datetime(2026, 1, 5, 12, 0, tzinfo=TZ)))
    row = rt.run_task(task_type=ScheduledTaskType.MARKET_MONITOR.value, trading_date=date(2026, 1, 5), account_id="paper-1")

    assert row.status == TaskStatus.FAILED_RETRYABLE.value
    assert row.error_message


def test_same_task_same_day_succeeds_once_and_manual_repeat_returns_existing(Session):
    rt = runtime(Session, clock(datetime(2026, 1, 5, 16, 0, tzinfo=TZ)))

    first = rt.run_task(task_type=ScheduledTaskType.PRE_MARKET_SCAN.value, trading_date=date(2026, 1, 5), account_id="paper-1")
    second = rt.run_task(task_type=ScheduledTaskType.PRE_MARKET_SCAN.value, trading_date=date(2026, 1, 5), account_id="paper-1")

    assert first.task_key == second.task_key
    assert first.status == second.status == TaskStatus.SUCCEEDED.value
    with Session() as session:
        assert session.query(ScheduledTaskRunRecord).count() == 1


def test_two_instances_compete_for_lease_only_one_wins(Session):
    clk = clock()
    first = TaskLeaseStore(clock=clk, lease_seconds=60, owner_id="one")
    second = TaskLeaseStore(clock=clk, lease_seconds=60, owner_id="two")

    with Session() as session:
        assert first.acquire(session, "lease-key") is True
        assert second.acquire(session, "lease-key") is False


def test_expired_lease_can_be_taken_over_and_heartbeat_extends(Session):
    clk = clock()
    first = TaskLeaseStore(clock=clk, lease_seconds=60, owner_id="one")
    second = TaskLeaseStore(clock=clk, lease_seconds=60, owner_id="two")

    with Session() as session:
        assert first.acquire(session, "lease-key") is True
        old_expires = session.execute(select(ScheduledTaskRunRecord)).first()
        assert first.heartbeat(session, "lease-key") is True
        clk.advance(timedelta(seconds=61))
        assert second.acquire(session, "lease-key") is True
        lease = second.active_count(session)

    assert old_expires is None
    assert lease == 1


def test_recovery_marks_running_tasks_recovered(Session):
    clk = clock()
    with Session() as session:
        session.add(
            ScheduledTaskRunRecord(
                task_run_id="run-1",
                task_key="key-1",
                idempotency_key="key-1",
                task_type="PRE_MARKET_SCAN",
                account_id="paper-1",
                session_date="2026-01-05",
                trading_date="2026-01-05",
                status=TaskStatus.RUNNING.value,
                attempt=1,
                started_at=clk.now(),
            )
        )
        RecoveryService(clock=clk).run(session)
        session.commit()
        row = session.scalars(select(ScheduledTaskRunRecord)).first()

    assert row.status == TaskStatus.RECOVERED.value


def test_recovery_pauses_orphan_frozen_cash(Session):
    clk = clock()
    with Session() as session:
        add_account(session, frozen="100.00")
        recovery = RecoveryService(clock=clk).run(session)
        session.commit()
        account = session.scalars(select(PaperAccountRecord)).first()
        issues = session.scalars(select(RuntimeRecoveryIssueRecord)).all()

    assert recovery.issue_count == 1
    assert account.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
    assert issues[0].issue_type == "ORPHAN_FROZEN_CASH"


def test_recovery_pauses_orphan_frozen_position(Session):
    clk = clock()
    with Session() as session:
        add_account(session)
        session.add(
            PaperPositionRecord(
                account_id="paper-1",
                symbol="600519",
                total_quantity=100,
                available_quantity=0,
                today_bought_quantity=0,
                locked_quantity=100,
                average_cost="10.00",
            )
        )
        RecoveryService(clock=clk).run(session)
        session.commit()
        account = session.scalars(select(PaperAccountRecord)).first()

    assert account.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value


def test_recovery_pauses_ledger_mismatch(Session):
    clk = clock()
    with Session() as session:
        add_account(session)
        session.add(
            PaperLedgerEntryRecord(
                entry_id="ledger-1",
                account_id="paper-1",
                event_type="INITIAL_DEPOSIT",
                amount="1.00",
                cash_available_after="1.00",
                cash_frozen_after="0.00",
                payload_json="{}",
                occurred_at=clk.now(),
            )
        )
        RecoveryService(clock=clk).run(session)
        session.commit()
        account = session.scalars(select(PaperAccountRecord)).first()

    assert account.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value


def test_notification_worker_retries_and_final_failure(Session):
    clk = clock()
    with Session() as session:
        session.add(
            NotificationOutboxRecord(
                message_id="msg-1",
                dedupe_key="dedupe-1",
                account_id="paper-1",
                notification_type="DAILY_REPORT",
                payload_json='{"text":"模拟交易"}',
                status=NotificationStatus.PENDING.value,
                retry_count=0,
                created_at=clk.now(),
                updated_at=clk.now(),
            )
        )
        worker = NotificationWorker(clock=clk, sender=lambda _payload: (_ for _ in ()).throw(RuntimeError("down")), max_attempts=2)
        worker.run_once(session)
        worker.run_once(session)
        session.commit()
        row = session.scalars(select(NotificationOutboxRecord)).first()

    assert row.status == NotificationStatus.FAILED_FINAL.value
    assert row.retry_count == 2


def test_notification_worker_restart_sends_pending(Session):
    clk = clock()
    sent = []
    with Session() as session:
        session.add(
            NotificationOutboxRecord(
                message_id="msg-1",
                dedupe_key="dedupe-1",
                account_id="paper-1",
                notification_type="DAILY_REPORT",
                payload_json='{"text":"模拟交易"}',
                status=NotificationStatus.PENDING.value,
                retry_count=0,
                created_at=clk.now(),
                updated_at=clk.now(),
            )
        )
        NotificationWorker(clock=clk, sender=lambda payload: sent.append(payload), max_attempts=2).run_once(session)
        session.commit()
        row = session.scalars(select(NotificationOutboxRecord)).first()

    assert row.status == NotificationStatus.SENT.value
    assert sent[0]["environment"] == "PAPER_TRADING"


def test_runtime_status_is_read_only(Session):
    rt = runtime(Session)
    first = rt.status()
    second = rt.status()

    assert first["environment"] == "PAPER_TRADING"
    assert second["pending_orders"] == first["pending_orders"]
    with Session() as session:
        assert session.query(ScheduledTaskRunRecord).count() == 0


def test_cli_once_and_recover_only_exit(monkeypatch, Session, capsys):
    rt = runtime(Session, clock(datetime(2026, 1, 6, 16, 0, tzinfo=TZ)))
    monkeypatch.setattr("app.paper_runtime.runtime_from_settings", lambda *_args, **_kwargs: rt)
    monkeypatch.setattr("app.paper_runtime.init_db", lambda: None)
    monkeypatch.setattr("app.paper_runtime.get_settings", lambda: type("S", (), {"timezone": "Asia/Shanghai"})())

    assert runtime_main(["--once"]) == 0
    assert "PAPER_TRADING" in capsys.readouterr().out
    assert runtime_main(["--recover-only"]) == 0
    assert "PAPER_TRADING" in capsys.readouterr().out


def test_no_broker_or_live_adapter_symbols_in_runtime_module():
    import app.paper_runtime as module

    assert not hasattr(module, "BrokerAdapter")
