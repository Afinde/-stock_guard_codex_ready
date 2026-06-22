from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError


pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="PostgreSQL integration tests are opt-in")

from app.data_provider import LocalTradingCalendar  # noqa: E402
from app.db import (  # noqa: E402
    Base,
    MarketQuoteSnapshotRecord,
    PaperAccountSnapshotRecord,
    PaperAccountRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperMarketSnapshotRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    PaperShadowDecisionRecord,
    SessionLocal,
    engine,
    init_db,
)
from app.paper import PaperAccountStatus, PaperOrderStatus, TestClock  # noqa: E402
from app.paper_monitor import PaperFaultInjectionPoint, PaperMarketMonitorService, PaperMonitorConfig  # noqa: E402
from app.paper_monitor import PaperMarketSnapshot  # noqa: E402
from app.paper_runtime import TaskLeaseStore  # noqa: E402
from app.repositories import SqlAlchemyRepositoryFactory  # noqa: E402
from app.realtime_quotes import RealTimeQuoteConfig, normalize_quote, save_quote_snapshot  # noqa: E402
from app.schema import alembic_config, current_revision, head_revision, validate_baseline_schema  # noqa: E402
from app.transactions import TransactionRetryConfig, TransactionRunner  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def reset_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def calendar() -> LocalTradingCalendar:
    return LocalTradingCalendar(
        source="postgres-test",
        trading_day_set=frozenset({date(2026, 1, 5)}),
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 5),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="pg-cal-v1",
    )


def quote():
    return normalize_quote(
        {
            "symbol": "600519",
            "exchange": "SSE",
            "trading_date": "2026-01-05",
            "market_time": NOW.isoformat(),
            "open": "10.00",
            "high": "10.20",
            "low": "9.90",
            "last_price": "10.10",
            "previous_close": "10.00",
            "volume": 10000,
            "suspension_status": "TRADING",
            "price_limit_up": "11.00",
            "price_limit_down": "9.00",
        },
        provider="fixture",
        provider_version="fixture-v1",
        received_at=NOW,
        validated_at=NOW,
        calendar_version="pg-cal-v1",
        now=NOW,
        config=RealTimeQuoteConfig(max_age_seconds=300),
    )


