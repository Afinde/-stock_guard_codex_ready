from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, inspect

from app.backtest import DailyBar, FeeConfig, InstrumentRules
from app.data_provider import LocalTradingCalendar, MarketDataError
from app.db import Base
from app.paper import (
    NotificationType,
    PaperAccountStatus,
    PaperLedgerEventType,
    PaperOrderStatus,
    PaperTradingConfig,
    PaperTradingError,
    PaperTradingService,
    ScheduledTaskType,
    TestClock as PaperTestClock,
    run_fixed_simulation_flow,
)
from app.risk import ProposedOrder, ProposedOrderStatus, RiskEngine, RiskPolicy, RiskStatus, stable_id
from app.strategy import SignalType
from tests.test_strategy import make_signal


TZ = ZoneInfo("Asia/Shanghai")


def calendar() -> LocalTradingCalendar:
    days = frozenset({date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)})
    return LocalTradingCalendar(
        source="paper-test",
        trading_day_set=days,
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 8),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="paper-test-v1",
    )


def rules() -> InstrumentRules:
    return InstrumentRules("600519", "SSE", "MAIN", 100, Decimal("0.01"), Decimal("0.10"), False, date(2001, 1, 1), None)


def service(*, cash: Decimal = Decimal("100000"), now: datetime | None = None, policy: RiskPolicy | None = None) -> PaperTradingService:
    clock = PaperTestClock(now or datetime(2026, 1, 5, 15, 10, tzinfo=TZ))
    svc = PaperTradingService(
        calendar=calendar(),
        clock=clock,
        fee_config=FeeConfig(minimum_commission=Decimal("0")),
        risk_policy=policy or RiskPolicy(),
        config=PaperTradingConfig(order_expiry_minutes=24 * 60),
    )
    svc.create_account(account_id="paper-1", name="Fixture", initial_cash=cash)
    return svc


def proposal_and_signal(svc: PaperTradingService, *, quantity: int | None = None, signal=None):
    signal = signal or make_signal()
    decision = RiskEngine().evaluate(
        signal=signal,
        account=svc.account_snapshot("paper-1"),
        policy=RiskPolicy(),
        reference_price=Decimal(str(signal.reference_price)),
        stop_price=Decimal(str(signal.stop_loss_price)),
    )
    qty = quantity or decision.approved_quantity
    proposal = ProposedOrder(
        proposal_id=stable_id("proposal", signal.symbol, signal.parameter_version, str(qty)),
        created_at=svc.clock.now(),
        expires_at=svc.clock.now() + timedelta(hours=4),
        symbol=signal.symbol,
        side="BUY",
        quantity=qty,
        reference_price=Decimal(str(signal.reference_price)),
        stop_price=Decimal(str(signal.stop_loss_price)),
        take_profit_1=Decimal(str(signal.take_profit_1_price)),
        take_profit_2=Decimal(str(signal.take_profit_2_price)),
        signal_identity=decision.signal_identity,
        risk_decision_id=decision.decision_id,
        status=ProposedOrderStatus.PROPOSED.value,
    )
    return proposal, signal


def accept_order(svc: PaperTradingService, *, key: str = "idem-1", quantity: int | None = 500):
    proposal, signal = proposal_and_signal(svc, quantity=quantity)
    return svc.accept_proposal(
        account_id="paper-1",
        proposal=proposal,
        signal=signal,
        operator="tester",
        idempotency_key=key,
    )


def fill_buy(svc: PaperTradingService, *, event_id: str = "open-1"):
    svc.clock.set(datetime(2026, 1, 6, 9, 31, tzinfo=TZ))
    return svc.process_market_event(
        account_id="paper-1",
        symbol="600519",
        bar=DailyBar(date(2026, 1, 6), Decimal("10.00"), Decimal("10.20"), Decimal("9.90"), Decimal("10.10"), 10000),
        previous_close=Decimal("10.00"),
        rules=rules(),
        market_data_as_of=date(2026, 1, 6),
        event_id=event_id,
    )


def test_create_paper_account_records_initial_deposit_ledger():
    svc = service()

    account = svc.accounts["paper-1"]
    assert account.status == PaperAccountStatus.ACTIVE.value
    assert account.cash_available == Decimal("100000.00")
    assert svc.ledger[0].event_type == PaperLedgerEventType.INITIAL_DEPOSIT.value


def test_paused_risk_off_and_closed_accounts_block_new_buy():
    for status in [PaperAccountStatus.PAUSED.value, PaperAccountStatus.RISK_OFF.value, PaperAccountStatus.CLOSED.value]:
        svc = service()
        svc.accounts["paper-1"].status = status
        proposal, signal = proposal_and_signal(svc)

        with pytest.raises(PaperTradingError, match="blocks new buys"):
            svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=signal, operator="tester", idempotency_key=status)


