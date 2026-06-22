from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import backtest
from app.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestError,
    BacktestOrder,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestSide,
    FeeConfig,
    InstrumentRules,
    MatchingEngine,
    Portfolio,
    SlippageConfig,
    export_backtest_csv,
    export_backtest_json,
)
from app.data_provider import LocalTradingCalendar, MarketDataSnapshot, market_data_checksum
from app.db import BacktestRunRecord, Base
from app.risk import RiskEngine, RiskPolicy, RiskStatus
from app.strategy import Signal, SignalType, StrategyConfig


TZ = ZoneInfo("Asia/Shanghai")


def calendar() -> LocalTradingCalendar:
    days = [date(2026, 1, day) for day in range(5, 10)]
    return LocalTradingCalendar(
        source="fixture",
        trading_day_set=frozenset(days),
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 9),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="cal-v1",
    )


def rules(symbol: str = "600519", *, price_limit: str = "0.10", lot_size: int = 100) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        exchange="SSE",
        board="MAIN",
        lot_size=lot_size,
        price_tick=Decimal("0.01"),
        price_limit_rule=Decimal(price_limit),
        is_st=False,
        listing_date=date(2001, 1, 1),
        delisting_date=None,
        metadata_version=f"rules-{price_limit}-{lot_size}",
    )


def bars(overrides: dict[int, dict] | None = None) -> pd.DataFrame:
    rows = [
        {"date": "2026-01-02", "open": "10.00", "high": "10.20", "low": "9.80", "close": "10.00", "volume": 10000},
        {"date": "2026-01-05", "open": "10.00", "high": "10.30", "low": "9.90", "close": "10.20", "volume": 10000},
        {"date": "2026-01-06", "open": "10.25", "high": "10.50", "low": "10.10", "close": "10.40", "volume": 10000},
        {"date": "2026-01-07", "open": "10.35", "high": "10.60", "low": "9.90", "close": "10.10", "volume": 10000},
        {"date": "2026-01-08", "open": "10.20", "high": "10.90", "low": "10.00", "close": "10.80", "volume": 10000},
        {"date": "2026-01-09", "open": "10.90", "high": "11.00", "low": "10.50", "close": "10.70", "volume": 10000},
    ]
    for index, values in (overrides or {}).items():
        rows[index].update(values)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def config(**overrides) -> BacktestConfig:
    values = {
        "start_date": date(2026, 1, 5),
        "end_date": date(2026, 1, 9),
        "initial_cash": Decimal("100000"),
        "symbols": ("600519",),
        "strategy_name": "fixture_strategy",
        "strategy_version": "1.0.0",
        "parameter_version": "fixture-params",
        "execution_price_adjust": "",
        "signal_price_adjust": "qfq",
        "volume_participation_rate": Decimal("0.10"),
    }
    values.update(overrides)
    return BacktestConfig(**values)


def make_signal_func(action: str = SignalType.BUY_WATCH.value, quantity_hint: int = 500):
    calls: list[date] = []

    def signal_func(**kwargs):
        snapshot: MarketDataSnapshot = kwargs["market_data"]
        calls.append(snapshot.last_date)
        return Signal(
            symbol=kwargs["symbol"],
            action=action,
            score=100 if action == SignalType.BUY_WATCH.value else 0,
            price=float(snapshot.bars.iloc[-1].close),
            stop_price=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("0.95")),
            take_profit_1=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("1.05")),
            take_profit_2=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("1.08")),
            suggested_shares=quantity_hint,
            reason="fixture",
            market_trade_date=snapshot.last_date,
            market_fetched_at=snapshot.fetched_at,
            signal_generated_at=snapshot.validated_at,
            strategy_name="fixture_strategy",
            strategy_version="1.0.0",
            parameter_version="fixture-params",
            parameter_snapshot="{}",
            market_as_of_date=snapshot.last_date,
            market_data_source=snapshot.provider,
            market_data_adjust=snapshot.adjust,
            signal_type=action,
            score_breakdown={},
            reasons=["fixture"],
            invalidation_conditions=["fixture"],
            reference_price=float(snapshot.bars.iloc[-1].close),
            stop_loss_price=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("0.95")),
            take_profit_1_price=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("1.05")),
            take_profit_2_price=float(Decimal(str(snapshot.bars.iloc[-1].close)) * Decimal("1.08")),
            market_data_checksum=snapshot.data_checksum,
            market_calendar_version=snapshot.calendar_version,
        )

    signal_func.calls = calls
    return signal_func


