from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from .data_provider import MarketDataSnapshot


class SignalType(StrEnum):
    BUY_WATCH = "BUY_WATCH"
    BUY_CONFIRM = "BUY_CONFIRM"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    RISK_OFF = "RISK_OFF"
    DATA_ERROR = "DATA_ERROR"


@dataclass(frozen=True)
class StrategyConfig:
    strategy_name: str = "multi_factor_v1"
    strategy_version: str = "1.0.0"
    ma_short_period: int = 20
    ma_long_period: int = 60
    momentum_period: int = 20
    volatility_period: int = 20
    volume_period: int = 20
    rsi_period: int = 14
    breakout_period: int = 20
    trend_weight: float = 30.0
    momentum_weight: float = 20.0
    volatility_weight: float = 15.0
    volume_weight: float = 15.0
    rsi_weight: float = 10.0
    breakout_weight: float = 10.0
    buy_watch_threshold: float = 70.0
    stop_loss_pct: float = 0.05
    take_profit_1_pct: float = 0.05
    take_profit_2_pct: float = 0.08
    momentum_min: float = 0.03
    momentum_max: float = 0.18
    volatility_max: float = 0.035
    volume_ratio_min: float = 1.1
    volume_ratio_max: float = 2.5
    rsi_min: float = 45.0
    rsi_max: float = 68.0

    def __post_init__(self) -> None:
        periods = [
            self.ma_short_period,
            self.ma_long_period,
            self.momentum_period,
            self.volatility_period,
            self.volume_period,
            self.rsi_period,
            self.breakout_period,
        ]
        if any(period <= 0 for period in periods):
            raise ValueError("strategy periods must be positive integers")

        weights = self.weights()
        if any(weight < 0 for weight in weights.values()):
            raise ValueError("strategy weights must not be negative")
        total_weight = self.total_weight
        if abs(total_weight - 100.0) > 1e-9:
            raise ValueError("strategy weights must sum to 100")
        if not 0 <= self.buy_watch_threshold <= total_weight:
            raise ValueError("buy_watch_threshold must be between 0 and total strategy weight")
        if not 0 < self.stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be greater than 0 and less than 1")
        if self.take_profit_1_pct <= 0 or self.take_profit_2_pct <= 0:
            raise ValueError("take-profit percentages must be greater than 0")
        if self.take_profit_2_pct <= self.take_profit_1_pct:
            raise ValueError("take_profit_2_pct must be greater than take_profit_1_pct")
        if self.ma_long_period <= self.ma_short_period:
            raise ValueError("ma_long_period must be greater than ma_short_period")

    @property
    def total_weight(self) -> float:
        return sum(self.weights().values())

    @property
    def parameter_version(self) -> str:
        return hashlib.sha256(self.parameter_snapshot.encode("utf-8")).hexdigest()[:16]

    @property
    def parameter_snapshot(self) -> str:
        return json.dumps(self.parameter_payload(), sort_keys=True, separators=(",", ":"))

    def weights(self) -> dict[str, float]:
        return {
            "trend": self.trend_weight,
            "momentum": self.momentum_weight,
            "volatility": self.volatility_weight,
            "volume": self.volume_weight,
            "rsi": self.rsi_weight,
            "breakout": self.breakout_weight,
        }

    def parameter_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactorScore:
    value: float | dict[str, float]
    score: float
    max_score: float
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str
    score: float
    price: float
    stop_price: float
    take_profit_1: float
    take_profit_2: float
    suggested_shares: int
    reason: str
    market_trade_date: date
    market_fetched_at: datetime
    signal_generated_at: datetime
    strategy_name: str
    strategy_version: str
    parameter_version: str
    parameter_snapshot: str
    market_as_of_date: date
    market_data_source: str
    market_data_adjust: str
    signal_type: str
    score_breakdown: dict[str, dict[str, Any]]
    reasons: list[str]
    invalidation_conditions: list[str]
    reference_price: float
    stop_loss_price: float
    take_profit_1_price: float
    take_profit_2_price: float
    market_data_checksum: str = ""
    market_calendar_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["market_trade_date"] = self.market_trade_date.isoformat()
        payload["market_as_of_date"] = self.market_as_of_date.isoformat()
        payload["market_fetched_at"] = self.market_fetched_at.isoformat()
        payload["signal_generated_at"] = self.signal_generated_at.isoformat()
        return payload