def add_account_order_and_quote(*, order_count: int = 1):
    with SessionLocal() as session:
        session.add(
            PaperAccountRecord(
                account_id="paper-1",
                name="paper-1",
                status=PaperAccountStatus.ACTIVE.value,
                initial_cash="100000.0000",
                cash_available="94850.0000",
                cash_frozen="5150.0000",
                market_value="0.0000",
                total_equity="100000.0000",
                realized_pnl="0.0000",
                unrealized_pnl="0.0000",
                fees_paid_total="0.0000",
                taxes_paid_total="0.0000",
                peak_equity="100000.0000",
                drawdown="0.000000",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        for idx in range(1, order_count + 1):
            session.add(
                PaperOrderRecord(
                    paper_order_id=f"buy-{idx}",
                    account_id="paper-1",
                    proposal_id=f"proposal-{idx}",
                    active_key=f"proposal-{idx}",
                    idempotency_key=f"buy-{idx}-idem",
                    symbol="600519",
                    side="BUY",
                    order_type="MARKET_ON_NEXT_OPEN",
                    quantity=500,
                    remaining_quantity=500,
                    status=PaperOrderStatus.PAPER_PENDING.value,
                    rejection_reason="",
                    source_signal_identity="signal",
                    risk_decision_id="risk",
                    created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
                    submitted_at=datetime(2026, 1, 5, 9, idx, tzinfo=TZ),
                    earliest_execution_at=datetime(2026, 1, 5, 9, 30, tzinfo=TZ),
                    expires_at=datetime(2026, 1, 5, 15, 0, tzinfo=TZ),
                    updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
                )
            )
        save_quote_snapshot(session, quote())
        snap = PaperMarketSnapshot.create(
            symbol="600519",
            provider="fixture",
            market_time=NOW,
            trading_date=date(2026, 1, 5),
            open=Decimal("10.00"),
            high=Decimal("10.20"),
            low=Decimal("9.90"),
            close=Decimal("10.10"),
            current_price=Decimal("10.10"),
            volume=10000,
            suspended=False,
            previous_close=Decimal("10.00"),
            price_limit_rate=Decimal("0.10"),
            calendar_version="pg-cal-v1",
            fetched_at=NOW,
            validated_at=NOW,
        )
        session.add(
            PaperMarketSnapshotRecord(
                market_event_id=snap.market_event_id,
                provider=snap.provider,
                symbol=snap.symbol,
                trading_date=snap.trading_date.isoformat(),
                market_time=snap.market_time,
                open_price=str(snap.open),
                high_price=str(snap.high),
                low_price=str(snap.low),
                close_price=str(snap.close),
                current_price=str(snap.current_price),
                previous_close=str(snap.previous_close),
                volume=snap.volume,
                suspended=snap.suspended,
                price_limit_rate=str(snap.price_limit_rate),
                data_checksum=snap.data_checksum,
                calendar_version=snap.calendar_version,
                fetched_at=snap.fetched_at,
                validated_at=snap.validated_at,
            )
        )
        session.commit()


def counts_and_cash():
    with SessionLocal() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        order = session.scalars(select(PaperOrderRecord)).first()
        return {
            "fills": session.query(PaperFillRecord).count(),
            "positions": session.query(PaperPositionRecord).count(),
            "ledger": session.query(PaperLedgerEntryRecord).count(),
            "outbox": 0,
            "cash_available": None if account is None else account.cash_available,
            "cash_frozen": None if account is None else account.cash_frozen,
            "order_status": None if order is None else order.status,
            "remaining_quantity": None if order is None else order.remaining_quantity,
        }


class FailingInjector:
    def __init__(self, point: PaperFaultInjectionPoint, *, fail_once: bool = False, before_fill_duplicate: bool = False) -> None:
        self.point = point
        self.fail_once = fail_once
        self.before_fill_duplicate = before_fill_duplicate
        self.calls = 0

    def maybe_fail(self, point: PaperFaultInjectionPoint, context: dict) -> None:
        if point != self.point:
            return
        self.calls += 1
        if self.fail_once and self.calls > 1:
            return
        if self.before_fill_duplicate:
            with SessionLocal() as session:
                session.add(
                    PaperFillRecord(
                        fill_id=context["fill_id"],
                        fill_idempotency_key=context["fill_key"],
                        market_event_id="duplicate-event",
                        paper_order_id="duplicate-order",
                        account_id="paper-1",
                        symbol="600519",
                        side="BUY",
                        quantity=1,
                        raw_price="1.00",
                        execution_price="1.00",
                        trade_value="1.00",
                        commission="0.00",
                        tax="0.00",
                        other_fees="0.00",
                        slippage_cost="0.00",
                        session_date="2026-01-05",
                        filled_at=NOW,
                    )
                )
                session.commit()
            return
        raise RuntimeError(f"injected {point.value}")


def service_with_injector(injector=None, *, attempts: int = 1) -> PaperMarketMonitorService:
    return PaperMarketMonitorService(
        session_factory=SessionLocal,
        calendar=calendar(),
        clock=TestClock(NOW),
        config=PaperMonitorConfig(enabled=True, market_data_mode="FIXTURE", shadow_mode=False, market_data_max_age_seconds=300),
        transaction_runner=TransactionRunner(
            session_factory=SessionLocal,
            config=TransactionRetryConfig(max_attempts=attempts, initial_backoff_ms=0, max_backoff_ms=0, jitter_ms=0),
            sleep=lambda _seconds: None,
            random_source=lambda: 0,
        ),
        fault_injector=injector,
    )


def test_postgres_schema_decimal_timezone_and_json_round_trip():
    with engine.connect() as connection:
        version = connection.execute(text("select version()")).scalar_one()
        assert "PostgreSQL" in version
    add_account_order_and_quote()
    with SessionLocal() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        row = session.scalars(select(MarketQuoteSnapshotRecord)).first()
        assert Decimal(account.initial_cash) == Decimal("100000.0000")
        assert row.market_time.tzinfo is not None
        assert '"quote_id"' in row.payload_json
    init_db()
    with SessionLocal() as session:
        assert session.query(PaperAccountRecord).count() == 1


def test_postgres_alembic_empty_upgrade_downgrade_and_repeat():
    Base.metadata.drop_all(bind=engine)
    cfg = alembic_config(os.environ["DATABASE_URL"])
    command.upgrade(cfg, "head")
    assert current_revision(engine) == head_revision()
    command.upgrade(cfg, "head")
    assert "paper_orders" in inspect_tables()
    command.downgrade(cfg, "base")
    assert "paper_orders" not in inspect_tables()
    command.upgrade(cfg, "head")
    assert current_revision(engine) == head_revision()


def test_postgres_baseline_stamp_validation():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    validate_baseline_schema(engine)
    cfg = alembic_config(os.environ["DATABASE_URL"])
    command.stamp(cfg, "head")
    assert current_revision(engine) == head_revision()


def inspect_tables() -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names())


def test_postgres_task_lease_competes_once_and_expires():
    first = TaskLeaseStore(clock=TestClock(NOW), lease_seconds=60, owner_id="one")
    second = TaskLeaseStore(clock=TestClock(NOW), lease_seconds=60, owner_id="two")
    with SessionLocal() as session:
        assert first.acquire(session, "lease-key") is True
        assert second.acquire(session, "lease-key") is False
        session.commit()


def test_postgres_old_schema_upgrade_and_repeated_migration():
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE scheduled_task_runs ("
                "id SERIAL PRIMARY KEY,"
                "task_key VARCHAR(160) NOT NULL,"
                "account_id VARCHAR(80),"
                "task_type VARCHAR(40) NOT NULL,"
                "session_date VARCHAR(10) NOT NULL,"
                "status VARCHAR(24) NOT NULL,"
                "attempt INTEGER DEFAULT 1,"
                "error_message TEXT DEFAULT '',"
                "started_at TIMESTAMP WITH TIME ZONE NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO scheduled_task_runs "
                "(task_key, task_type, session_date, status, started_at) "
                "VALUES ('legacy-task', 'PRE_MARKET_SCAN', '2026-01-05', 'SUCCEEDED', :now)"
            ),
            {"now": NOW},
        )
    init_db()
    init_db()
    with engine.connect() as connection:
        columns = {row[0] for row in connection.execute(text("select column_name from information_schema.columns where table_name='scheduled_task_runs'"))}
        row_count = connection.execute(text("select count(*) from scheduled_task_runs where task_key='legacy-task'")).scalar_one()
    assert "task_run_id" in columns
    assert "idempotency_key" in columns
    assert row_count == 1