def engine(**kwargs) -> BacktestEngine:
    signal_func = kwargs.pop("signal_func", make_signal_func())
    return BacktestEngine(
        config=kwargs.pop("config", config()),
        calendar=kwargs.pop("calendar", calendar()),
        market_data=kwargs.pop("market_data", {"600519": bars()}),
        instrument_rules=kwargs.pop("instrument_rules", {"600519": rules()}),
        strategy_config=StrategyConfig(),
        risk_policy=kwargs.pop("risk_policy", RiskPolicy()),
        fee_config=kwargs.pop("fee_config", FeeConfig(minimum_commission=Decimal("0"))),
        slippage_config=kwargs.pop("slippage_config", SlippageConfig(buy_slippage_bps=Decimal("10"), sell_slippage_bps=Decimal("10"))),
        signal_func=signal_func,
        risk_engine=kwargs.pop("risk_engine", RiskEngine()),
        persist=kwargs.pop("persist", False),
    )


def test_t_day_signal_only_executes_on_t_plus_1_open():
    run = engine().run()

    buy_order = [order for order in run.orders if order.side == BacktestSide.BUY.value][0]
    first_fill = run.fills[0]

    assert buy_order.created_session == date(2026, 1, 5)
    assert buy_order.earliest_execution_session == date(2026, 1, 6)
    assert first_fill.session_date == date(2026, 1, 6)


def test_strategy_never_sees_future_bars():
    signal_func = make_signal_func()
    engine(signal_func=signal_func).run()

    assert signal_func.calls == sorted(signal_func.calls)
    assert max(signal_func.calls) <= date(2026, 1, 9)


def test_same_config_and_data_are_deterministic():
    first = engine().run().result.to_dict()
    second = engine().run().result.to_dict()

    assert first == second


def test_different_data_checksum_changes_run_identity():
    first = engine().run()
    changed = bars({2: {"close": "10.41", "high": "10.51"}})
    second = engine(market_data={"600519": changed}).run()

    assert first.result.data_checksums["600519"] != second.result.data_checksums["600519"]
    assert first.run_id != second.run_id


def test_calendar_coverage_shortage_fails_closed():
    short_calendar = LocalTradingCalendar(
        source="fixture",
        trading_day_set=frozenset({date(2026, 1, 5)}),
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 5),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
    )

    with pytest.raises(Exception, match="coverage"):
        engine(calendar=short_calendar).run()


def test_adjusted_execution_price_is_rejected():
    with pytest.raises(Exception):
        config(execution_price_adjust="qfq")


def test_next_open_buy_adds_slippage_and_sell_subtracts_slippage():
    run = engine().run()
    buy_fill = [fill for fill in run.fills if fill.side == BacktestSide.BUY.value][0]
    sell_order = BacktestOrder(
        backtest_order_id="sell",
        run_id="run",
        symbol="600519",
        side=BacktestSide.SELL.value,
        order_type=BacktestOrderType.MARKET_ON_NEXT_OPEN.value,
        quantity=100,
        remaining_quantity=100,
        limit_price=None,
        created_session=date(2026, 1, 5),
        earliest_execution_session=date(2026, 1, 6),
        expiry_session=date(2026, 1, 6),
    )
    portfolio = Portfolio(cash_available=Decimal("0"), positions={"600519": backtest.BacktestPosition(symbol="600519", total_quantity=100, available_quantity=100, average_cost=Decimal("10"), last_price=Decimal("10"))})
    fill = engine().matching.execute(
        order=sell_order,
        bar=backtest.DailyBar(date(2026, 1, 6), Decimal("10.25"), Decimal("10.50"), Decimal("10.10"), Decimal("10.40"), 10000),
        previous_close=Decimal("10"),
        rules=rules(),
        portfolio=portfolio,
        run_id="run",
    )

    assert buy_fill.raw_price == Decimal("10.25")
    assert buy_fill.execution_price == Decimal("10.27")
    assert fill is not None
    assert fill.execution_price == Decimal("10.23")


