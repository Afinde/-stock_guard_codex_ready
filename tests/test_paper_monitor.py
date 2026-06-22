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
    PaperAccountSnapshotRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperOrderMarketEventRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    RiskDecisionRecord,
)
from app.paper import PaperAccountStatus, PaperOrderStatus, TestClock
from app.paper_monitor import (
    PaperMarketMonitorService,
    PaperMarketSnapshot,
    PaperMonitorConfig,
    PaperSettlementService,
    market_event_id,
)
from app.data_provider import LocalTradingCalendar, MarketDataError


TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def Session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def calendar() -> LocalTradingCalendar:
    return LocalTradingCalendar(
        source="monitor-test",
        trading_day_set=frozenset({date(2026, 1, 5), date(2026, 1, 6)}),
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 6),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="monitor-cal-v1",
    )


def clock(value: datetime | None = None) -> TestClock:
    return TestClock(value or datetime(2026, 1, 5, 10, 0, tzinfo=TZ))


def monitor(Session, clk: TestClock | None = None, *, batch_size: int = 50) -> PaperMarketMonitorService:
    return PaperMarketMonitorService(
        session_factory=Session,
        calendar=calendar(),
        clock=clk or clock(),
        config=PaperMonitorConfig(enabled=True, batch_size=batch_size, market_data_max_age_seconds=300),
    )


def settlement(Session, clk: TestClock | None = None) -> PaperSettlementService:
    return PaperSettlementService(
        session_factory=Session,
        calendar=calendar(),
        clock=clk or clock(datetime(2026, 1, 5, 16, 0, tzinfo=TZ)),
        config=PaperMonitorConfig(enabled=True, valuation_adjust="raw"),
    )


def add_account(session, account_id: str = "paper-1", *, cash: str = "94850.00", frozen: str = "5150.00", status: str = PaperAccountStatus.ACTIVE.value):
    row = PaperAccountRecord(
        account_id=account_id,
        name=account_id,
        status=status,
        initial_cash="100000.00",
        cash_available=cash,
        cash_frozen=frozen,
        market_value="0.00",
        total_equity=str(Decimal(cash) + Decimal(frozen)),
        realized_pnl="0.00",
        unrealized_pnl="0.00",
        fees_paid_total="0.00",
        taxes_paid_total="0.00",
        peak_equity="100000.00",
        drawdown="0.000000",
        created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
    )
    session.add(row)
    return row


def add_buy_order(session, account_id: str = "paper-1", *, order_id: str = "buy-1", qty: int = 500, status: str = PaperOrderStatus.PAPER_PENDING.value):
    row = PaperOrderRecord(
        paper_order_id=order_id,
        account_id=account_id,
        proposal_id=order_id + "-proposal",
        active_key=order_id + "-proposal",
        idempotency_key=order_id + "-idem",
        symbol="600519",
        side="BUY",
        order_type="MARKET_ON_NEXT_OPEN",
        quantity=qty,
        remaining_quantity=qty,
        status=status,
        rejection_reason="",
        source_signal_identity="signal",
        risk_decision_id="risk",
        created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        submitted_at=datetime(2026, 1, 5, 9, 1, tzinfo=TZ),
        earliest_execution_at=datetime(2026, 1, 5, 9, 30, tzinfo=TZ),
        expires_at=datetime(2026, 1, 5, 15, 0, tzinfo=TZ),
        updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
    )
    session.add(row)
    return row


def add_sell_order(session, account_id: str = "paper-1", *, order_id: str = "sell-1", qty: int = 500, status: str = PaperOrderStatus.PAPER_PENDING.value):
    row = PaperOrderRecord(
        paper_order_id=order_id,
        account_id=account_id,
        proposal_id=None,
        active_key=None,
        idempotency_key=order_id + "-idem",
        symbol="600519",
        side="SELL",
        order_type="MARKET_ON_NEXT_OPEN",
        quantity=qty,
        remaining_quantity=qty,
        status=status,
        rejection_reason="",
        source_signal_identity="sell",
        risk_decision_id="",
        created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        submitted_at=datetime(2026, 1, 5, 9, 1, tzinfo=TZ),
        earliest_execution_at=datetime(2026, 1, 5, 9, 30, tzinfo=TZ),
        expires_at=datetime(2026, 1, 5, 15, 0, tzinfo=TZ),
        updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
    )
    session.add(row)
    return row