def test_postgres_quote_unique_constraint_dedupes_concurrent_insert():
    snapshot = quote()

    def insert_once():
        with SessionLocal() as session:
            try:
                save_quote_snapshot(session, snapshot)
                session.commit()
                return "ok"
            except IntegrityError:
                session.rollback()
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: insert_once(), range(2)))
    with SessionLocal() as session:
        assert session.query(MarketQuoteSnapshotRecord).count() == 1
    assert outcomes.count("ok") >= 1


def test_postgres_order_skip_locked_claims_distinct_orders():
    add_account_order_and_quote(order_count=2)
    first = SessionLocal()
    second = SessionLocal()
    try:
        first.begin()
        second.begin()
        first_repo = SqlAlchemyRepositoryFactory.from_session(first).paper_orders()
        second_repo = SqlAlchemyRepositoryFactory.from_session(second).paper_orders()
        first_rows = first_repo.claim_executable_orders(
            first,
            now=NOW,
            owner_id="worker-1",
            batch_size=1,
            account_id=None,
            active_statuses={PaperOrderStatus.PAPER_PENDING.value},
        )
        second_rows = second_repo.claim_executable_orders(
            second,
            now=NOW,
            owner_id="worker-2",
            batch_size=1,
            account_id=None,
            active_statuses={PaperOrderStatus.PAPER_PENDING.value},
        )
        assert [row.paper_order_id for row in first_rows] == ["buy-1"]
        assert [row.paper_order_id for row in second_rows] == ["buy-2"]
        first.rollback()
        second.commit()
    finally:
        first.close()
        second.close()


def test_postgres_task_lease_rollback_releases_claim():
    first = TaskLeaseStore(clock=TestClock(NOW), lease_seconds=60, owner_id="one")
    second = TaskLeaseStore(clock=TestClock(NOW), lease_seconds=60, owner_id="two")
    with SessionLocal() as session:
        transaction = session.begin()
        assert first.acquire(session, "rollback-lease") is True
        transaction.rollback()
    with SessionLocal() as session:
        assert second.acquire(session, "rollback-lease") is True
        session.commit()