def test_tick_and_lot_rounding_and_small_order_rejection():
    r = rules(lot_size=200)
    assert backtest.legal_price(Decimal("10.251"), r, side=BacktestSide.BUY.value) == Decimal("10.26")
    assert backtest.floor_to_lot(399, r.lot_size) == 200
    assert backtest.floor_to_lot(199, r.lot_size) == 0


def test_t1_blocks_same_day_stop_and_releases_next_session():
    run = engine(market_data={"600519": bars({2: {"low": "9.50"}})}).run()

    blocked = [order for order in run.orders if order.status == BacktestOrderStatus.BLOCKED_T1.value]
    assert blocked
    assert run.positions_by_day[date(2026, 1, 6)][0].today_bought_quantity > 0
    assert run.positions_by_day[date(2026, 1, 7)][0].available_quantity > 0


def test_suspension_and_price_limits_block_fills():
    suspended_run = engine(market_data={"600519": bars({2: {"suspended": True}})}).run()
    assert any(order.status == BacktestOrderStatus.BLOCKED_SUSPENSION.value for order in suspended_run.orders)

    limit_run = engine(market_data={"600519": bars({2: {"open": "11.22", "high": "11.22", "low": "11.22", "close": "11.22"}})}).run()
    assert any(order.status == BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value for order in limit_run.orders)


def test_different_price_limit_rules_are_used():
    st_rules = rules(price_limit="0.05")
    limit_run = engine(
        instrument_rules={"600519": st_rules},
        market_data={"600519": bars({2: {"open": "10.72", "high": "10.72", "low": "10.72", "close": "10.72"}})},
    ).run()
    assert any(order.status == BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value for order in limit_run.orders)


def test_missing_instrument_rules_fail_closed():
    with pytest.raises(BacktestError, match="missing instrument rules"):
        engine(instrument_rules={}).run()


def test_volume_participation_partial_fill_and_remaining_quantity():
    run = engine(market_data={"600519": bars({2: {"volume": 1300}})}).run()
    order = [order for order in run.orders if order.side == BacktestSide.BUY.value][0]

    assert order.status == BacktestOrderStatus.PARTIALLY_FILLED.value
    assert order.remaining_quantity > 0


def test_order_expiry_does_not_change_assets_after_expiry():
    run = engine(market_data={"600519": bars({2: {"suspended": True}, 3: {"suspended": True}})}).run()

    assert any(order.status in {BacktestOrderStatus.BLOCKED_SUSPENSION.value, BacktestOrderStatus.EXPIRED.value} for order in run.orders)


def test_limit_buy_and_limit_sell_touch_conditions():
    bt = engine()
    buy = BacktestOrder("buy", "run", "600519", BacktestSide.BUY.value, BacktestOrderType.LIMIT.value, 100, 100, Decimal("10.10"), date(2026, 1, 6), date(2026, 1, 6), date(2026, 1, 6))
    sell = BacktestOrder("sell", "run", "600519", BacktestSide.SELL.value, BacktestOrderType.LIMIT.value, 100, 100, Decimal("10.50"), date(2026, 1, 6), date(2026, 1, 6), date(2026, 1, 6))
    portfolio = Portfolio(cash_available=Decimal("100000"), positions={"600519": backtest.BacktestPosition(symbol="600519", total_quantity=100, available_quantity=100, average_cost=Decimal("10"), last_price=Decimal("10"))})
    bar = backtest.DailyBar(date(2026, 1, 6), Decimal("10.20"), Decimal("10.60"), Decimal("10.05"), Decimal("10.40"), 10000)

    assert bt.matching.execute(order=buy, bar=bar, previous_close=Decimal("10"), rules=rules(), portfolio=portfolio, run_id="run") is not None
    assert bt.matching.execute(order=sell, bar=bar, previous_close=Decimal("10"), rules=rules(), portfolio=portfolio, run_id="run") is not None


def test_same_bar_stop_take_profit_conflict_uses_worst_case():
    run = engine(market_data={"600519": bars({3: {"high": "11.00", "low": "9.70"}})}).run()

    assert run.result.same_bar_conflict_count >= 1