def add_position(session, account_id: str = "paper-1", *, total: int = 500, available: int = 500, locked: int = 0, today: int = 0):
    row = PaperPositionRecord(
        account_id=account_id,
        symbol="600519",
        total_quantity=total,
        available_quantity=available,
        today_bought_quantity=today,
        locked_quantity=locked,
        average_cost="10.00",
        last_price="10.00",
        market_value=str(Decimal("10.00") * Decimal(total)),
        realized_pnl="0.00",
        unrealized_pnl="0.00",
    )
    session.add(row)
    return row


def snapshot(*, market_time: datetime | None = None, volume: int = 10000, suspended: bool = False, open_price: str = "10.00", high: str = "10.20", low: str = "9.90", close: str = "10.10", previous_close: str = "10.00", trading_date: date = date(2026, 1, 5)) -> PaperMarketSnapshot:
    mt = market_time or datetime(2026, 1, 5, 10, 0, tzinfo=TZ)
    return PaperMarketSnapshot.create(
        symbol="600519",
        provider="fixture",
        market_time=mt,
        trading_date=trading_date,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        current_price=Decimal(close),
        volume=volume,
        suspended=suspended,
        previous_close=Decimal(previous_close),
        price_limit_rate=Decimal("0.10"),
        calendar_version="monitor-cal-v1",
        fetched_at=mt,
        validated_at=mt,
    )


def setup_buy(Session, snap: PaperMarketSnapshot | None = None):
    svc = monitor(Session)
    with Session() as session:
        add_account(session)
        add_buy_order(session)
        session.commit()
    svc.save_market_snapshot(snap or snapshot())
    return svc


def test_market_monitor_reads_executable_orders_and_stable_sort(Session):
    svc = monitor(Session)
    with Session() as session:
        add_account(session, "b-account")
        add_account(session, "a-account")
        add_buy_order(session, "b-account", order_id="b-order")
        add_buy_order(session, "a-account", order_id="a-order")
        rows = svc._query_executable_orders(session, datetime(2026, 1, 5, 10, 0, tzinfo=TZ), None)
    assert [row.paper_order_id for row in rows] == ["a-order", "b-order"]


def test_non_trading_or_midday_does_not_match(Session):
    with pytest.raises(RuntimeError):
        monitor(Session, clock(datetime(2026, 1, 5, 12, 0, tzinfo=TZ))).run_once(trading_date=date(2026, 1, 5))
    with pytest.raises(RuntimeError):
        monitor(Session, clock(datetime(2026, 1, 6, 8, 0, tzinfo=TZ))).run_once(trading_date=date(2026, 1, 6))


def test_market_snapshot_checksum_and_event_id_are_stable():
    first = snapshot()
    second = snapshot()
    changed = snapshot(close="10.11")
    assert first.data_checksum == second.data_checksum
    assert first.market_event_id == second.market_event_id
    assert first.data_checksum != changed.data_checksum
    assert market_event_id(first) == first.market_event_id


def test_buy_fill_updates_order_cash_position_ledger_outbox_and_risk(Session):
    svc = setup_buy(Session)
    result = svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        account = session.scalars(select(PaperAccountRecord)).first()
        position = session.scalars(select(PaperPositionRecord)).first()
        fills = session.scalars(select(PaperFillRecord)).all()
    assert result["fill_id"]
    assert order.status == PaperOrderStatus.FILLED.value
    assert account.cash_frozen == "0.00"
    assert position.total_quantity == 500
    assert len(fills) == 1
    with Session() as session:
        assert session.query(PaperLedgerEntryRecord).count() >= 2
        assert session.query(NotificationOutboxRecord).count() == 1
        assert session.query(RiskDecisionRecord).count() == 1


def test_same_order_and_market_event_is_idempotent_after_restart(Session):
    svc = setup_buy(Session)
    first = svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    second = monitor(Session).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperFillRecord).count() == 1
        assert session.query(PaperOrderMarketEventRecord).count() == 1
    assert second["outcome"] in {"TERMINAL", "FILLED"}
    assert first["fill_id"]


def test_future_and_stale_market_data_do_not_fill(Session):
    future = snapshot(market_time=datetime(2026, 1, 5, 10, 1, tzinfo=TZ))
    svc = setup_buy(Session, future)
    with pytest.raises(MarketDataError):
        svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperFillRecord).count() == 0
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    LocalSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    stale_svc = setup_buy(LocalSession, snapshot(market_time=datetime(2026, 1, 5, 9, 0, tzinfo=TZ)))
    with pytest.raises(MarketDataError):
        stale_svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))


def test_risk_off_blocks_buy_without_asset_change(Session):
    svc = monitor(Session)
    with Session() as session:
        add_account(session, status=PaperAccountStatus.RISK_OFF.value)
        add_buy_order(session)
        session.commit()
    svc.save_market_snapshot(snapshot())
    result = svc.run_once(trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperFillRecord).count() == 0
    assert result["fills"] == 0