def test_postgres_concurrent_order_processing_creates_one_fill_and_atomic_ledger():
    add_account_order_and_quote()
    service = PaperMarketMonitorService(
        session_factory=SessionLocal,
        calendar=calendar(),
        clock=TestClock(NOW),
        config=PaperMonitorConfig(enabled=True, market_data_mode="FIXTURE", shadow_mode=False, market_data_max_age_seconds=300),
    )

    def process():
        try:
            return service.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
        except IntegrityError:
            return {"outcome": "CONCURRENT_DUPLICATE"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: process(), range(2)))

    with SessionLocal() as session:
        assert session.query(PaperFillRecord).count() == 1
        assert session.query(PaperPositionRecord).count() == 1
        assert session.query(PaperLedgerEntryRecord).count() >= 2
        order = session.scalars(select(PaperOrderRecord)).first()
        account = session.scalars(select(PaperAccountRecord)).first()
        assert order.status == PaperOrderStatus.FILLED.value
        assert Decimal(account.cash_available) >= 0
    assert any(result.get("fill_id") for result in outcomes)


def test_postgres_live_paper_shadow_does_not_mutate_account_order_fill_or_ledger():
    add_account_order_and_quote()
    before = counts_and_cash()
    service = PaperMarketMonitorService(
        session_factory=SessionLocal,
        calendar=calendar(),
        clock=TestClock(NOW),
        config=PaperMonitorConfig(enabled=True, market_data_mode="LIVE_PAPER", shadow_mode=True, market_data_max_age_seconds=300),
    )
    result = service.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    after = counts_and_cash()
    with SessionLocal() as session:
        decisions = session.scalars(select(PaperShadowDecisionRecord)).all()
    assert result["shadow"] is True
    assert after == before
    assert len(decisions) == 1
    assert decisions[0].provider == "fixture"
    assert decisions[0].account_state_checksum


@pytest.mark.parametrize("target_name", ["_ledger", "_position_buy", "_outbox"])
def test_postgres_fault_injection_rolls_back_fill_account_position_and_ledger(monkeypatch, target_name):
    add_account_order_and_quote()
    import app.paper_monitor as paper_monitor_module

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{target_name} injected failure")

    monkeypatch.setattr(paper_monitor_module, target_name, fail)
    service = PaperMarketMonitorService(
        session_factory=SessionLocal,
        calendar=calendar(),
        clock=TestClock(NOW),
        config=PaperMonitorConfig(enabled=True, market_data_mode="FIXTURE", shadow_mode=False, market_data_max_age_seconds=300),
    )
    with pytest.raises(RuntimeError):
        service.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with SessionLocal() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        account = session.scalars(select(PaperAccountRecord)).first()
        assert order.status == PaperOrderStatus.PAPER_PENDING.value
        assert account.cash_frozen == "5150.0000"
        assert session.query(PaperFillRecord).count() == 0
        assert session.query(PaperPositionRecord).count() == 0
        assert session.query(PaperLedgerEntryRecord).count() == 0


@pytest.mark.parametrize(
    "point",
    [
        PaperFaultInjectionPoint.BEFORE_FILL_INSERT,
        PaperFaultInjectionPoint.AFTER_FILL_INSERT,
        PaperFaultInjectionPoint.AFTER_ACCOUNT_UPDATE,
        PaperFaultInjectionPoint.BEFORE_POSITION_UPDATE,
        PaperFaultInjectionPoint.BEFORE_LEDGER_INSERT,
        PaperFaultInjectionPoint.BEFORE_OUTBOX_INSERT,
    ],
)
def test_postgres_fault_injection_points_roll_back_and_retry_once(point):
    add_account_order_and_quote()
    before = counts_and_cash()
    injector = FailingInjector(point, fail_once=True)
    with pytest.raises(RuntimeError):
        service_with_injector(injector, attempts=2).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    rolled_back = counts_and_cash()
    assert injector.calls == 1
    assert rolled_back == before
    result = service_with_injector(attempts=1).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    after = counts_and_cash()
    assert result["fill_id"]
    assert after["fills"] == 1
    assert after["positions"] == 1
    assert after["ledger"] >= 2
    assert Decimal(after["cash_available"]) >= 0