def test_minimum_commission_sell_tax_and_partial_fee_are_recorded():
    run = engine(fee_config=FeeConfig(minimum_commission=Decimal("5"), sell_tax_rate=Decimal("0.001"))).run()

    assert run.result.total_commission >= Decimal("5")
    assert run.result.total_tax >= Decimal("0")
    assert run.result.total_slippage_cost > Decimal("0")


def test_buy_sell_cash_position_average_cost_and_realized_pnl():
    run = engine().run()

    assert run.daily_equity[-1].total_equity > Decimal("0")
    assert run.result.total_commission >= Decimal("0")
    assert all(item.total_equity == item.cash + item.market_value for item in run.daily_equity)


def test_rejected_and_expired_orders_do_not_change_assets():
    run = engine(config=config(initial_cash=Decimal("100")), risk_engine=FixedRiskEngine(RiskStatus.APPROVED.value, quantity=100)).run()

    assert any(order.status == BacktestOrderStatus.REJECTED.value for order in run.orders)
    assert run.result.final_equity == Decimal("100")


class FixedRiskEngine:
    def __init__(self, status: str, quantity: int = 0):
        self.status = status
        self.quantity = quantity

    def evaluate(self, *, signal, account, policy, reference_price, stop_price, industry=None):
        decision = RiskEngine().evaluate(
            signal=signal,
            account=account,
            policy=policy,
            reference_price=reference_price,
            stop_price=stop_price,
            industry=industry,
        )
        return replace(decision, status=self.status, approved_quantity=self.quantity, requested_quantity=self.quantity)


def test_risk_rejected_and_risk_off_do_not_create_executable_orders():
    rejected = engine(risk_engine=FixedRiskEngine(RiskStatus.REJECTED.value)).run()
    risk_off = engine(risk_engine=FixedRiskEngine(RiskStatus.RISK_OFF.value)).run()

    assert not [order for order in rejected.orders if order.status == BacktestOrderStatus.PENDING.value]
    assert not [order for order in risk_off.orders if order.status == BacktestOrderStatus.PENDING.value]


def test_reduced_uses_approved_quantity_and_data_error_does_not_order():
    reduced = engine(risk_engine=FixedRiskEngine(RiskStatus.REDUCED.value, quantity=100)).run()
    data_error = engine(signal_func=make_signal_func(SignalType.DATA_ERROR.value)).run()

    assert any(order.quantity == 100 for order in reduced.orders)
    assert not data_error.orders


def test_metrics_drawdown_sharpe_no_trade_and_turnover_are_defined():
    no_trade = engine(signal_func=make_signal_func(SignalType.HOLD.value)).run()

    assert no_trade.result.trade_count == 0
    assert no_trade.result.win_rate is None
    assert no_trade.result.max_drawdown >= Decimal("0")
    assert no_trade.result.turnover == Decimal("0")


def test_failed_backtest_persists_failed_status(monkeypatch):
    engine_db = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine_db)
    Session = sessionmaker(bind=engine_db, autoflush=False, autocommit=False)
    monkeypatch.setattr(backtest, "SessionLocal", Session)
    monkeypatch.setattr(backtest, "init_db", lambda: None)

    with pytest.raises(BacktestError):
        engine(market_data={"600519": bars().iloc[:2]}, persist=True).run()

    with Session() as session:
        row = session.query(BacktestRunRecord).one()
    assert row.status == "FAILED"
    assert row.error_message


def test_json_and_csv_export_are_readable(tmp_path):
    run = engine().run()
    json_path = tmp_path / "run.json"
    csv_dir = tmp_path / "csv"

    export_backtest_json(run, json_path)
    export_backtest_csv(run, csv_dir)

    assert json.loads(json_path.read_text(encoding="utf-8"))["run_id"] == run.run_id
    assert (csv_dir / "daily_equity.csv").read_text(encoding="utf-8")


def test_old_database_tables_remain_compatible():
    engine_db = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine_db)

    assert engine_db.dialect.has_table(engine_db.connect(), "signals")
    assert engine_db.dialect.has_table(engine_db.connect(), "backtest_runs")
