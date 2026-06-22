from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import service
from app.data_provider import MarketDataError
from app.db import Base, SignalRecord
from app.strategy import StrategyConfig
from tests.test_strategy import make_signal


def settings():
    return SimpleNamespace(
        market_data_adjust="qfq",
        market_data_min_history_bars=80,
        market_data_max_stale_days=7,
        timezone="Asia/Shanghai",
        watchlist=["600519"],
        account_equity=100000,
        risk_per_trade=0.005,
        stop_loss_pct=0.05,
        max_single_position_pct=0.15,
        strategy_name="multi_factor_v1",
        strategy_version="1.0.0",
        strategy_ma_short_period=20,
        strategy_ma_long_period=60,
        strategy_momentum_period=20,
        strategy_volatility_period=20,
        strategy_volume_period=20,
        strategy_rsi_period=14,
        strategy_breakout_period=20,
        strategy_trend_weight=30,
        strategy_momentum_weight=20,
        strategy_volatility_weight=15,
        strategy_volume_weight=15,
        strategy_rsi_weight=10,
        strategy_breakout_weight=10,
        strategy_buy_watch_threshold=70,
        strategy_stop_loss_pct=0.05,
        strategy_take_profit_1_pct=0.05,
        strategy_take_profit_2_pct=0.08,
    )


def test_market_data_error_becomes_data_error(monkeypatch):
    monkeypatch.setattr(service, "get_settings", settings)

    def fail_fetch(*_args, **_kwargs):
        raise MarketDataError("duplicate trade date")

    monkeypatch.setattr(service, "fetch_daily_history", fail_fetch)

    assert service.scan_watchlist() == [
        {"symbol": "600519", "action": "DATA_ERROR", "reason": "duplicate trade date"}
    ]


def test_unexpected_exception_is_logged_and_reraised(monkeypatch, caplog):
    monkeypatch.setattr(service, "get_settings", settings)

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("database driver exploded")

    monkeypatch.setattr(service, "fetch_daily_history", fail_fetch)

    with pytest.raises(RuntimeError, match="database driver exploded"):
        service.scan_watchlist()

    assert "Unexpected scan failure for symbol 600519" in caplog.text


@pytest.fixture
def memory_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(service, "SessionLocal", Session)
    return Session


def test_old_signal_record_missing_new_fields_can_be_read(memory_session):
    with memory_session() as session:
        session.add(
            SignalRecord(
                symbol="600519",
                action="HOLD",
                score=0,
                price=10,
                stop_price=9.5,
                take_profit_1=10.5,
                take_profit_2=10.8,
                suggested_shares=0,
                reason="old row",
            )
        )
        session.commit()

    rows = service.latest_signals()

    assert rows[0]["symbol"] == "600519"
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["score_breakdown"] == {}
    assert rows[0]["reasons"] == []
    assert rows[0]["signal_type"] == "HOLD"
    assert rows[0]["reference_price"] == 10


def test_same_signal_is_not_inserted_repeatedly(memory_session):
    signal = make_signal()

    service.save_signal(signal)
    service.save_signal(signal)

    with memory_session() as session:
        rows = session.query(SignalRecord).all()
    assert len(rows) == 1


def test_same_strategy_version_with_different_parameter_versions_can_be_retained(memory_session):
    first = make_signal(StrategyConfig(buy_watch_threshold=90))
    second = make_signal(StrategyConfig(buy_watch_threshold=89))

    service.save_signal(first)
    service.save_signal(second)

    with memory_session() as session:
        rows = session.query(SignalRecord).order_by(SignalRecord.parameter_version).all()

    assert len(rows) == 2
    assert len({row.parameter_version for row in rows}) == 2
    assert rows[0].parameter_snapshot


def test_different_strategy_versions_can_be_retained(memory_session):
    first = make_signal(StrategyConfig(strategy_version="1.0.0"))
    second = make_signal(StrategyConfig(strategy_version="1.0.1"))

    service.save_signal(first)
    service.save_signal(second)

    with memory_session() as session:
        rows = session.query(SignalRecord).order_by(SignalRecord.strategy_version).all()

    assert [row.strategy_version for row in rows] == ["1.0.0", "1.0.1"]
    assert json.loads(rows[0].score_breakdown)["trend"]["max_score"] == 30


def test_database_unique_constraint_catches_duplicate_dedupe_key(memory_session):
    signal = make_signal()
    dedupe_key = service.signal_dedupe_key(signal)

    with memory_session() as session:
        first = SignalRecord(
            symbol=signal.symbol,
            action=signal.action,
            score=signal.score,
            price=signal.price,
            stop_price=signal.stop_price,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            suggested_shares=signal.suggested_shares,
            reason=signal.reason,
            dedupe_key=dedupe_key,
        )
        second = SignalRecord(
            symbol=signal.symbol,
            action=signal.action,
            score=signal.score,
            price=signal.price,
            stop_price=signal.stop_price,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            suggested_shares=signal.suggested_shares,
            reason=signal.reason,
            dedupe_key=dedupe_key,
        )
        session.add(first)
        session.commit()
        session.add(second)
        with pytest.raises(IntegrityError):
            session.commit()
