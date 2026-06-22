from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.backtest import (
    BacktestEngine,
    BacktestError,
    BacktestOrderStatus,
    CorporateAction,
    CorporateActionType,
    FeeConfig,
    ResultQuality,
)
from app.risk import RiskPolicy
from app.strategy import SignalType, StrategyConfig
from tests.test_backtest import bars, calendar, config, make_signal_func, rules


def action(action_type: str, **overrides) -> CorporateAction:
    values = {
        "action_id": "",
        "symbol": "600519",
        "action_type": action_type,
        "announcement_date": date(2026, 1, 5),
        "record_date": date(2026, 1, 7),
        "ex_date": date(2026, 1, 8),
        "source": "fixture",
        "source_version": "ca-v1",
    }
    values.update(overrides)
    return CorporateAction(**values)


def ca_engine(*, corporate_actions=(), signal_func=None, fee_config=None, cfg=None, market_data=None, instrument_rules=None):
    return BacktestEngine(
        config=cfg or config(),
        calendar=calendar(),
        market_data=market_data or {"600519": bars()},
        instrument_rules=instrument_rules or {"600519": rules()},
        strategy_config=StrategyConfig(),
        risk_policy=RiskPolicy(),
        fee_config=fee_config or FeeConfig(minimum_commission=Decimal("0")),
        corporate_actions=tuple(corporate_actions),
        signal_func=signal_func or make_signal_func(),
    )


def buy_once_signal():
    buy = make_signal_func(SignalType.BUY_WATCH.value)
    hold = make_signal_func(SignalType.HOLD.value)

    def signal_func(**kwargs):
        if kwargs["market_data"].last_date == date(2026, 1, 5):
            return buy(**kwargs)
        return hold(**kwargs)

    return signal_func


def test_corporate_action_identity_and_validation_are_stable():
    first = action(CorporateActionType.CASH_DIVIDEND.value, cash_per_share=Decimal("0.10"))
    second = action(CorporateActionType.CASH_DIVIDEND.value, cash_per_share=Decimal("0.10"))

    assert first.action_id == second.action_id
    assert first.data_checksum == second.data_checksum
    with pytest.raises(ValueError):
        action(CorporateActionType.CASH_DIVIDEND.value, cash_per_share=Decimal("-0.01"))
    with pytest.raises(ValueError):
        action(CorporateActionType.CASH_DIVIDEND.value, announcement_date=date(2026, 1, 8), cash_per_share=Decimal("0.10"))


def test_future_announcement_does_not_reach_strategy():
    seen: dict[date, int] = {}
    signal_func = make_signal_func()

    def wrapped(**kwargs):
        seen[kwargs["market_data"].last_date] = len(kwargs["visible_corporate_actions"])
        return signal_func(**kwargs)

    ca = action(
        CorporateActionType.CASH_DIVIDEND.value,
        announcement_date=date(2026, 1, 8),
        record_date=date(2026, 1, 8),
        ex_date=date(2026, 1, 9),
        payment_date=date(2026, 1, 9),
        cash_per_share=Decimal("0.10"),
    )
    ca_engine(corporate_actions=(ca,), signal_func=wrapped).run()

    assert seen[date(2026, 1, 5)] == 0
    assert seen[date(2026, 1, 7)] == 0
    assert seen[date(2026, 1, 8)] == 1


def test_cash_dividend_entitlement_payment_tax_and_no_duplicate():
    ca = action(
        CorporateActionType.CASH_DIVIDEND.value,
        payment_date=date(2026, 1, 8),
        cash_per_share=Decimal("0.10"),
    )
    run = ca_engine(
        corporate_actions=(ca,),
        fee_config=FeeConfig(minimum_commission=Decimal("0"), dividend_tax_rate=Decimal("0.10")),
        signal_func=buy_once_signal(),
    ).run()

    assert len(run.dividend_entitlements) == 1
    entitlement = run.dividend_entitlements[0]
    assert entitlement.eligible_quantity == 900
    assert entitlement.gross_cash == Decimal("90.00")
    assert entitlement.tax == Decimal("9.00")
    assert entitlement.net_cash == Decimal("81.00")
    assert entitlement.status == "PAID"
    assert run.result.gross_dividend_income == Decimal("90.00")
    assert run.result.dividend_tax == Decimal("9.00")
    assert run.result.net_dividend_income == Decimal("81.00")
    assert sum(1 for event in run.corporate_action_events if event.event_type == "DIVIDEND_CASH_RECEIVED") == 1


def test_record_date_after_sell_still_receives_and_after_buy_does_not():
    dividend = action(
        CorporateActionType.CASH_DIVIDEND.value,
        payment_date=date(2026, 1, 9),
        cash_per_share=Decimal("0.10"),
    )
    paid_after_sell = ca_engine(corporate_actions=(dividend,), signal_func=buy_once_signal()).run()

    assert paid_after_sell.dividend_entitlements[0].status == "PAID"

    def late_buy(**kwargs):
        if kwargs["market_data"].last_date < date(2026, 1, 7):
            return make_signal_func(SignalType.HOLD.value)(**kwargs)
        return make_signal_func(SignalType.BUY_WATCH.value)(**kwargs)

    bought_after_record = ca_engine(corporate_actions=(dividend,), signal_func=late_buy).run()
    assert bought_after_record.dividend_entitlements == []