def strategy_config_from_settings(settings: Any) -> StrategyConfig:
    return StrategyConfig(
        strategy_name=settings.strategy_name,
        strategy_version=settings.strategy_version,
        ma_short_period=settings.strategy_ma_short_period,
        ma_long_period=settings.strategy_ma_long_period,
        momentum_period=settings.strategy_momentum_period,
        volatility_period=settings.strategy_volatility_period,
        volume_period=settings.strategy_volume_period,
        rsi_period=settings.strategy_rsi_period,
        breakout_period=settings.strategy_breakout_period,
        trend_weight=settings.strategy_trend_weight,
        momentum_weight=settings.strategy_momentum_weight,
        volatility_weight=settings.strategy_volatility_weight,
        volume_weight=settings.strategy_volume_weight,
        rsi_weight=settings.strategy_rsi_weight,
        breakout_weight=settings.strategy_breakout_weight,
        buy_watch_threshold=settings.strategy_buy_watch_threshold,
        stop_loss_pct=settings.strategy_stop_loss_pct,
        take_profit_1_pct=settings.strategy_take_profit_1_pct,
        take_profit_2_pct=settings.strategy_take_profit_2_pct,
    )


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def enrich(df: pd.DataFrame, config: StrategyConfig | None = None) -> pd.DataFrame:
    cfg = config or StrategyConfig()
    out = df.copy()
    out["ma_short"] = out["close"].rolling(cfg.ma_short_period).mean()
    out["ma_long"] = out["close"].rolling(cfg.ma_long_period).mean()
    out["momentum"] = out["close"].pct_change(cfg.momentum_period)
    out["volatility"] = out["close"].pct_change().rolling(cfg.volatility_period).std()
    out["volume_ma"] = out["volume"].rolling(cfg.volume_period).mean()
    out["volume_ratio"] = out["volume"] / out["volume_ma"]
    out["rsi"] = rsi(out["close"], cfg.rsi_period)
    out["breakout_high"] = out["high"].rolling(cfg.breakout_period).max().shift(1)
    return out


