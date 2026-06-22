from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import risk_service
from app.db import Base, ProposedOrderRecord, RiskDecisionRecord
from app.risk import (
    AccountSnapshot,
    PositionSnapshot,
    ProposedOrderStatus,
    RiskEngine,
    RiskPolicy,
    RiskStatus,
    create_order_proposal,
)
from app.strategy import SignalType
from app.strategy import StrategyConfig
from tests.test_strategy import make_signal


TZ = ZoneInfo("Asia/Shanghai")


def account(**overrides) -> AccountSnapshot:
    values = {
        "account_id": "acct-fixture",
        "as_of": datetime(2026, 6, 18, 16, 0, tzinfo=TZ),
        "total_equity": Decimal("100000"),
        "available_cash": Decimal("20000"),
        "market_value": Decimal("0"),
        "frozen_cash": Decimal("0"),
        "daily_realized_pnl": Decimal("0"),
        "daily_unrealized_pnl": Decimal("0"),
        "peak_equity": Decimal("100000"),
        "consecutive_losses": 0,
        "positions": tuple(),
    }
    values.update(overrides)
    return AccountSnapshot(**values)


def position(
    symbol: str = "600519",
    market_value: Decimal = Decimal("5000"),
    industry: str | None = "consumer",
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        quantity=100,
        available_quantity=100,
        average_cost=Decimal("50"),
        current_price=Decimal("50"),
        market_value=market_value,
        industry=industry,
    )


def decision(
    *,
    acct: AccountSnapshot | None = None,
    policy: RiskPolicy | None = None,
    signal=None,
    reference_price: Decimal = Decimal("20"),
    stop_price: Decimal = Decimal("19"),
    industry: str | None = "consumer",
):
    engine = RiskEngine()
    return engine.evaluate(
        signal=signal or make_signal(),
        account=acct or account(),
        policy=policy or RiskPolicy(),
        reference_price=reference_price,
        stop_price=stop_price,
        industry=industry,
    )


@pytest.fixture
def memory_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(risk_service, "SessionLocal", Session)
    return Session


def test_risk_policy_default_config_valid():
    policy = RiskPolicy()

    assert policy.risk_per_trade == Decimal("0.005")
    assert policy.lot_size == 100
    assert policy.version


def test_invalid_risk_ratio_config_fails():
    with pytest.raises(ValueError, match="between 0 and 1"):
        RiskPolicy(max_symbol_weight=Decimal("1.5"))
    with pytest.raises(ValueError, match="less than"):
        RiskPolicy(risk_per_trade=Decimal("0.20"))


def test_invalid_drawdown_relationship_fails():
    with pytest.raises(ValueError, match="greater than"):
        RiskPolicy(reduce_risk_drawdown=Decimal("0.12"), risk_off_drawdown=Decimal("0.12"))


def test_invalid_lot_size_fails():
    with pytest.raises(ValueError, match="positive integer"):
        RiskPolicy(lot_size=0)


def test_account_snapshot_negative_money_fails():
    with pytest.raises(ValueError, match="must not be negative"):
        account(available_cash=Decimal("-1"))


def test_account_snapshot_naive_time_fails():
    with pytest.raises(ValueError, match="timezone"):
        account(as_of=datetime(2026, 6, 18, 16, 0))


def test_single_trade_risk_budget_calculation_correct():
    result = decision()

    assert result.status == RiskStatus.APPROVED
    assert result.approved_quantity == 500
    assert result.risk_amount == Decimal("500.00")


def test_quantity_is_rounded_down_to_lot():
    result = decision(policy=RiskPolicy(lot_size=200))

    assert result.approved_quantity == 400


def test_does_not_exceed_available_cash():
    result = decision(acct=account(available_cash=Decimal("2100")))

    assert result.approved_quantity == 100
    assert result.approved_notional == Decimal("2000.00")


def test_does_not_exceed_symbol_weight():
    acct = account(positions=(position(market_value=Decimal("14600")),), market_value=Decimal("14600"))

    result = decision(acct=acct)

    assert result.approved_quantity == 0
    assert result.status == RiskStatus.REJECTED


def test_does_not_exceed_portfolio_weight():
    acct = account(market_value=Decimal("59500"), positions=(position("000001", Decimal("59500"), "bank"),))

    result = decision(acct=acct)

    assert result.approved_quantity == 0
    assert result.status == RiskStatus.REJECTED


def test_existing_same_symbol_position_counts_toward_symbol_exposure():
    acct = account(positions=(position(market_value=Decimal("10000")),), market_value=Decimal("10000"))

    result = decision(acct=acct)

    assert result.approved_quantity == 200
    assert result.symbol_weight_after == Decimal("0.140000")


def test_existing_industry_position_counts_toward_industry_exposure():
    acct = account(positions=(position("000001", Decimal("24500"), "consumer"),), market_value=Decimal("24500"))

    result = decision(acct=acct)

    assert result.approved_quantity == 0
    assert result.status == RiskStatus.REJECTED


def test_industry_empty_rule_is_explicitly_not_executed():
    result = decision(industry=None)

    industry_rule = [rule for rule in result.rules if rule.rule_name == "industry_weight"][0]
    assert industry_rule.passed is True
    assert "未执行" in industry_rule.reason
    assert result.industry_weight_after is None