def test_stock_dividend_lowers_cost_locks_and_releases_new_shares():
    ca = action(
        CorporateActionType.STOCK_DIVIDEND.value,
        stock_ratio=Decimal("0.10"),
        tradable_date=date(2026, 1, 9),
    )
    run = ca_engine(corporate_actions=(ca,), signal_func=buy_once_signal()).run()
    jan8 = run.positions_by_day[date(2026, 1, 8)][0]
    jan9 = run.positions_by_day[date(2026, 1, 9)][0]

    assert jan8.total_quantity == 90
    assert jan8.locked_quantity == 90
    assert jan8.average_cost < Decimal("10.27")
    assert any(event.event_type == "SHARES_RELEASED" for event in run.corporate_action_events)


def test_capitalization_split_and_reverse_split_are_audited():
    capitalization = action(CorporateActionType.CAPITALIZATION.value, capitalization_ratio=Decimal("0.10"))
    split = action(CorporateActionType.SPLIT.value, stock_ratio=Decimal("2"), ex_date=date(2026, 1, 9), record_date=date(2026, 1, 8))
    reverse = action(CorporateActionType.REVERSE_SPLIT.value, stock_ratio=Decimal("2"), ex_date=date(2026, 1, 9), record_date=date(2026, 1, 8), source_version="reverse")

    cap_run = ca_engine(corporate_actions=(capitalization,)).run()
    split_run = ca_engine(corporate_actions=(split,)).run()
    reverse_run = ca_engine(corporate_actions=(reverse,)).run()

    assert cap_run.result.capitalization_events == 1
    assert split_run.result.split_events == 1
    assert reverse_run.result.split_events == 1
    assert any(event.event_type == "COST_BASIS_ADJUSTED" for event in split_run.corporate_action_events)
    assert reverse_run.positions_by_day[date(2026, 1, 9)][0].total_quantity > 0


def test_corporate_action_cancels_pending_order_without_asset_change():
    ca = action(
        CorporateActionType.CASH_DIVIDEND.value,
        record_date=date(2026, 1, 5),
        ex_date=date(2026, 1, 6),
        payment_date=date(2026, 1, 7),
        cash_per_share=Decimal("0.10"),
    )
    run = ca_engine(corporate_actions=(ca,)).run()

    cancelled = [order for order in run.orders if order.status == BacktestOrderStatus.CANCELLED_CORPORATE_ACTION.value]
    assert cancelled
    assert cancelled[0].corporate_action_id == ca.action_id
    assert run.daily_equity[1].total_equity == Decimal("100000.00")


def test_strategy_can_regenerate_after_corporate_action():
    ca = action(
        CorporateActionType.CASH_DIVIDEND.value,
        record_date=date(2026, 1, 5),
        ex_date=date(2026, 1, 6),
        payment_date=date(2026, 1, 7),
        cash_per_share=Decimal("0.10"),
    )
    run = ca_engine(corporate_actions=(ca,)).run()

    buy_orders = [order for order in run.orders if order.side == "BUY"]
    assert len(buy_orders) >= 2
    assert any(order.created_session > ca.ex_date for order in buy_orders)


def test_raw_execution_required_and_quality_is_explicit():
    with pytest.raises(Exception):
        config(execution_price_adjust="qfq")
    run = ca_engine(corporate_actions=()).run()
    assert run.result.result_quality == ResultQuality.REALISTIC_WITH_MODELED_CORPORATE_ACTIONS.value


def test_rights_issue_default_fail_closed_and_incomplete_quality():
    rights = action(
        CorporateActionType.RIGHTS_ISSUE.value,
        rights_ratio=Decimal("0.30"),
        rights_price=Decimal("8"),
    )
    with pytest.raises(BacktestError, match="rights issue"):
        ca_engine(corporate_actions=(rights,)).run()


def test_delisting_fails_closed_when_position_exists():
    delisting = action(
        CorporateActionType.DELISTING.value,
        record_date=date(2026, 1, 7),
        ex_date=date(2026, 1, 7),
    )
    with pytest.raises(BacktestError, match="delisting"):
        ca_engine(corporate_actions=(delisting,)).run()


def test_symbol_change_keeps_position_continuity():
    symbol_change = action(
        CorporateActionType.SYMBOL_CHANGE.value,
        record_date=date(2026, 1, 7),
        ex_date=date(2026, 1, 8),
        new_symbol="600520",
    )
    run = ca_engine(corporate_actions=(symbol_change,)).run()

    assert any(event.event_type == "SYMBOL_CHANGED" for event in run.corporate_action_events)
    assert any(position.symbol == "600520" for position in run.positions_by_day[date(2026, 1, 8)])


def test_corporate_action_results_are_deterministic_and_balanced():
    ca = action(
        CorporateActionType.CASH_DIVIDEND.value,
        payment_date=date(2026, 1, 8),
        cash_per_share=Decimal("0.10"),
    )
    first = ca_engine(corporate_actions=(ca,)).run()
    second = ca_engine(corporate_actions=(ca,)).run()

    assert first.result.to_dict() == second.result.to_dict()
    assert all(item.total_equity == item.cash + item.market_value for item in first.daily_equity)