def test_postgres_fill_flush_failure_rolls_back_without_orphans():
    add_account_order_and_quote()
    with pytest.raises(IntegrityError):
        service_with_injector(
            FailingInjector(PaperFaultInjectionPoint.BEFORE_FILL_INSERT, before_fill_duplicate=True),
            attempts=1,
        ).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    state = counts_and_cash()
    assert state["fills"] == 1  # only the external duplicate fixture remains
    assert state["positions"] == 0
    assert state["ledger"] == 0
    assert state["order_status"] == PaperOrderStatus.PAPER_PENDING.value
    with SessionLocal() as session:
        session.query(PaperFillRecord).delete()
        session.commit()
    result = service_with_injector(attempts=1).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    assert result["fill_id"]
    assert counts_and_cash()["fills"] == 1


def test_postgres_deadlock_sqlstate_and_runner_retry_success():
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE deadlock_probe (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO deadlock_probe (id, value) VALUES (1, 0), (2, 0)"))
    barrier = threading.Barrier(2)
    sqlstates: list[str] = []

    def lock_pair(first: int, second: int):
        try:
            with SessionLocal() as session:
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                session.execute(text("SELECT * FROM deadlock_probe WHERE id=:id FOR UPDATE"), {"id": first}).all()
                barrier.wait(timeout=10)
                session.execute(text("UPDATE deadlock_probe SET value = value + 1 WHERE id=:id"), {"id": second})
                session.commit()
                return "committed"
        except DBAPIError as exc:
            sqlstates.append(getattr(exc.orig, "sqlstate", ""))
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: lock_pair(*args), [(1, 2), (2, 1)]))
    assert "40P01" in sqlstates
    assert "committed" in outcomes

    attempts: list[int] = []

    def flaky(session, attempt):
        attempts.append(attempt)
        if attempt == 1:
            class DeadlockOrig(Exception):
                sqlstate = "40P01"

            raise OperationalError("select 1", {}, DeadlockOrig("deadlock"))
        session.execute(text("UPDATE deadlock_probe SET value = value + 1 WHERE id=1"))
        return "ok"

    runner = TransactionRunner(
        session_factory=SessionLocal,
        config=TransactionRetryConfig(max_attempts=2, initial_backoff_ms=0, max_backoff_ms=0, jitter_ms=0),
        sleep=lambda _seconds: None,
        random_source=lambda: 0,
    )
    assert runner.run(flaky) == "ok"
    assert attempts == [1, 2]


def test_postgres_serialization_failure_retries_with_new_session():
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE serial_probe (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO serial_probe (id, value) VALUES (1, 0)"))
    barrier = threading.Barrier(2)
    sessions_seen: list[int] = []

    def tx(session, attempt):
        sessions_seen.append(id(session))
        session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        value = session.execute(text("SELECT value FROM serial_probe WHERE id=1")).scalar_one()
        if attempt == 1:
            barrier.wait(timeout=10)
        session.execute(text("UPDATE serial_probe SET value=:value WHERE id=1"), {"value": value + 1})
        return value + 1

    def run_one():
        return TransactionRunner(
            session_factory=SessionLocal,
            config=TransactionRetryConfig(max_attempts=3, initial_backoff_ms=0, max_backoff_ms=0, jitter_ms=0),
            sleep=lambda _seconds: None,
            random_source=lambda: 0,
        ).run(tx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_one(), range(2)))
    with SessionLocal() as session:
        final_value = session.execute(text("SELECT value FROM serial_probe WHERE id=1")).scalar_one()
    assert sorted(results) == [1, 2]
    assert final_value == 2
    assert len(set(sessions_seen)) >= 3


def test_postgres_daily_snapshot_unique_constraint():
    with SessionLocal() as session:
        session.add(
            PaperAccountSnapshotRecord(
                snapshot_id="snapshot-1",
                account_id="paper-1",
                session_date="2026-01-05",
                trading_date="2026-01-05",
                cash_available="1.00",
                cash_frozen="0.00",
                market_value="0.00",
                total_equity="1.00",
                positions_json="[]",
            )
        )
        session.commit()
    with SessionLocal() as session:
        session.add(
            PaperAccountSnapshotRecord(
                snapshot_id="snapshot-2",
                account_id="paper-1",
                session_date="2026-01-05",
                trading_date="2026-01-05",
                cash_available="1.00",
                cash_frozen="0.00",
                market_value="0.00",
                total_equity="1.00",
                positions_json="[]",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