def test_suspended_price_limit_and_liquidity_block(Session):
    for snap, expected in [
        (snapshot(suspended=True), PaperOrderStatus.BLOCKED_SUSPENSION.value),
        (snapshot(open_price="11.00", high="11.00", low="11.00", close="11.00"), PaperOrderStatus.BLOCKED_PRICE_LIMIT.value),
        (snapshot(volume=500), "BLOCKED_LIQUIDITY"),
    ]:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        LocalSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        svc = setup_buy(LocalSession, snap)
        svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
        with LocalSession() as session:
            assert session.scalars(select(PaperOrderRecord)).first().status == expected
            assert session.query(PaperFillRecord).count() == 0


def test_t1_blocks_sell_then_next_event_can_recheck(Session):
    svc = monitor(Session)
    with Session() as session:
        add_account(session, cash="100000.00", frozen="0.00")
        add_position(session, available=0, today=500)
        add_sell_order(session)
        session.commit()
    svc.save_market_snapshot(snapshot())
    svc.process_order(order_id="sell-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        assert order.status == PaperOrderStatus.BLOCKED_T1.value
        position = session.scalars(select(PaperPositionRecord)).first()
        position.available_quantity = 500
        position.today_bought_quantity = 0
        session.commit()
    later = snapshot(market_time=datetime(2026, 1, 5, 10, 1, tzinfo=TZ), close="10.20")
    svc.save_market_snapshot(later)
    svc.clock.set(datetime(2026, 1, 5, 10, 1, tzinfo=TZ))
    svc.process_order(order_id="sell-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperFillRecord).count() == 1


def test_partial_buy_and_sell_update_remaining_and_freezes(Session):
    svc = setup_buy(Session, snapshot(volume=1500))
    svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        account = session.scalars(select(PaperAccountRecord)).first()
    assert order.status == PaperOrderStatus.PARTIALLY_FILLED.value
    assert order.remaining_quantity == 400
    assert Decimal(account.cash_frozen) > 0

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    LocalSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    sell_svc = monitor(LocalSession)
    with LocalSession() as session:
        add_account(session, cash="100000.00", frozen="0.00")
        add_position(session, total=500, available=0, locked=500)
        add_sell_order(session)
        session.commit()
    sell_svc.save_market_snapshot(snapshot(volume=1500))
    sell_svc.process_order(order_id="sell-1", trading_date=date(2026, 1, 5))
    with LocalSession() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        position = session.scalars(select(PaperPositionRecord)).first()
    assert order.status == PaperOrderStatus.PARTIALLY_FILLED.value
    assert order.remaining_quantity == 400
    assert position.locked_quantity == 400


def test_fill_write_failure_rolls_back_everything(Session, monkeypatch):
    svc = setup_buy(Session)
    import app.paper_monitor as module

    def fail(*_args, **_kwargs):
        raise RuntimeError("ledger failed")

    monkeypatch.setattr(module, "_ledger", fail)
    with pytest.raises(RuntimeError):
        svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperFillRecord).count() == 0
        assert session.scalars(select(PaperOrderRecord)).first().status == PaperOrderStatus.PAPER_PENDING.value


def test_settlement_saves_checksum_calendar_version_and_is_idempotent(Session):
    svc = setup_buy(Session)
    svc.process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    result = settlement(Session).settle(trading_date=date(2026, 1, 5))
    again = settlement(Session).settle(trading_date=date(2026, 1, 5))
    with Session() as session:
        snap = session.scalars(select(PaperAccountSnapshotRecord)).first()
    assert result["settled"][0]["existing"] is False
    assert again["settled"][0]["existing"] is True
    assert snap.calendar_version == "monitor-cal-v1"
    assert snap.valuation_adjust == "raw"
    assert "600519" in snap.market_data_checksums_json


def test_settlement_missing_price_fails_closed_and_no_report(Session):
    with Session() as session:
        add_account(session, cash="95000.00", frozen="0.00")
        add_position(session)
        session.commit()
    with pytest.raises(RuntimeError):
        settlement(Session).settle(trading_date=date(2026, 1, 5))
    with Session() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        assert account.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
        assert session.query(NotificationOutboxRecord).count() == 0


def test_run_fixed_simulation_flow_not_used_by_runtime():
    import app.paper_runtime as runtime_module

    assert "run_fixed_simulation_flow" not in open(runtime_module.__file__, encoding="utf-8").read()