def calculate_position_shares(
    account_equity: float,
    entry_price: float,
    stop_loss_pct: float,
    risk_per_trade: float,
    max_single_position_pct: float,
) -> int:
    if entry_price <= 0 or stop_loss_pct <= 0:
        return 0
    risk_budget = account_equity * risk_per_trade
    risk_per_share = entry_price * stop_loss_pct
    shares_by_risk = int(risk_budget / risk_per_share)
    shares_by_cap = int((account_equity * max_single_position_pct) / entry_price)
    raw = max(0, min(shares_by_risk, shares_by_cap))
    return (raw // 100) * 100  # A-share board lot


def generate_signal(
    symbol: str,
    market_data: MarketDataSnapshot,
    account_equity: float,
    risk_per_trade: float = 0.005,
    stop_loss_pct: float | None = None,
    max_single_position_pct: float = 0.15,
    config: StrategyConfig | None = None,
) -> Signal:
    cfg = config or StrategyConfig()
    effective_stop_loss_pct = cfg.stop_loss_pct if stop_loss_pct is None else stop_loss_pct
    if market_data.symbol != symbol:
        raise ValueError(f"Market data symbol mismatch: {market_data.symbol} != {symbol}")

    df = enrich(market_data.bars, cfg)
    min_bars = max(
        cfg.ma_long_period,
        cfg.momentum_period + 1,
        cfg.volatility_period + 1,
        cfg.volume_period,
        cfg.rsi_period + 1,
        cfg.breakout_period + 1,
    )
    if len(df) < min_bars:
        raise ValueError(f"Insufficient history for {symbol}; need at least {min_bars} bars")

    row = df.iloc[-1]
    breakdown = score_factors(row, cfg)
    score = round(sum(item["score"] for item in breakdown.values()), 2)
    signal_type = SignalType.BUY_WATCH if score >= cfg.buy_watch_threshold else SignalType.HOLD
    price = round(float(row.close), 2)
    reasons = _signal_reasons(breakdown)
    invalidation_conditions = _invalidation_conditions(row, price, cfg)
    shares = calculate_position_shares(
        account_equity=account_equity,
        entry_price=price,
        stop_loss_pct=effective_stop_loss_pct,
        risk_per_trade=risk_per_trade,
        max_single_position_pct=max_single_position_pct,
    ) if signal_type == SignalType.BUY_WATCH else 0
    stop_price = round(price * (1 - effective_stop_loss_pct), 2)
    take_profit_1 = round(price * (1 + cfg.take_profit_1_pct), 2)
    take_profit_2 = round(price * (1 + cfg.take_profit_2_pct), 2)

    return Signal(
        symbol=symbol,
        action=signal_type.value,
        score=score,
        price=price,
        stop_price=stop_price,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        suggested_shares=shares,
        reason="；".join(reasons) if reasons else "未满足入选条件",
        market_trade_date=market_data.last_date,
        market_fetched_at=market_data.fetched_at,
        signal_generated_at=market_data.validated_at,
        strategy_name=cfg.strategy_name,
        strategy_version=cfg.strategy_version,
        parameter_version=cfg.parameter_version,
        parameter_snapshot=cfg.parameter_snapshot,
        market_as_of_date=market_data.last_date,
        market_data_source=market_data.provider,
        market_data_adjust=market_data.adjust,
        signal_type=signal_type.value,
        score_breakdown=breakdown,
        reasons=reasons,
        invalidation_conditions=invalidation_conditions,
        reference_price=price,
        stop_loss_price=stop_price,
        take_profit_1_price=take_profit_1,
        take_profit_2_price=take_profit_2,
        market_data_checksum=market_data.data_checksum,
        market_calendar_version=market_data.calendar_version,
    )


def score_factors(row: pd.Series, config: StrategyConfig) -> dict[str, dict[str, Any]]:
    trend_passed = bool(row.close > row.ma_short > row.ma_long)
    momentum_passed = bool(config.momentum_min <= row.momentum <= config.momentum_max)
    volatility_passed = bool(row.volatility <= config.volatility_max)
    volume_passed = bool(config.volume_ratio_min <= row.volume_ratio <= config.volume_ratio_max)
    rsi_passed = bool(config.rsi_min <= row.rsi <= config.rsi_max)
    breakout_passed = bool(row.close > row.breakout_high)

    factors = {
        "trend": FactorScore(
            value={
                "close": _finite_float(row.close),
                "ma_short": _finite_float(row.ma_short),
                "ma_long": _finite_float(row.ma_long),
            },
            score=config.trend_weight if trend_passed else 0.0,
            max_score=config.trend_weight,
            passed=trend_passed,
            reason=(
                f"收盘价高于MA{config.ma_short_period}和MA{config.ma_long_period}，"
                f"且MA{config.ma_short_period}高于MA{config.ma_long_period}"
                if trend_passed
                else "趋势结构未满足"
            ),
        ),
        "momentum": FactorScore(
            value=_finite_float(row.momentum),
            score=config.momentum_weight if momentum_passed else 0.0,
            max_score=config.momentum_weight,
            passed=momentum_passed,
            reason=(
                f"{config.momentum_period}日动量处于配置区间"
                if momentum_passed
                else f"{config.momentum_period}日动量未处于配置区间"
            ),
        ),
        "volatility": FactorScore(
            value=_finite_float(row.volatility),
            score=config.volatility_weight if volatility_passed else 0.0,
            max_score=config.volatility_weight,
            passed=volatility_passed,
            reason=(
                f"{config.volatility_period}日波动率受控"
                if volatility_passed
                else f"{config.volatility_period}日波动率未满足上限"
            ),
        ),
        "volume": FactorScore(
            value=_finite_float(row.volume_ratio),
            score=config.volume_weight if volume_passed else 0.0,
            max_score=config.volume_weight,
            passed=volume_passed,
            reason=(
                f"成交量达到{config.volume_period}日均量的配置倍数"
                if volume_passed
                else f"成交量未处于{config.volume_period}日均量配置倍数"
            ),
        ),
        "rsi": FactorScore(
            value=_finite_float(row.rsi),
            score=config.rsi_weight if rsi_passed else 0.0,
            max_score=config.rsi_weight,
            passed=rsi_passed,
            reason="RSI处于允许区间" if rsi_passed else "RSI未处于允许区间",
        ),
        "breakout": FactorScore(
            value={
                "close": _finite_float(row.close),
                "breakout_high": _finite_float(row.breakout_high),
            },
            score=config.breakout_weight if breakout_passed else 0.0,
            max_score=config.breakout_weight,
            passed=breakout_passed,
            reason=(
                f"突破前{config.breakout_period}日高点"
                if breakout_passed
                else f"未突破前{config.breakout_period}日高点"
            ),
        ),
    }
    return {name: factor.to_dict() for name, factor in factors.items()}


def evaluate_exit(entry_price: float, current_price: float, highest_price: float) -> tuple[str, str]:
    if current_price <= entry_price * 0.95:
        return SignalType.SELL.value, "触发固定5%止损"
    if highest_price >= entry_price * 1.08 and current_price <= highest_price * 0.97:
        return SignalType.SELL.value, "达到第二目标后回撤3%，触发移动止盈"
    if current_price >= entry_price * 1.05:
        return SignalType.REDUCE.value, "达到第一目标，建议分批减仓而非一次清仓"
    return SignalType.HOLD.value, "未触发退出条件"


def _signal_reasons(score_breakdown: dict[str, dict[str, Any]]) -> list[str]:
    return [item["reason"] for item in score_breakdown.values() if item["passed"]]


def _invalidation_conditions(row: pd.Series, reference_price: float, config: StrategyConfig) -> list[str]:
    stop_price = round(reference_price * (1 - config.stop_loss_pct), 2)
    return [
        f"价格跌破MA{config.ma_short_period}：{_finite_float(row.ma_short):.2f}",
        f"价格跌破止损价：{stop_price:.2f}",
        f"趋势结构失效：收盘价不再高于MA{config.ma_short_period}且MA{config.ma_short_period}不再高于MA{config.ma_long_period}",
        "行情数据过期",
        "数据源返回异常",
    ]


def _finite_float(value: Any) -> float:
    number = float(value)
    if not np.isfinite(number):
        return float("nan")
    return round(number, 6)