def test_proposal_state_machine_allows_review_then_accept_and_blocks_reopen():
    svc = service()
    proposal, signal = proposal_and_signal(svc)

    assert svc.transition_proposal(proposal, to_status=ProposedOrderStatus.REVIEWED.value, operator="tester", reason="read") == "REVIEWED"
    order = svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=signal, operator="tester", idempotency_key="reviewed")

    assert order.status == PaperOrderStatus.PAPER_PENDING.value
    with pytest.raises(PaperTradingError, match="invalid proposal transition"):
        svc.transition_proposal(proposal, to_status=ProposedOrderStatus.CANCELLED.value, operator="tester", reason="late")


def test_expired_data_error_and_risk_off_proposals_cannot_be_accepted():
    svc = service(now=datetime(2026, 1, 5, 16, 10, tzinfo=TZ))
    proposal, signal = proposal_and_signal(svc)
    expired = replace(proposal, expires_at=datetime(2026, 1, 5, 15, 0, tzinfo=TZ))
    with pytest.raises(PaperTradingError, match="expired"):
        svc.accept_proposal(account_id="paper-1", proposal=expired, signal=signal, operator="tester", idempotency_key="expired")

    bad_signal = replace(signal, signal_type=SignalType.DATA_ERROR, action=SignalType.DATA_ERROR)
    with pytest.raises(PaperTradingError, match="DATA_ERROR"):
        svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=bad_signal, operator="tester", idempotency_key="data-error")

    risk_signal = replace(signal, signal_type=SignalType.RISK_OFF, action=SignalType.RISK_OFF)
    with pytest.raises(PaperTradingError, match="RISK_OFF"):
        svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=risk_signal, operator="tester", idempotency_key="risk-off")


def test_accept_proposal_is_idempotent_and_freezes_cash_once():
    svc = service()
    proposal, signal = proposal_and_signal(svc)

    first = svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=signal, operator="tester", idempotency_key="same")
    second = svc.accept_proposal(account_id="paper-1", proposal=proposal, signal=signal, operator="tester", idempotency_key="same")

    assert first.paper_order_id == second.paper_order_id
    assert len(svc.orders) == 1
    assert [entry.event_type for entry in svc.ledger].count(PaperLedgerEventType.CASH_FROZEN.value) == 1


def test_buy_fill_updates_cash_position_and_does_not_duplicate_same_event():
    svc = service()
    order = accept_order(svc, quantity=500)

    first = fill_buy(svc, event_id="same-bar")
    second = fill_buy(svc, event_id="same-bar")

    position = svc.positions[("paper-1", "600519")]
    assert len(first) == 1
    assert second == []
    assert order.status == PaperOrderStatus.FILLED.value
    assert position.total_quantity == 500
    assert position.today_bought_quantity == 500
    assert len(svc.fills) == 1


def test_t1_release_moves_today_bought_to_available():
    svc = service()
    accept_order(svc, quantity=500)
    fill_buy(svc)

    svc.clock.set(datetime(2026, 1, 7, 9, 20, tzinfo=TZ))
    svc.run_task(account_id="paper-1", task_type=ScheduledTaskType.SESSION_START.value, session_date=date(2026, 1, 7))

    position = svc.positions[("paper-1", "600519")]
    assert position.today_bought_quantity == 0
    assert position.available_quantity == 500
    assert PaperLedgerEventType.POSITION_RELEASED.value in [entry.event_type for entry in svc.ledger]


def test_same_day_sell_is_blocked_by_t1():
    svc = service()
    accept_order(svc, quantity=500)
    fill_buy(svc)

    order = svc.create_sell_order(account_id="paper-1", symbol="600519", quantity=500, reason="manual-sell")
    svc.clock.set(datetime(2026, 1, 6, 14, 0, tzinfo=TZ))
    svc.process_market_event(
        account_id="paper-1",
        symbol="600519",
        bar=DailyBar(date(2026, 1, 6), Decimal("10.10"), Decimal("10.20"), Decimal("9.90"), Decimal("10.00"), 10000),
        previous_close=Decimal("10.00"),
        rules=rules(),
        market_data_as_of=date(2026, 1, 6),
        event_id="same-day-sell",
    )

    assert order.status == PaperOrderStatus.BLOCKED_T1.value


def test_stale_market_data_blocks_order_before_strategy_or_fill():
    svc = service()
    accept_order(svc, quantity=500)
    svc.clock.set(datetime(2026, 1, 6, 9, 31, tzinfo=TZ))

    with pytest.raises(MarketDataError, match="stale market data"):
        svc.process_market_event(
            account_id="paper-1",
            symbol="600519",
            bar=DailyBar(date(2026, 1, 6), Decimal("10.00"), Decimal("10.20"), Decimal("9.90"), Decimal("10.10"), 10000),
            previous_close=Decimal("10.00"),
            rules=rules(),
            market_data_as_of=date(2026, 1, 5),
            event_id="stale",
        )

    order = next(iter(svc.orders.values()))
    assert order.status == PaperOrderStatus.BLOCKED_STALE_DATA.value
    assert any(message.notification_type == NotificationType.STALE_DATA.value for message in svc.outbox.values())