def test_rejects_when_approved_quantity_below_one_lot():
    result = decision(acct=account(available_cash=Decimal("1000")))

    assert result.status == RiskStatus.REJECTED
    assert "批准数量不足一个交易单位" in result.rejection_reasons


def test_invalid_when_stop_not_below_reference_price():
    result = decision(stop_price=Decimal("20"))

    assert result.status == RiskStatus.INVALID_INPUT


def test_invalid_when_peak_equity_zero():
    result = decision(acct=account(peak_equity=Decimal("0")))

    assert result.status == RiskStatus.INVALID_INPUT


def test_daily_loss_limit_triggers_risk_off():
    result = decision(acct=account(daily_realized_pnl=Decimal("-2000")))

    assert result.status == RiskStatus.RISK_OFF


def test_consecutive_losses_trigger_risk_off():
    result = decision(acct=account(consecutive_losses=3))

    assert result.status == RiskStatus.RISK_OFF


def test_drawdown_reduce_risk_lowers_portfolio_limit():
    result = decision(acct=account(total_equity=Decimal("92000"), peak_equity=Decimal("100000")))

    assert result.status == RiskStatus.REDUCED
    assert any(rule.rule_name == "effective_portfolio_limit" and "降风险" in rule.reason for rule in result.rules)


def test_drawdown_risk_off_blocks_new_buy():
    result = decision(acct=account(total_equity=Decimal("88000"), peak_equity=Decimal("100000")))

    assert result.status == RiskStatus.RISK_OFF


def test_high_score_signal_cannot_bypass_risk_off():
    high_score_signal = make_signal()
    result = decision(signal=high_score_signal, acct=account(consecutive_losses=3))

    assert high_score_signal.score >= 90
    assert result.status == RiskStatus.RISK_OFF


def test_data_error_signal_cannot_generate_order_proposal():
    signal = replace(make_signal(), signal_type=SignalType.DATA_ERROR, action=SignalType.DATA_ERROR)
    result = decision(signal=signal)

    assert result.status == RiskStatus.INVALID_INPUT
    assert create_order_proposal(signal=signal, decision=result) is None


def test_non_buy_signal_cannot_generate_buy_proposal():
    signal = replace(make_signal(), signal_type=SignalType.HOLD, action=SignalType.HOLD)
    result = decision(signal=signal)

    assert result.status == RiskStatus.REJECTED
    assert create_order_proposal(signal=signal, decision=result) is None


def test_approved_generates_proposed_order():
    signal = make_signal()
    result = decision(signal=signal)

    order = create_order_proposal(signal=signal, decision=result, now=datetime(2026, 6, 18, 16, 5, tzinfo=TZ))

    assert order is not None
    assert order.status == ProposedOrderStatus.PROPOSED
    assert order.quantity == result.approved_quantity
    assert order.side == "BUY"


def test_reduced_uses_approved_quantity_for_proposal():
    signal = make_signal()
    result = decision(signal=signal, acct=account(total_equity=Decimal("92000"), peak_equity=Decimal("100000")))

    order = create_order_proposal(signal=signal, decision=result, now=datetime(2026, 6, 18, 16, 5, tzinfo=TZ))

    assert result.status == RiskStatus.REDUCED
    assert order is not None
    assert order.quantity == result.approved_quantity


def test_rejected_does_not_generate_proposal():
    signal = make_signal()
    result = decision(signal=signal, acct=account(available_cash=Decimal("1000")))

    assert create_order_proposal(signal=signal, decision=result) is None


def test_same_input_repeated_proposal_has_same_id():
    signal = make_signal()
    result = decision(signal=signal)
    now = datetime(2026, 6, 18, 16, 5, tzinfo=TZ)

    first = create_order_proposal(signal=signal, decision=result, now=now)
    second = create_order_proposal(signal=signal, decision=result, now=now)

    assert first is not None and second is not None
    assert first.proposal_id == second.proposal_id


def test_repeated_saved_order_proposal_is_idempotent(memory_session):
    signal = make_signal()
    result = decision(signal=signal)
    order = create_order_proposal(
        signal=signal,
        decision=result,
        now=datetime(2026, 6, 18, 16, 5, tzinfo=TZ),
    )
    assert order is not None

    risk_service.save_order_proposal(order)
    risk_service.save_order_proposal(order)

    with memory_session() as session:
        assert session.query(ProposedOrderRecord).count() == 1


def test_repeated_saved_risk_decision_is_idempotent(memory_session):
    acct = account()
    policy = RiskPolicy()
    result = decision(acct=acct, policy=policy)

    risk_service.save_risk_decision(result, acct, policy)
    risk_service.save_risk_decision(result, acct, policy)

    with memory_session() as session:
        assert session.query(RiskDecisionRecord).count() == 1


def test_different_parameter_version_signal_has_different_identity():
    first = make_signal()
    second = make_signal(config=StrategyConfig(buy_watch_threshold=90))

    assert first.parameter_version != second.parameter_version
    assert decision(signal=first).signal_identity != decision(signal=second).signal_identity


def test_unexpected_exception_is_not_converted_to_rejected():
    engine = RiskEngine()

    with pytest.raises(AttributeError):
        engine.evaluate(
            signal=make_signal(),
            account=object(),
            policy=RiskPolicy(),
            reference_price=Decimal("20"),
            stop_price=Decimal("19"),
            industry="consumer",
        )
