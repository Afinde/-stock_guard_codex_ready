from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.data_provider import MarketDataSnapshot
from app.strategy import (
    SignalType,
    StrategyConfig,
    calculate_position_shares,
    evaluate_exit,
    generate_signal,
)


def make_snapshot(symbol: str = "600519", adjust: str = "qfq") -> MarketDataSnapshot:
    periods = 90
    closes = [10 + index * 0.03 + np.sin(index / 3) * 0.2 for index in range(periods)]
    closes[-1] = max(closes) + 1
    dates = pd.date_range(end="2026-06-18", periods=periods, freq="D")
    bars = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [price + 0.2 for price in closes],
            "low": [price - 0.2 for price in closes],
            "close": closes,
            "volume": [1000] * periods,
        }
    )
    bars.loc[periods - 1, "volume"] = 1500
    fetched_at = datetime(2026, 6, 18, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    validated_at = datetime(2026, 6, 18, 16, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    return MarketDataSnapshot(
        bars=bars,
        provider="fixture",
        symbol=symbol,
        adjust=adjust,
        first_date=date(2026, 3, 21),
        last_date=date(2026, 6, 18),
        row_count=periods,
        fetched_at=fetched_at,
        validated_at=validated_at,
        data_version=f"fixture:daily:{adjust}",
    )


def make_signal(config: StrategyConfig | None = None):
    return generate_signal(
        symbol="600519",
        market_data=make_snapshot(),
        account_equity=100000,
        risk_per_trade=0.005,
        max_single_position_pct=0.15,
        config=config or StrategyConfig(),
    )


def test_position_sizing_uses_account_risk():
    shares = calculate_position_shares(
        account_equity=100000,
        entry_price=20,
        stop_loss_pct=0.05,
        risk_per_trade=0.005,
        max_single_position_pct=0.15,
    )
    assert shares == 500


def test_fixed_stop():
    action, _ = evaluate_exit(entry_price=10, current_price=9.49, highest_price=10.2)
    assert action == SignalType.SELL


def test_default_config_preserves_existing_scoring_behavior():
    signal = make_signal()

    assert signal.action == SignalType.BUY_WATCH
    assert signal.score == 90
    assert signal.stop_price == round(signal.price * 0.95, 2)
    assert signal.take_profit_1 == round(signal.price * 1.05, 2)
    assert signal.take_profit_2 == round(signal.price * 1.08, 2)


def test_invalid_period_config_fails():
    with pytest.raises(ValueError, match="positive integers"):
        StrategyConfig(ma_short_period=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, strategy_ma_short_period=0)


def test_invalid_weight_config_fails():
    with pytest.raises(ValueError, match="sum to 100"):
        StrategyConfig(trend_weight=31)
    with pytest.raises(ValueError, match="strategy weights must sum to 100"):
        Settings(_env_file=None, strategy_trend_weight=31)


def test_invalid_stop_loss_take_profit_config_fails():
    with pytest.raises(ValueError, match="less than 1"):
        StrategyConfig(stop_loss_pct=1)
    with pytest.raises(ValueError, match="greater than take_profit_1"):
        StrategyConfig(take_profit_1_pct=0.08, take_profit_2_pct=0.05)
    with pytest.raises(ValueError, match="strategy_take_profit_2_pct"):
        Settings(_env_file=None, strategy_take_profit_1_pct=0.08, strategy_take_profit_2_pct=0.05)


def test_same_input_produces_same_output():
    first = make_signal().to_dict()
    second = make_signal().to_dict()

    assert first == second


def test_score_equals_factor_score_sum():
    signal = make_signal()

    assert signal.score == sum(item["score"] for item in signal.score_breakdown.values())


def test_score_breakdown_contains_all_required_factors():
    signal = make_signal()

    assert set(signal.score_breakdown) == {
        "trend",
        "momentum",
        "volatility",
        "volume",
        "rsi",
        "breakout",
    }
    for item in signal.score_breakdown.values():
        assert {"value", "score", "max_score", "passed", "reason"} <= set(item)


def test_signal_contains_strategy_version_and_market_metadata():
    signal = make_signal()

    assert signal.strategy_name == "multi_factor_v1"
    assert signal.strategy_version == "1.0.0"
    assert signal.parameter_version
    assert signal.parameter_snapshot
    assert json.loads(signal.parameter_snapshot)["ma_short_period"] == 20
    assert signal.market_as_of_date == date(2026, 6, 18)
    assert signal.market_data_source == "fixture"
    assert signal.market_data_adjust == "qfq"


def test_parameter_version_is_hash_of_stable_parameter_snapshot():
    config = StrategyConfig()

    assert config.parameter_snapshot == json.dumps(
        config.parameter_payload(), sort_keys=True, separators=(",", ":")
    )
    assert config.parameter_version == hashlib.sha256(
        config.parameter_snapshot.encode("utf-8")
    ).hexdigest()[:16]


def test_buy_watch_contains_real_reasons_and_invalidation_conditions():
    signal = make_signal()

    assert any("收盘价高于MA20" in reason for reason in signal.reasons)
    assert any("20日动量处于配置区间" in reason for reason in signal.reasons)
    assert any("成交量达到20日均量" in reason for reason in signal.reasons)
    assert any("价格跌破MA20" in item for item in signal.invalidation_conditions)
    assert any("价格跌破止损价" in item for item in signal.invalidation_conditions)
    assert any("行情数据过期" in item for item in signal.invalidation_conditions)


def test_score_below_threshold_does_not_buy_watch():
    config = StrategyConfig(buy_watch_threshold=91)

    signal = make_signal(config)

    assert signal.score == 90
    assert signal.action == SignalType.HOLD


def test_score_equal_threshold_is_buy_watch():
    config = StrategyConfig(buy_watch_threshold=90)

    signal = make_signal(config)

    assert signal.score == 90
    assert signal.action == SignalType.BUY_WATCH


def test_strategy_layer_does_not_generate_buy_confirm():
    signal = make_signal(StrategyConfig(buy_watch_threshold=0))

    assert signal.action == SignalType.BUY_WATCH
    assert signal.action != SignalType.BUY_CONFIRM