def test_non_trading_day_market_event_fails_closed():
    svc = service()
    accept_order(svc, quantity=500)

    with pytest.raises(PaperTradingError, match="not a trading day"):
        svc.process_market_event(
            account_id="paper-1",
            symbol="600519",
            bar=DailyBar(date(2026, 1, 10), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), 10000),
            previous_close=Decimal("10"),
            rules=rules(),
            market_data_as_of=date(2026, 1, 10),
            event_id="weekend",
        )


def test_risk_recheck_blocks_new_buy_and_high_score_cannot_bypass():
    svc = service(policy=RiskPolicy(max_consecutive_losses=1))
    svc.accounts["paper-1"].consecutive_losses = 1
    order = accept_order(svc, quantity=500)

    fill_buy(svc)

    assert order.status == PaperOrderStatus.BLOCKED_RISK.value
    assert svc.accounts["paper-1"].status == PaperAccountStatus.RISK_OFF.value


def test_monitor_creates_take_profit_sell_order_without_immediate_fill():
    svc = service()
    accept_order(svc, quantity=500)
    fill_buy(svc)
    svc.clock.set(datetime(2026, 1, 7, 9, 20, tzinfo=TZ))
    svc.release_t1(account_id="paper-1", session_date=date(2026, 1, 7))

    orders = svc.monitor_positions(account_id="paper-1", prices={"600519": Decimal("11.00")}, session_date=date(2026, 1, 7))

    assert len(orders) == 1
    assert orders[0].side == "SELL"
    assert orders[0].status == PaperOrderStatus.PAPER_PENDING.value
    assert any(message.notification_type == NotificationType.TAKE_PROFIT_TRIGGERED.value for message in svc.outbox.values())


def test_no_duplicate_active_sell_order_for_same_position():
    svc = service()
    accept_order(svc, quantity=500)
    fill_buy(svc)
    svc.clock.set(datetime(2026, 1, 7, 9, 20, tzinfo=TZ))
    svc.release_t1(account_id="paper-1", session_date=date(2026, 1, 7))

    first = svc.monitor_positions(account_id="paper-1", prices={"600519": Decimal("11.00")}, session_date=date(2026, 1, 7))
    second = svc.monitor_positions(account_id="paper-1", prices={"600519": Decimal("11.20")}, session_date=date(2026, 1, 7))

    assert first[0].paper_order_id == second[0].paper_order_id
    assert len([order for order in svc.orders.values() if order.side == "SELL"]) == 1


def test_scheduler_task_is_idempotent_and_retry_is_explicit():
    svc = service()

    first = svc.run_task(account_id="paper-1", task_type=ScheduledTaskType.PRE_MARKET_SCAN.value, session_date=date(2026, 1, 5))
    second = svc.run_task(account_id="paper-1", task_type=ScheduledTaskType.PRE_MARKET_SCAN.value, session_date=date(2026, 1, 5))
    third = svc.run_task(account_id="paper-1", task_type=ScheduledTaskType.PRE_MARKET_SCAN.value, session_date=date(2026, 1, 5), retry=True)

    assert first.task_key == second.task_key == third.task_key
    assert second.attempt == 1
    assert third.attempt == 2


def test_outbox_failure_does_not_rollback_and_is_retryable():
    svc = service()
    accept_order(svc, quantity=500)

    def fail(_message):
        raise RuntimeError("webhook down")

    svc.dispatch_outbox(fail)

    message = next(iter(svc.outbox.values()))
    assert message.status == "FAILED"
    assert message.retry_count == 1
    assert len(svc.orders) == 1


def test_daily_settlement_marks_to_market_and_creates_daily_report():
    svc = service()
    accept_order(svc, quantity=500)
    fill_buy(svc)

    report = svc.daily_settlement(account_id="paper-1", prices={"600519": Decimal("10.80")}, session_date=date(2026, 1, 6))

    assert report["total_equity"]
    assert any(entry.event_type == PaperLedgerEventType.DAILY_MARK_TO_MARKET.value for entry in svc.ledger)
    assert any(message.notification_type == NotificationType.DAILY_REPORT.value for message in svc.outbox.values())


def test_recovery_pauses_account_when_ledger_is_inconsistent():
    svc = service()
    svc.accounts["paper-1"].initial_cash = Decimal("1.00")

    recovered = svc.recover_account(account_id="paper-1")

    assert recovered.status == PaperAccountStatus.PAUSED.value


def test_paper_database_tables_are_created_without_breaking_existing_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    tables = set(inspect(engine).get_table_names())

    assert "paper_accounts" in tables
    assert "paper_orders" in tables
    assert "paper_fills" in tables
    assert "paper_ledger_entries" in tables
    assert "notification_outbox" in tables
    assert "signals" in tables


def test_fixed_offline_paper_flow_completes_buy_release_sell_settlement_report():
    report = run_fixed_simulation_flow()

    assert len(report["fills"]) == 2
    assert any(order["side"] == "BUY" and order["status"] == PaperOrderStatus.FILLED.value for order in report["orders"])
    assert any(order["side"] == "SELL" and order["status"] == PaperOrderStatus.FILLED.value for order in report["orders"])
    assert PaperLedgerEventType.POSITION_RELEASED.value in report["ledger_events"]
    assert NotificationType.DAILY_REPORT.value in report["outbox_types"]
