from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .data_provider import LocalTradingCalendar, MarketDataSnapshot, market_data_checksum
from .db import (
    BacktestDailyEquityRecord,
    BacktestFillRecord,
    BacktestOrderRecord,
    BacktestPositionRecord,
    BacktestRunRecord,
    BacktestCorporateActionEventRecord,
    CorporateActionRecord,
    DividendEntitlementRecord,
    SessionLocal,
    init_db,
)
from .risk import (
    AccountSnapshot,
    PositionSnapshot,
    RiskDecision,
    RiskEngine,
    RiskPolicy,
    RiskStatus,
    decimal_to_str,
    signal_identity,
    stable_id,
    stable_json,
)
from .strategy import Signal, SignalType, StrategyConfig, generate_signal


MONEY = Decimal("0.01")
TZ = ZoneInfo("Asia/Shanghai")


class BacktestError(RuntimeError):
    pass


class ResearchOnlyError(BacktestError):
    pass


class EventType(StrEnum):
    SESSION_START = "SESSION_START"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    MARKET_OPEN = "MARKET_OPEN"
    ORDER = "ORDER"
    FILL = "FILL"
    BAR = "BAR"
    SESSION_CLOSE = "SESSION_CLOSE"
    SIGNAL = "SIGNAL"
    RISK = "RISK"
    SETTLEMENT = "SETTLEMENT"


class BacktestOrderType(StrEnum):
    MARKET_ON_NEXT_OPEN = "MARKET_ON_NEXT_OPEN"
    LIMIT = "LIMIT"


class BacktestSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BacktestOrderStatus(StrEnum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    BLOCKED_T1 = "BLOCKED_T1"
    BLOCKED_SUSPENSION = "BLOCKED_SUSPENSION"
    BLOCKED_PRICE_LIMIT = "BLOCKED_PRICE_LIMIT"
    BLOCKED_LIQUIDITY = "BLOCKED_LIQUIDITY"
    CANCELLED_CORPORATE_ACTION = "CANCELLED_CORPORATE_ACTION"


class SameBarConflictPolicy(StrEnum):
    WORST_CASE = "WORST_CASE"


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CAPITALIZATION = "CAPITALIZATION"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    DELISTING = "DELISTING"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class ResultQuality(StrEnum):
    REALISTIC_WITH_MODELED_CORPORATE_ACTIONS = "REALISTIC_WITH_MODELED_CORPORATE_ACTIONS"
    RESEARCH_ONLY_ADJUSTED_PRICES = "RESEARCH_ONLY_ADJUSTED_PRICES"
    INCOMPLETE_CORPORATE_ACTIONS = "INCOMPLETE_CORPORATE_ACTIONS"


class DividendEntitlementStatus(StrEnum):
    CREATED = "CREATED"
    PAID = "PAID"


class RightsIssuePolicy(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"
    IGNORE_WITH_WARNING = "IGNORE_WITH_WARNING"
    CASH_SUBSCRIBE = "CASH_SUBSCRIBE"


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    initial_cash: Decimal
    symbols: tuple[str, ...]
    strategy_name: str
    strategy_version: str
    parameter_version: str
    execution_price_adjust: str = ""
    signal_price_adjust: str = "qfq"
    benchmark: str | None = None
    annualization_days: int = 244
    risk_free_rate: Decimal = Decimal("0")
    same_bar_conflict_policy: str = SameBarConflictPolicy.WORST_CASE.value
    order_expiry_sessions: int = 1
    volume_participation_rate: Decimal = Decimal("0.10")
    sale_proceeds_reusable_same_day: bool = False
    random_seed: int | None = None
    rights_issue_policy: str = RightsIssuePolicy.FAIL_CLOSED.value

    def __post_init__(self) -> None:
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be earlier than end_date")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be greater than 0")
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if self.annualization_days <= 0:
            raise ValueError("annualization_days must be positive")
        if self.order_expiry_sessions <= 0:
            raise ValueError("order_expiry_sessions must be positive")
        if not (Decimal("0") < self.volume_participation_rate <= Decimal("1")):
            raise ValueError("volume_participation_rate must be in (0, 1]")
        if self.same_bar_conflict_policy != SameBarConflictPolicy.WORST_CASE.value:
            raise ValueError("same_bar_conflict_policy currently supports WORST_CASE only")
        if self.execution_price_adjust:
            raise ResearchOnlyError("execution_price_adjust must be empty/raw for cash P&L in this milestone")
        if self.rights_issue_policy not in {policy.value for policy in RightsIssuePolicy}:
            raise ValueError("rights_issue_policy is invalid")
        if not self.strategy_name or not self.strategy_version or not self.parameter_version:
            raise ValueError("strategy name/version and parameter_version must be explicit")

    @property
    def config_checksum(self) -> str:
        return hashlib.sha256(stable_json(self.to_dict()).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_cash": decimal_to_str(self.initial_cash),
            "symbols": list(self.symbols),
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "execution_price_adjust": self.execution_price_adjust,
            "signal_price_adjust": self.signal_price_adjust,
            "benchmark": self.benchmark,
            "annualization_days": self.annualization_days,
            "risk_free_rate": decimal_to_str(self.risk_free_rate),
            "same_bar_conflict_policy": self.same_bar_conflict_policy,
            "order_expiry_sessions": self.order_expiry_sessions,
            "volume_participation_rate": decimal_to_str(self.volume_participation_rate),
            "sale_proceeds_reusable_same_day": self.sale_proceeds_reusable_same_day,
            "random_seed": self.random_seed,
            "rights_issue_policy": self.rights_issue_policy,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BacktestConfig":
        return cls(
            start_date=date.fromisoformat(payload["start_date"]),
            end_date=date.fromisoformat(payload["end_date"]),
            initial_cash=Decimal(str(payload["initial_cash"])),
            symbols=tuple(payload["symbols"]),
            strategy_name=payload["strategy_name"],
            strategy_version=payload["strategy_version"],
            parameter_version=payload["parameter_version"],
            execution_price_adjust=payload.get("execution_price_adjust", ""),
            signal_price_adjust=payload.get("signal_price_adjust", "qfq"),
            benchmark=payload.get("benchmark"),
            annualization_days=int(payload.get("annualization_days", 244)),
            risk_free_rate=Decimal(str(payload.get("risk_free_rate", "0"))),
            same_bar_conflict_policy=payload.get("same_bar_conflict_policy", SameBarConflictPolicy.WORST_CASE.value),
            order_expiry_sessions=int(payload.get("order_expiry_sessions", 1)),
            volume_participation_rate=Decimal(str(payload.get("volume_participation_rate", "0.10"))),
            sale_proceeds_reusable_same_day=bool(payload.get("sale_proceeds_reusable_same_day", False)),
            random_seed=payload.get("random_seed"),
            rights_issue_policy=payload.get("rights_issue_policy", RightsIssuePolicy.FAIL_CLOSED.value),
        )


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    symbol: str
    action_type: str
    announcement_date: date
    record_date: date
    ex_date: date
    payment_date: date | None = None
    tradable_date: date | None = None
    cash_per_share: Decimal | None = None
    stock_ratio: Decimal | None = None
    capitalization_ratio: Decimal | None = None
    rights_ratio: Decimal | None = None
    rights_price: Decimal | None = None
    new_symbol: str | None = None
    source: str = "offline"
    source_version: str = "fixture-v1"
    data_checksum: str = ""

    def __post_init__(self) -> None:
        if self.action_type not in {item.value for item in CorporateActionType}:
            raise ValueError("invalid corporate action type")
        if self.announcement_date > self.record_date:
            raise ValueError("announcement_date must not be after record_date")
        if self.record_date > self.ex_date:
            raise ValueError("record_date must not be after ex_date")
        if self.payment_date is not None and self.payment_date < self.record_date:
            raise ValueError("payment_date must not be before record_date")
        if self.tradable_date is not None and self.tradable_date < self.ex_date:
            raise ValueError("tradable_date must not be before ex_date")
        for value in [
            self.cash_per_share,
            self.stock_ratio,
            self.capitalization_ratio,
            self.rights_ratio,
            self.rights_price,
        ]:
            if value is not None and value < 0:
                raise ValueError("corporate action amounts and ratios must not be negative")
        if self.action_type == CorporateActionType.CASH_DIVIDEND.value and self.cash_per_share is None:
            raise ValueError("cash dividend requires cash_per_share")
        if self.action_type == CorporateActionType.STOCK_DIVIDEND.value and self.stock_ratio is None:
            raise ValueError("stock dividend requires stock_ratio")
        if self.action_type == CorporateActionType.CAPITALIZATION.value and self.capitalization_ratio is None:
            raise ValueError("capitalization requires capitalization_ratio")
        if self.action_type in {CorporateActionType.SPLIT.value, CorporateActionType.REVERSE_SPLIT.value} and self.stock_ratio is None:
            raise ValueError("split and reverse split require stock_ratio")
        if self.action_type == CorporateActionType.RIGHTS_ISSUE.value and (self.rights_ratio is None or self.rights_price is None):
            raise ValueError("rights issue requires rights_ratio and rights_price")
        if self.action_type == CorporateActionType.SYMBOL_CHANGE.value and not self.new_symbol:
            raise ValueError("symbol change requires new_symbol")
        if not self.action_id:
            object.__setattr__(self, "action_id", stable_id("ca", self.symbol, self.action_type, self.ex_date.isoformat(), self.source_version))
        if not self.data_checksum:
            object.__setattr__(self, "data_checksum", hashlib.sha256(stable_json(self.to_dict(include_checksum=False)).encode("utf-8")).hexdigest())

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload = {
            "action_id": self.action_id,
            "symbol": self.symbol,
            "action_type": self.action_type,
            "announcement_date": self.announcement_date.isoformat(),
            "record_date": self.record_date.isoformat(),
            "ex_date": self.ex_date.isoformat(),
            "payment_date": None if self.payment_date is None else self.payment_date.isoformat(),
            "tradable_date": None if self.tradable_date is None else self.tradable_date.isoformat(),
            "cash_per_share": None if self.cash_per_share is None else decimal_to_str(self.cash_per_share),
            "stock_ratio": None if self.stock_ratio is None else decimal_to_str(self.stock_ratio),
            "capitalization_ratio": None if self.capitalization_ratio is None else decimal_to_str(self.capitalization_ratio),
            "rights_ratio": None if self.rights_ratio is None else decimal_to_str(self.rights_ratio),
            "rights_price": None if self.rights_price is None else decimal_to_str(self.rights_price),
            "new_symbol": self.new_symbol,
            "source": self.source,
            "source_version": self.source_version,
        }
        if include_checksum:
            payload["data_checksum"] = self.data_checksum
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CorporateAction":
        return cls(
            action_id=str(payload.get("action_id", "")),
            symbol=payload["symbol"],
            action_type=payload["action_type"],
            announcement_date=date.fromisoformat(payload["announcement_date"]),
            record_date=date.fromisoformat(payload["record_date"]),
            ex_date=date.fromisoformat(payload["ex_date"]),
            payment_date=date.fromisoformat(payload["payment_date"]) if payload.get("payment_date") else None,
            tradable_date=date.fromisoformat(payload["tradable_date"]) if payload.get("tradable_date") else None,
            cash_per_share=_optional_decimal(payload.get("cash_per_share")),
            stock_ratio=_optional_decimal(payload.get("stock_ratio")),
            capitalization_ratio=_optional_decimal(payload.get("capitalization_ratio")),
            rights_ratio=_optional_decimal(payload.get("rights_ratio")),
            rights_price=_optional_decimal(payload.get("rights_price")),
            new_symbol=payload.get("new_symbol"),
            source=payload.get("source", "offline"),
            source_version=payload.get("source_version", "fixture-v1"),
            data_checksum=payload.get("data_checksum", ""),
        )


@dataclass
class DividendEntitlement:
    entitlement_id: str
    action_id: str
    symbol: str
    eligible_quantity: int
    gross_cash: Decimal
    tax: Decimal
    net_cash: Decimal
    record_date: date
    payment_date: date
    status: str = DividendEntitlementStatus.CREATED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "entitlement_id": self.entitlement_id,
            "action_id": self.action_id,
            "symbol": self.symbol,
            "eligible_quantity": self.eligible_quantity,
            "gross_cash": decimal_to_str(self.gross_cash),
            "tax": decimal_to_str(self.tax),
            "net_cash": decimal_to_str(self.net_cash),
            "record_date": self.record_date.isoformat(),
            "payment_date": self.payment_date.isoformat(),
            "status": self.status,
        }


@dataclass(frozen=True)
class CorporateActionLedgerEvent:
    run_id: str
    action_id: str
    symbol: str
    event_type: str
    session_date: date
    before_json: str
    after_json: str
    amount: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "action_id": self.action_id,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "session_date": self.session_date.isoformat(),
            "before_json": json.loads(self.before_json) if self.before_json else {},
            "after_json": json.loads(self.after_json) if self.after_json else {},
            "amount": decimal_to_str(self.amount),
        }


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    exchange: str
    board: str
    lot_size: int
    price_tick: Decimal
    price_limit_rule: Decimal
    is_st: bool
    listing_date: date
    delisting_date: date | None
    settlement_rule: str = "T+1"
    allow_odd_lot_sell: bool = True
    metadata_version: str = "fixture-rules-v1"

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")
        if not (Decimal("0") < self.price_limit_rule < Decimal("1")):
            raise ValueError("price_limit_rule must be in (0, 1)")


@dataclass(frozen=True)
class FeeConfig:
    buy_commission_rate: Decimal = Decimal("0.0003")
    sell_commission_rate: Decimal = Decimal("0.0003")
    minimum_commission: Decimal = Decimal("5")
    sell_tax_rate: Decimal = Decimal("0.001")
    transfer_fee_rate: Decimal = Decimal("0")
    other_buy_fee_rate: Decimal = Decimal("0")
    other_sell_fee_rate: Decimal = Decimal("0")
    dividend_tax_rate: Decimal = Decimal("0")


@dataclass(frozen=True)
class SlippageConfig:
    buy_slippage_bps: Decimal = Decimal("5")
    sell_slippage_bps: Decimal = Decimal("5")
    max_volume_participation_rate: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class DailyBar:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    suspended: bool = False


@dataclass(frozen=True)
class BacktestEvent:
    event_id: str
    event_type: str
    session_date: date
    sequence_number: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_date": self.session_date.isoformat(),
            "sequence_number": self.sequence_number,
            "payload": self.payload,
        }


@dataclass
class BacktestOrder:
    backtest_order_id: str
    run_id: str
    symbol: str
    side: str
    order_type: str
    quantity: int
    remaining_quantity: int
    limit_price: Decimal | None
    created_session: date
    earliest_execution_session: date
    expiry_session: date
    status: str = BacktestOrderStatus.PENDING.value
    rejection_reason: str = ""
    source_signal_identity: str = ""
    risk_decision_id: str = ""
    corporate_action_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ["limit_price"]:
            payload[key] = None if payload[key] is None else decimal_to_str(payload[key])
        for key in ["created_session", "earliest_execution_session", "expiry_session"]:
            payload[key] = payload[key].isoformat()
        return payload


@dataclass(frozen=True)
class BacktestFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    raw_price: Decimal
    execution_price: Decimal
    trade_value: Decimal
    commission: Decimal
    tax: Decimal
    other_fees: Decimal
    slippage_cost: Decimal
    session_date: date

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, Decimal):
                payload[key] = decimal_to_str(value)
            if isinstance(value, date):
                payload[key] = value.isoformat()
        return payload


@dataclass
class BacktestPosition:
    symbol: str
    total_quantity: int = 0
    available_quantity: int = 0
    locked_quantity: int = 0
    today_bought_quantity: int = 0
    average_cost: Decimal = Decimal("0")
    last_price: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")

    def release_t1(self) -> None:
        self.available_quantity += self.today_bought_quantity
        self.today_bought_quantity = 0

    def release_locked(self, quantity: int) -> None:
        release = min(quantity, self.locked_quantity)
        self.locked_quantity -= release
        self.available_quantity += release

    def mark(self, price: Decimal) -> None:
        self.last_price = price
        self.market_value = money(price * Decimal(self.total_quantity))
        self.unrealized_pnl = money((price - self.average_cost) * Decimal(self.total_quantity))

    def buy(self, quantity: int, total_cost: Decimal, price: Decimal) -> None:
        if quantity <= 0:
            raise BacktestError("buy quantity must be positive")
        new_quantity = self.total_quantity + quantity
        weighted_cost = self.average_cost * Decimal(self.total_quantity) + total_cost
        self.average_cost = price_tick_round(weighted_cost / Decimal(new_quantity), Decimal("0.0001"))
        self.total_quantity = new_quantity
        self.today_bought_quantity += quantity
        self.mark(price)

    def add_bonus_shares(self, quantity: int, *, locked: bool, price: Decimal) -> None:
        if quantity <= 0:
            return
        total_cost = self.average_cost * Decimal(self.total_quantity)
        self.total_quantity += quantity
        if locked:
            self.locked_quantity += quantity
        else:
            self.available_quantity += quantity
        self.average_cost = Decimal("0") if self.total_quantity == 0 else price_tick_round(total_cost / Decimal(self.total_quantity), Decimal("0.0001"))
        self.mark(price)

    def rescale_quantity(self, new_quantity: int, *, price: Decimal) -> None:
        if new_quantity < 0:
            raise BacktestError("rescaled quantity must not be negative")
        total_cost = self.average_cost * Decimal(self.total_quantity)
        ratio = Decimal("0") if self.total_quantity == 0 else Decimal(new_quantity) / Decimal(self.total_quantity)
        self.total_quantity = new_quantity
        self.available_quantity = min(new_quantity, int(Decimal(self.available_quantity) * ratio))
        self.locked_quantity = min(new_quantity - self.available_quantity, int(Decimal(self.locked_quantity) * ratio))
        self.today_bought_quantity = max(0, new_quantity - self.available_quantity - self.locked_quantity)
        self.average_cost = Decimal("0") if new_quantity == 0 else price_tick_round(total_cost / Decimal(new_quantity), Decimal("0.0001"))
        self.mark(price)

    def sell(self, quantity: int, proceeds_after_fee: Decimal, price: Decimal) -> None:
        if quantity <= 0 or quantity > self.available_quantity or quantity > self.total_quantity:
            raise BacktestError("sell quantity exceeds available position")
        cost_basis = self.average_cost * Decimal(quantity)
        self.realized_pnl += money(proceeds_after_fee - cost_basis)
        self.total_quantity -= quantity
        self.available_quantity -= quantity
        if self.total_quantity == 0:
            self.average_cost = Decimal("0")
        self.mark(price if self.total_quantity else Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_quantity": self.total_quantity,
            "available_quantity": self.available_quantity,
            "locked_quantity": self.locked_quantity,
            "today_bought_quantity": self.today_bought_quantity,
            "average_cost": decimal_to_str(self.average_cost),
            "last_price": decimal_to_str(self.last_price),
            "market_value": decimal_to_str(self.market_value),
            "realized_pnl": decimal_to_str(self.realized_pnl),
            "unrealized_pnl": decimal_to_str(self.unrealized_pnl),
        }


@dataclass(frozen=True)
class CashLedgerEntry:
    session_date: date
    reason: str
    amount: Decimal
    cash_after: Decimal


@dataclass
class Portfolio:
    cash_available: Decimal
    positions: dict[str, BacktestPosition] = field(default_factory=dict)
    cash_frozen: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    taxes_paid: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    gross_dividend_income: Decimal = Decimal("0")
    dividend_tax: Decimal = Decimal("0")
    net_dividend_income: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    ledger: list[CashLedgerEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity = self.cash_available

    @property
    def total_market_value(self) -> Decimal:
        return money(sum((position.market_value for position in self.positions.values()), Decimal("0")))

    @property
    def total_equity(self) -> Decimal:
        return money(self.cash_available + self.cash_frozen + self.total_market_value)

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal("0")
        return ((self.peak_equity - self.total_equity) / self.peak_equity).quantize(Decimal("0.000001"))

    def position(self, symbol: str) -> BacktestPosition:
        if symbol not in self.positions:
            self.positions[symbol] = BacktestPosition(symbol=symbol)
        return self.positions[symbol]

    def release_t1(self) -> None:
        for position in self.positions.values():
            position.release_t1()

    def release_locked_shares(self, symbol: str, quantity: int) -> None:
        self.position(symbol).release_locked(quantity)

    def apply_fill(self, fill: BacktestFill) -> None:
        position = self.position(fill.symbol)
        if fill.side == BacktestSide.BUY.value:
            cash_delta = -(fill.trade_value + fill.commission + fill.other_fees)
            self._cash(fill.session_date, "BUY_TRADE_VALUE", -fill.trade_value)
            self._cash(fill.session_date, "BUY_COMMISSION", -fill.commission)
            self._cash(fill.session_date, "BUY_OTHER_FEES", -fill.other_fees)
            if self.cash_available < 0:
                raise BacktestError("cash_available became negative")
            position.buy(fill.quantity, -cash_delta, fill.execution_price)
        else:
            proceeds = fill.trade_value - fill.commission - fill.tax - fill.other_fees
            position.sell(fill.quantity, proceeds, fill.execution_price)
            self._cash(fill.session_date, "SELL_TRADE_VALUE", fill.trade_value)
            self._cash(fill.session_date, "SELL_COMMISSION", -fill.commission)
            self._cash(fill.session_date, "SELL_TAX", -fill.tax)
            self._cash(fill.session_date, "SELL_OTHER_FEES", -fill.other_fees)
        self.fees_paid += fill.commission + fill.other_fees
        self.taxes_paid += fill.tax
        self.slippage_cost += fill.slippage_cost
        self.realized_pnl = money(sum((p.realized_pnl for p in self.positions.values()), Decimal("0")))
        self._assert_valid()

    def receive_dividend(self, entitlement: DividendEntitlement) -> None:
        self._cash(entitlement.payment_date, "DIVIDEND_CASH_RECEIVED", entitlement.net_cash)
        self.gross_dividend_income += entitlement.gross_cash
        self.dividend_tax += entitlement.tax
        self.net_dividend_income += entitlement.net_cash
        self.taxes_paid += entitlement.tax
        self._assert_valid()

    def mark_to_market(self, prices: dict[str, Decimal]) -> None:
        for symbol, position in self.positions.items():
            if position.total_quantity > 0:
                position.mark(prices.get(symbol, position.last_price))
        self.unrealized_pnl = money(sum((p.unrealized_pnl for p in self.positions.values()), Decimal("0")))
        if self.total_equity > self.peak_equity:
            self.peak_equity = self.total_equity
        self._assert_valid()

    def to_account_snapshot(self, session_date: date) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="backtest",
            as_of=datetime.combine(session_date, time(15, 0), tzinfo=TZ),
            total_equity=max(self.total_equity, Decimal("0.01")),
            available_cash=max(self.cash_available, Decimal("0")),
            market_value=self.total_market_value,
            frozen_cash=self.cash_frozen,
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=Decimal("0"),
            peak_equity=max(self.peak_equity, self.total_equity, Decimal("0.01")),
            consecutive_losses=0,
            positions=tuple(
                PositionSnapshot(
                    symbol=position.symbol,
                    quantity=position.total_quantity,
                    available_quantity=position.available_quantity,
                    average_cost=position.average_cost,
                    current_price=position.last_price,
                    market_value=position.market_value,
                )
                for position in self.positions.values()
                if position.total_quantity > 0
            ),
        )

    def _cash(self, session_date: date, reason: str, amount: Decimal) -> None:
        self.cash_available = money(self.cash_available + amount)
        self.ledger.append(CashLedgerEntry(session_date, reason, money(amount), self.cash_available))

    def _assert_valid(self) -> None:
        if self.cash_available < 0 or self.cash_frozen < 0:
            raise BacktestError("portfolio cash is negative")
        for position in self.positions.values():
            if position.total_quantity < 0 or position.available_quantity < 0:
                raise BacktestError("portfolio position quantity is negative")
            if position.locked_quantity < 0 or position.today_bought_quantity < 0:
                raise BacktestError("portfolio locked or today quantity is negative")
            if position.available_quantity > position.total_quantity:
                raise BacktestError("available quantity exceeds total quantity")
            if position.available_quantity + position.locked_quantity + position.today_bought_quantity > position.total_quantity:
                raise BacktestError("position quantity buckets exceed total quantity")


@dataclass(frozen=True)
class BacktestDailyEquity:
    session_date: date
    cash: Decimal
    market_value: Decimal
    total_equity: Decimal
    daily_return: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    exposure: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "cash": decimal_to_str(self.cash),
            "market_value": decimal_to_str(self.market_value),
            "total_equity": decimal_to_str(self.total_equity),
            "daily_return": decimal_to_str(self.daily_return),
            "peak_equity": decimal_to_str(self.peak_equity),
            "drawdown": decimal_to_str(self.drawdown),
            "exposure": decimal_to_str(self.exposure),
        }


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    status: str
    config_checksum: str
    strategy_name: str
    strategy_version: str
    parameter_version: str
    calendar_version: str
    instrument_rules_version: str
    corporate_action_version: str
    data_checksums: dict[str, str]
    code_version: str
    initial_cash: Decimal
    final_equity: Decimal
    total_return: Decimal
    annualized_return: Decimal | None
    max_drawdown: Decimal
    max_drawdown_start: date | None
    max_drawdown_end: date | None
    recovery_date: date | None
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    calmar_ratio: Decimal | None
    trade_count: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    payoff_ratio: Decimal | None
    profit_factor: Decimal | None
    max_consecutive_wins: int
    max_consecutive_losses: int
    turnover: Decimal
    average_exposure: Decimal
    average_holding_sessions: Decimal | None
    total_commission: Decimal
    total_tax: Decimal
    total_other_fees: Decimal
    total_slippage_cost: Decimal
    gross_dividend_income: Decimal
    dividend_tax: Decimal
    net_dividend_income: Decimal
    stock_dividend_events: int
    capitalization_events: int
    split_events: int
    rights_issue_events: int
    cancelled_by_corporate_action_count: int
    result_quality: str
    corporate_action_limitations: list[str]
    blocked_t1_count: int
    blocked_suspension_count: int
    blocked_price_limit_count: int
    blocked_liquidity_count: int
    same_bar_conflict_count: int
    notice: str
    limitations: list[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = decimal_to_str(value)
            elif isinstance(value, date):
                payload[key] = value.isoformat()
        return payload


@dataclass
class BacktestRun:
    run_id: str
    config: BacktestConfig
    result: BacktestResult
    events: list[BacktestEvent]
    orders: list[BacktestOrder]
    fills: list[BacktestFill]
    corporate_actions: tuple[CorporateAction, ...]
    dividend_entitlements: list[DividendEntitlement]
    corporate_action_events: list[CorporateActionLedgerEvent]
    daily_equity: list[BacktestDailyEquity]
    positions_by_day: dict[date, list[BacktestPosition]]
    status: str = "COMPLETED"
    error_message: str = ""


class FeeModel:
    def __init__(self, config: FeeConfig) -> None:
        self.config = config

    def calculate(self, *, side: str, trade_value: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        rate = self.config.buy_commission_rate if side == BacktestSide.BUY.value else self.config.sell_commission_rate
        commission = max(money(trade_value * rate), self.config.minimum_commission)
        tax = money(trade_value * self.config.sell_tax_rate) if side == BacktestSide.SELL.value else Decimal("0.00")
        transfer = money(trade_value * self.config.transfer_fee_rate)
        other_rate = self.config.other_buy_fee_rate if side == BacktestSide.BUY.value else self.config.other_sell_fee_rate
        other = money(trade_value * other_rate) + transfer
        return commission, tax, other


class SlippageModel:
    def __init__(self, config: SlippageConfig) -> None:
        self.config = config

    def apply(self, *, side: str, raw_price: Decimal, rules: InstrumentRules) -> tuple[Decimal, Decimal]:
        bps = self.config.buy_slippage_bps if side == BacktestSide.BUY.value else self.config.sell_slippage_bps
        signed = raw_price * bps / Decimal("10000")
        slipped = raw_price + signed if side == BacktestSide.BUY.value else raw_price - signed
        execution = legal_price(slipped, rules, side=side)
        slippage = abs(execution - raw_price)
        return execution, money(slippage)


class MatchingEngine:
    def __init__(self, fee_model: FeeModel, slippage_model: SlippageModel) -> None:
        self.fee_model = fee_model
        self.slippage_model = slippage_model

    def execute(
        self,
        *,
        order: BacktestOrder,
        bar: DailyBar,
        previous_close: Decimal,
        rules: InstrumentRules,
        portfolio: Portfolio,
        run_id: str,
    ) -> BacktestFill | None:
        if order.status != BacktestOrderStatus.PENDING.value:
            return None
        if bar.session_date < order.earliest_execution_session:
            return None
        if bar.session_date > order.expiry_session:
            order.status = BacktestOrderStatus.EXPIRED.value
            order.rejection_reason = "order expired"
            return None
        if bar.suspended or bar.volume <= 0:
            order.status = BacktestOrderStatus.BLOCKED_SUSPENSION.value
            order.rejection_reason = "suspended"
            return None
        if _blocked_by_price_limit(order.side, bar, previous_close, rules):
            order.status = BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value
            order.rejection_reason = "price limit"
            return None
        raw_price = self._raw_price(order, bar)
        if raw_price is None:
            return None
        max_quantity = int(Decimal(bar.volume) * self.slippage_model.config.max_volume_participation_rate)
        max_quantity = floor_to_lot(max_quantity, rules.lot_size)
        fill_quantity = min(order.remaining_quantity, max_quantity)
        fill_quantity = floor_to_lot(fill_quantity, rules.lot_size) if order.side == BacktestSide.BUY.value else fill_quantity
        if fill_quantity <= 0:
            order.status = BacktestOrderStatus.BLOCKED_LIQUIDITY.value
            order.rejection_reason = "liquidity"
            return None
        if order.side == BacktestSide.SELL.value:
            position = portfolio.position(order.symbol)
            if position.available_quantity <= 0:
                order.status = BacktestOrderStatus.BLOCKED_T1.value
                order.rejection_reason = "T+1 available quantity is zero"
                return None
            fill_quantity = min(fill_quantity, position.available_quantity)
        execution_price, slippage_per_share = self.slippage_model.apply(side=order.side, raw_price=raw_price, rules=rules)
        trade_value = money(execution_price * Decimal(fill_quantity))
        commission, tax, other = self.fee_model.calculate(side=order.side, trade_value=trade_value)
        if order.side == BacktestSide.BUY.value:
            total_cost = trade_value + commission + other
            affordable = floor_to_lot(int(portfolio.cash_available / execution_price), rules.lot_size)
            fill_quantity = min(fill_quantity, affordable)
            if fill_quantity < rules.lot_size:
                order.status = BacktestOrderStatus.REJECTED.value
                order.rejection_reason = "cash below one lot"
                return None
            trade_value = money(execution_price * Decimal(fill_quantity))
            commission, tax, other = self.fee_model.calculate(side=order.side, trade_value=trade_value)
            total_cost = trade_value + commission + other
            if total_cost > portfolio.cash_available:
                fill_quantity = floor_to_lot(fill_quantity - rules.lot_size, rules.lot_size)
            if fill_quantity < rules.lot_size:
                order.status = BacktestOrderStatus.REJECTED.value
                order.rejection_reason = "cash insufficient after fees"
                return None
            trade_value = money(execution_price * Decimal(fill_quantity))
            commission, tax, other = self.fee_model.calculate(side=order.side, trade_value=trade_value)
        slippage_cost = money(slippage_per_share * Decimal(fill_quantity))
        order.remaining_quantity -= fill_quantity
        order.status = (
            BacktestOrderStatus.FILLED.value
            if order.remaining_quantity == 0
            else BacktestOrderStatus.PARTIALLY_FILLED.value
        )
        return BacktestFill(
            fill_id=stable_id("fill", run_id, order.backtest_order_id, bar.session_date.isoformat(), str(fill_quantity)),
            order_id=order.backtest_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_quantity,
            raw_price=raw_price,
            execution_price=execution_price,
            trade_value=trade_value,
            commission=commission,
            tax=tax,
            other_fees=other,
            slippage_cost=slippage_cost,
            session_date=bar.session_date,
        )

    def _raw_price(self, order: BacktestOrder, bar: DailyBar) -> Decimal | None:
        if order.order_type == BacktestOrderType.MARKET_ON_NEXT_OPEN.value:
            return bar.open
        if order.limit_price is None:
            order.status = BacktestOrderStatus.REJECTED.value
            order.rejection_reason = "missing limit_price"
            return None
        if order.side == BacktestSide.BUY.value and bar.low <= order.limit_price:
            return order.limit_price
        if order.side == BacktestSide.SELL.value and bar.high >= order.limit_price:
            return order.limit_price
        return None


class BacktestEngine:
    def __init__(
        self,
        *,
        config: BacktestConfig,
        calendar: LocalTradingCalendar,
        market_data: dict[str, pd.DataFrame],
        instrument_rules: dict[str, InstrumentRules],
        strategy_config: StrategyConfig,
        risk_policy: RiskPolicy | None = None,
        fee_config: FeeConfig | None = None,
        slippage_config: SlippageConfig | None = None,
        corporate_actions: tuple[CorporateAction, ...] = tuple(),
        signal_func: Callable[..., Signal] = generate_signal,
        risk_engine: RiskEngine | None = None,
        persist: bool = False,
    ) -> None:
        self.config = config
        self.calendar = calendar
        self.market_data = {symbol: _normalize_bars(df) for symbol, df in market_data.items()}
        self.instrument_rules = instrument_rules
        self.strategy_config = strategy_config
        self.risk_policy = risk_policy or RiskPolicy()
        self.fee_config = fee_config or FeeConfig()
        self.signal_func = signal_func
        self.risk_engine = risk_engine or RiskEngine()
        self.matching = MatchingEngine(
            FeeModel(self.fee_config),
            SlippageModel(slippage_config or SlippageConfig(max_volume_participation_rate=config.volume_participation_rate)),
        )
        self.corporate_actions = tuple(sorted(corporate_actions, key=lambda item: (item.ex_date, item.action_id)))
        self.persist = persist
        self.events: list[BacktestEvent] = []
        self.orders: list[BacktestOrder] = []
        self.fills: list[BacktestFill] = []
        self.dividend_entitlements: list[DividendEntitlement] = []
        self.corporate_action_events: list[CorporateActionLedgerEvent] = []
        self.daily_equity: list[BacktestDailyEquity] = []
        self.positions_by_day: dict[date, list[BacktestPosition]] = {}
        self.same_bar_conflict_count = 0
        self.data_checksums = {symbol: market_data_checksum(df) for symbol, df in self.market_data.items()}
        self.corporate_action_checksum = corporate_action_checksum(self.corporate_actions)
        self.run_id = stable_id("bt", self.config.config_checksum, stable_json(self.data_checksums), self.corporate_action_checksum)

    def run(self) -> BacktestRun:
        portfolio = Portfolio(cash_available=self.config.initial_cash)
        previous_equity = portfolio.total_equity
        try:
            _validate_inputs(self.config, self.calendar, self.market_data, self.instrument_rules)
            sessions = self.calendar.trading_days(self.config.start_date, self.config.end_date)
            for session in sessions:
                self._event(EventType.SESSION_START, session)
                self._process_corporate_actions(session, portfolio)
                portfolio.release_t1()
                self._release_corporate_locked_shares(session, portfolio)
                self._pay_dividends(session, portfolio)
                self._cancel_orders_for_corporate_actions(session)
                bars = self._bars_for_session(session)
                previous_closes = self._previous_closes(session)
                self._event(EventType.MARKET_OPEN, session)
                self._execute_open_orders(session, bars, previous_closes, portfolio)
                self._event(EventType.BAR, session)
                self._process_exits(session, bars, previous_closes, portfolio)
                self._event(EventType.SESSION_CLOSE, session)
                portfolio.mark_to_market({symbol: bar.close for symbol, bar in bars.items()})
                self._generate_next_session_orders(session, portfolio)
                self._event(EventType.SETTLEMENT, session)
                equity = portfolio.total_equity
                daily_return = Decimal("0") if previous_equity == 0 else (equity - previous_equity) / previous_equity
                exposure = Decimal("0") if equity == 0 else portfolio.total_market_value / equity
                self.daily_equity.append(
                    BacktestDailyEquity(
                        session_date=session,
                        cash=portfolio.cash_available,
                        market_value=portfolio.total_market_value,
                        total_equity=equity,
                        daily_return=daily_return.quantize(Decimal("0.000001")),
                        peak_equity=portfolio.peak_equity,
                        drawdown=portfolio.drawdown,
                        exposure=exposure.quantize(Decimal("0.000001")),
                    )
                )
                self.positions_by_day[session] = [replace(position) for position in portfolio.positions.values()]
                previous_equity = equity
            result = self._result(portfolio, status="COMPLETED")
            run = BacktestRun(
                run_id=self.run_id,
                config=self.config,
                result=result,
                events=self.events,
                orders=self.orders,
                fills=self.fills,
                corporate_actions=self.corporate_actions,
                dividend_entitlements=self.dividend_entitlements,
                corporate_action_events=self.corporate_action_events,
                daily_equity=self.daily_equity,
                positions_by_day=self.positions_by_day,
            )
            if self.persist:
                save_backtest_run(run)
            return run
        except Exception as exc:
            result = self._result(portfolio, status="FAILED")
            failed_run = BacktestRun(
                run_id=self.run_id,
                config=self.config,
                result=result,
                events=self.events,
                orders=self.orders,
                fills=self.fills,
                corporate_actions=self.corporate_actions,
                dividend_entitlements=self.dividend_entitlements,
                corporate_action_events=self.corporate_action_events,
                daily_equity=self.daily_equity,
                positions_by_day=self.positions_by_day,
                status="FAILED",
                error_message=str(exc),
            )
            if self.persist:
                save_backtest_run(failed_run)
            raise

    def _event(self, event_type: EventType, session: date, payload: dict[str, Any] | None = None) -> None:
        sequence = len([event for event in self.events if event.session_date == session]) + 1
        self.events.append(
            BacktestEvent(
                event_id=stable_id("evt", self.run_id, session.isoformat(), str(sequence), event_type.value),
                event_type=event_type.value,
                session_date=session,
                sequence_number=sequence,
                payload=payload or {},
            )
        )

    def _process_corporate_actions(self, session: date, portfolio: Portfolio) -> None:
        for action in self.corporate_actions:
            if action.record_date == session and action.action_type == CorporateActionType.CASH_DIVIDEND.value:
                self._create_dividend_entitlement(action, session, portfolio)
            if action.ex_date != session:
                continue
            self._event(EventType.CORPORATE_ACTION, session, {"action_id": action.action_id, "action_type": action.action_type})
            if action.action_type == CorporateActionType.RIGHTS_ISSUE.value:
                if self.config.rights_issue_policy == RightsIssuePolicy.FAIL_CLOSED.value:
                    raise BacktestError(f"rights issue requires explicit policy: {action.action_id}")
                self._record_corporate_event(action, session, "RIGHTS_SUBSCRIPTION", {}, {"policy": self.config.rights_issue_policy})
                continue
            if action.action_type == CorporateActionType.DELISTING.value:
                if any(p.total_quantity > 0 for p in portfolio.positions.values() if p.symbol == action.symbol):
                    raise BacktestError(f"delisting settlement is unsupported: {action.action_id}")
                self._cancel_symbol_orders(action.symbol, action.action_id, session)
                continue
            if action.action_type == CorporateActionType.SYMBOL_CHANGE.value:
                self._apply_symbol_change(action, session, portfolio)
                continue
            if action.action_type in {
                CorporateActionType.STOCK_DIVIDEND.value,
                CorporateActionType.CAPITALIZATION.value,
                CorporateActionType.SPLIT.value,
                CorporateActionType.REVERSE_SPLIT.value,
            }:
                self._apply_quantity_action(action, session, portfolio)

    def _create_dividend_entitlement(self, action: CorporateAction, session: date, portfolio: Portfolio) -> None:
        position = portfolio.positions.get(action.symbol)
        eligible = 0 if position is None else position.total_quantity
        if eligible <= 0:
            return
        if any(item.action_id == action.action_id for item in self.dividend_entitlements):
            return
        assert action.cash_per_share is not None
        gross = money(Decimal(eligible) * action.cash_per_share)
        tax = money(gross * self.fee_config.dividend_tax_rate)
        net = money(gross - tax)
        payment_date = action.payment_date or action.ex_date
        entitlement = DividendEntitlement(
            entitlement_id=stable_id("div", self.run_id, action.action_id, str(eligible)),
            action_id=action.action_id,
            symbol=action.symbol,
            eligible_quantity=eligible,
            gross_cash=gross,
            tax=tax,
            net_cash=net,
            record_date=session,
            payment_date=payment_date,
        )
        self.dividend_entitlements.append(entitlement)
        self._record_corporate_event(action, session, "DIVIDEND_ENTITLEMENT_CREATED", {}, entitlement.to_dict(), gross)

    def _pay_dividends(self, session: date, portfolio: Portfolio) -> None:
        for entitlement in self.dividend_entitlements:
            if entitlement.payment_date != session or entitlement.status == DividendEntitlementStatus.PAID.value:
                continue
            before = {"cash": decimal_to_str(portfolio.cash_available)}
            portfolio.receive_dividend(entitlement)
            entitlement.status = DividendEntitlementStatus.PAID.value
            after = {"cash": decimal_to_str(portfolio.cash_available), "entitlement": entitlement.to_dict()}
            action = self._action_by_id(entitlement.action_id)
            self._record_corporate_event(action, session, "DIVIDEND_CASH_RECEIVED", before, after, entitlement.net_cash)

    def _release_corporate_locked_shares(self, session: date, portfolio: Portfolio) -> None:
        for action in self.corporate_actions:
            if action.tradable_date != session:
                continue
            position = portfolio.positions.get(action.symbol)
            if position is None or position.locked_quantity <= 0:
                continue
            before = position.to_dict()
            quantity = position.locked_quantity
            portfolio.release_locked_shares(action.symbol, quantity)
            self._record_corporate_event(action, session, "SHARES_RELEASED", before, position.to_dict())

    def _apply_quantity_action(self, action: CorporateAction, session: date, portfolio: Portfolio) -> None:
        position = portfolio.positions.get(action.symbol)
        if position is None or position.total_quantity <= 0:
            return
        before = position.to_dict()
        price = self._action_valuation_price(action.symbol, session)
        if action.action_type == CorporateActionType.STOCK_DIVIDEND.value:
            assert action.stock_ratio is not None
            added = int(Decimal(position.total_quantity) * action.stock_ratio)
            position.add_bonus_shares(added, locked=(action.tradable_date or session) > session, price=price)
        elif action.action_type == CorporateActionType.CAPITALIZATION.value:
            assert action.capitalization_ratio is not None
            added = int(Decimal(position.total_quantity) * action.capitalization_ratio)
            position.add_bonus_shares(added, locked=(action.tradable_date or session) > session, price=price)
        elif action.action_type == CorporateActionType.SPLIT.value:
            assert action.stock_ratio is not None
            position.rescale_quantity(int(Decimal(position.total_quantity) * action.stock_ratio), price=price)
        elif action.action_type == CorporateActionType.REVERSE_SPLIT.value:
            assert action.stock_ratio is not None and action.stock_ratio > 0
            position.rescale_quantity(int(Decimal(position.total_quantity) / action.stock_ratio), price=price)
        after = position.to_dict()
        self._record_corporate_event(action, session, "STOCK_QUANTITY_ADJUSTED", before, after)
        self._record_corporate_event(action, session, "COST_BASIS_ADJUSTED", before, after)
        if position.locked_quantity > 0:
            self._record_corporate_event(action, session, "SHARES_LOCKED", before, after)

    def _apply_symbol_change(self, action: CorporateAction, session: date, portfolio: Portfolio) -> None:
        if not action.new_symbol:
            raise BacktestError(f"symbol change missing mapping: {action.action_id}")
        position = portfolio.positions.pop(action.symbol, None)
        if position is not None:
            before = position.to_dict()
            position.symbol = action.new_symbol
            portfolio.positions[action.new_symbol] = position
            self._record_corporate_event(action, session, "SYMBOL_CHANGED", before, position.to_dict())
        for order in self.orders:
            if order.symbol == action.symbol:
                order.symbol = action.new_symbol

    def _cancel_orders_for_corporate_actions(self, session: date) -> None:
        for action in self.corporate_actions:
            if action.ex_date == session and action.action_type in {
                CorporateActionType.CASH_DIVIDEND.value,
                CorporateActionType.STOCK_DIVIDEND.value,
                CorporateActionType.CAPITALIZATION.value,
                CorporateActionType.RIGHTS_ISSUE.value,
                CorporateActionType.SPLIT.value,
                CorporateActionType.REVERSE_SPLIT.value,
                CorporateActionType.DELISTING.value,
            }:
                self._cancel_symbol_orders(action.symbol, action.action_id, session)

    def _cancel_symbol_orders(self, symbol: str, action_id: str, session: date) -> None:
        action = self._action_by_id(action_id)
        for order in self.orders:
            if order.symbol == symbol and order.status == BacktestOrderStatus.PENDING.value:
                before = order.to_dict()
                order.status = BacktestOrderStatus.CANCELLED_CORPORATE_ACTION.value
                order.rejection_reason = f"cancelled by corporate action {action_id}"
                order.corporate_action_id = action_id
                self._record_corporate_event(action, session, "ORDER_CANCELLED_CORPORATE_ACTION", before, order.to_dict())

    def _action_valuation_price(self, symbol: str, session: date) -> Decimal:
        df = self.market_data[symbol]
        rows = df[df["date"].dt.date == session]
        if rows.empty:
            return _previous_close(df, session)
        return Decimal(rows.iloc[0].close)

    def _record_corporate_event(
        self,
        action: CorporateAction,
        session: date,
        event_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
        amount: Decimal = Decimal("0"),
    ) -> None:
        self.corporate_action_events.append(
            CorporateActionLedgerEvent(
                run_id=self.run_id,
                action_id=action.action_id,
                symbol=action.symbol,
                event_type=event_type,
                session_date=session,
                before_json=stable_json(before),
                after_json=stable_json(after),
                amount=money(amount),
            )
        )

    def _action_by_id(self, action_id: str) -> CorporateAction:
        for action in self.corporate_actions:
            if action.action_id == action_id:
                return action
        raise BacktestError(f"unknown corporate action: {action_id}")

    def _bars_for_session(self, session: date) -> dict[str, DailyBar]:
        return {symbol: _bar_at(df, session) for symbol, df in self.market_data.items()}

    def _previous_closes(self, session: date) -> dict[str, Decimal]:
        return {symbol: _previous_close(df, session) for symbol, df in self.market_data.items()}

    def _execute_open_orders(
        self,
        session: date,
        bars: dict[str, DailyBar],
        previous_closes: dict[str, Decimal],
        portfolio: Portfolio,
    ) -> None:
        for order in list(self.orders):
            if order.status != BacktestOrderStatus.PENDING.value:
                continue
            fill = self.matching.execute(
                order=order,
                bar=bars[order.symbol],
                previous_close=previous_closes[order.symbol],
                rules=self.instrument_rules[order.symbol],
                portfolio=portfolio,
                run_id=self.run_id,
            )
            if fill is not None:
                portfolio.apply_fill(fill)
                self.fills.append(fill)
                self._event(EventType.FILL, session, {"fill_id": fill.fill_id, "order_id": order.backtest_order_id})

    def _process_exits(
        self,
        session: date,
        bars: dict[str, DailyBar],
        previous_closes: dict[str, Decimal],
        portfolio: Portfolio,
    ) -> None:
        for symbol, position in list(portfolio.positions.items()):
            if position.total_quantity <= 0:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue
            stop_price = money(position.average_cost * Decimal("0.95"))
            take_profit_price = money(position.average_cost * Decimal("1.05"))
            stop_hit = bar.low <= stop_price
            take_hit = bar.high >= take_profit_price
            if not stop_hit and not take_hit:
                continue
            if stop_hit and take_hit:
                self.same_bar_conflict_count += 1
            limit_price = stop_price if stop_hit else take_profit_price
            if position.available_quantity <= 0:
                order = self._new_order(
                    symbol=symbol,
                    side=BacktestSide.SELL,
                    quantity=position.total_quantity,
                    order_type=BacktestOrderType.LIMIT,
                    created_session=session,
                    earliest_execution_session=session,
                    expiry_session=session,
                    limit_price=limit_price,
                    source_signal_identity="exit_rule",
                    risk_decision_id="exit_rule",
                )
                order.status = BacktestOrderStatus.BLOCKED_T1.value
                order.rejection_reason = "T+1 blocks same-day exit"
                self.orders.append(order)
                continue
            order = self._new_order(
                symbol=symbol,
                side=BacktestSide.SELL,
                quantity=position.available_quantity,
                order_type=BacktestOrderType.LIMIT,
                created_session=session,
                earliest_execution_session=session,
                expiry_session=session,
                limit_price=limit_price,
                source_signal_identity="exit_rule",
                risk_decision_id="exit_rule",
            )
            self.orders.append(order)
            fill = self.matching.execute(
                order=order,
                bar=bar,
                previous_close=previous_closes[symbol],
                rules=self.instrument_rules[symbol],
                portfolio=portfolio,
                run_id=self.run_id,
            )
            if fill is not None:
                portfolio.apply_fill(fill)
                self.fills.append(fill)

    def _generate_next_session_orders(self, session: date, portfolio: Portfolio) -> None:
        try:
            next_session = self.calendar.next_trading_day(session)
        except Exception:
            return
        for symbol in self.config.symbols:
            if self._is_delisted(symbol, session):
                if portfolio.positions.get(symbol, BacktestPosition(symbol)).total_quantity > 0:
                    raise BacktestError(f"delisted symbol still held: {symbol}")
                continue
            history = self.market_data[symbol][self.market_data[symbol]["date"].dt.date <= session].copy()
            if history.empty or history["date"].dt.date.max() > session:
                raise BacktestError("future data leak detected")
            snapshot = MarketDataSnapshot(
                bars=history,
                provider="backtest_fixture",
                symbol=symbol,
                adjust=self.config.signal_price_adjust,
                first_date=history["date"].iloc[0].date(),
                last_date=session,
                row_count=len(history),
                fetched_at=datetime.combine(session, time(15, 0), tzinfo=TZ),
                validated_at=datetime.combine(session, time(15, 0), tzinfo=TZ),
                data_version="backtest_fixture:daily",
                calendar_version=self.calendar.version,
                data_checksum=market_data_checksum(history),
                expected_market_date=session,
                actual_market_date=session,
            )
            signal_kwargs = {
                "symbol": symbol,
                "market_data": snapshot,
                "account_equity": float(portfolio.total_equity),
                "risk_per_trade": float(self.risk_policy.risk_per_trade),
                "max_single_position_pct": float(self.risk_policy.max_symbol_weight),
                "config": self.strategy_config,
                "visible_corporate_actions": [
                    action for action in self.corporate_actions
                    if action.symbol == symbol and action.announcement_date <= session
                ],
            }
            try:
                signal = self.signal_func(**signal_kwargs)
            except TypeError:
                signal_kwargs.pop("visible_corporate_actions")
                signal = self.signal_func(**signal_kwargs)
            self._event(EventType.SIGNAL, session, {"symbol": symbol, "signal_type": signal.signal_type})
            if signal.signal_type == SignalType.DATA_ERROR.value:
                continue
            account = portfolio.to_account_snapshot(session)
            decision = self.risk_engine.evaluate(
                signal=signal,
                account=account,
                policy=self.risk_policy,
                reference_price=Decimal(str(signal.reference_price)),
                stop_price=Decimal(str(signal.stop_loss_price)),
            )
            decision = _deterministic_decision(decision, signal, account, self.risk_policy, session)
            self._event(EventType.RISK, session, {"status": decision.status, "symbol": symbol})
            if decision.status not in {RiskStatus.APPROVED.value, RiskStatus.REDUCED.value}:
                continue
            if decision.approved_quantity <= 0:
                continue
            identity = signal_identity(signal)
            if any(
                order.source_signal_identity == identity
                and order.status == BacktestOrderStatus.PENDING.value
                for order in self.orders
            ):
                continue
            quantity = floor_to_lot(decision.approved_quantity, self.instrument_rules[symbol].lot_size)
            if quantity < self.instrument_rules[symbol].lot_size:
                continue
            order = self._new_order(
                symbol=symbol,
                side=BacktestSide.BUY,
                quantity=quantity,
                order_type=BacktestOrderType.MARKET_ON_NEXT_OPEN,
                created_session=session,
                earliest_execution_session=next_session,
                expiry_session=_expiry_session(self.calendar, session, self.config.order_expiry_sessions),
                limit_price=None,
                source_signal_identity=identity,
                risk_decision_id=decision.decision_id,
            )
            self.orders.append(order)
            self._event(EventType.ORDER, session, {"order_id": order.backtest_order_id})

    def _is_delisted(self, symbol: str, session: date) -> bool:
        return any(
            action.symbol == symbol
            and action.action_type == CorporateActionType.DELISTING.value
            and action.ex_date <= session
            for action in self.corporate_actions
        )

    def _new_order(
        self,
        *,
        symbol: str,
        side: BacktestSide,
        quantity: int,
        order_type: BacktestOrderType,
        created_session: date,
        earliest_execution_session: date,
        expiry_session: date,
        limit_price: Decimal | None,
        source_signal_identity: str,
        risk_decision_id: str,
    ) -> BacktestOrder:
        return BacktestOrder(
            backtest_order_id=stable_id(
                "bto",
                self.run_id,
                symbol,
                side.value,
                order_type.value,
                str(quantity),
                created_session.isoformat(),
                source_signal_identity,
            ),
            run_id=self.run_id,
            symbol=symbol,
            side=side.value,
            order_type=order_type.value,
            quantity=quantity,
            remaining_quantity=quantity,
            limit_price=limit_price,
            created_session=created_session,
            earliest_execution_session=earliest_execution_session,
            expiry_session=expiry_session,
            source_signal_identity=source_signal_identity,
            risk_decision_id=risk_decision_id,
        )

    def _result(self, portfolio: Portfolio, *, status: str) -> BacktestResult:
        metrics = calculate_metrics(
            initial_cash=self.config.initial_cash,
            daily_equity=self.daily_equity,
            fills=self.fills,
            portfolio=portfolio,
            annualization_days=self.config.annualization_days,
            risk_free_rate=self.config.risk_free_rate,
        )
        blocked = _blocked_counts(self.orders)
        return BacktestResult(
            run_id=self.run_id,
            status=status,
            config_checksum=self.config.config_checksum,
            strategy_name=self.config.strategy_name,
            strategy_version=self.config.strategy_version,
            parameter_version=self.config.parameter_version,
            calendar_version=self.calendar.version,
            instrument_rules_version=_rules_version(self.instrument_rules),
            corporate_action_version=self.corporate_action_checksum[:16],
            data_checksums=self.data_checksums,
            code_version=code_version(),
            initial_cash=self.config.initial_cash,
            final_equity=metrics["final_equity"],
            total_return=metrics["total_return"],
            annualized_return=metrics["annualized_return"],
            max_drawdown=metrics["max_drawdown"],
            max_drawdown_start=metrics["max_drawdown_start"],
            max_drawdown_end=metrics["max_drawdown_end"],
            recovery_date=metrics["recovery_date"],
            sharpe_ratio=metrics["sharpe_ratio"],
            sortino_ratio=metrics["sortino_ratio"],
            calmar_ratio=metrics["calmar_ratio"],
            trade_count=len(self.fills),
            win_rate=metrics["win_rate"],
            average_win=metrics["average_win"],
            average_loss=metrics["average_loss"],
            payoff_ratio=metrics["payoff_ratio"],
            profit_factor=metrics["profit_factor"],
            max_consecutive_wins=metrics["max_consecutive_wins"],
            max_consecutive_losses=metrics["max_consecutive_losses"],
            turnover=metrics["turnover"],
            average_exposure=metrics["average_exposure"],
            average_holding_sessions=metrics["average_holding_sessions"],
            total_commission=sum((fill.commission for fill in self.fills), Decimal("0")),
            total_tax=sum((fill.tax for fill in self.fills), Decimal("0")),
            total_other_fees=sum((fill.other_fees for fill in self.fills), Decimal("0")),
            total_slippage_cost=sum((fill.slippage_cost for fill in self.fills), Decimal("0")),
            gross_dividend_income=portfolio.gross_dividend_income,
            dividend_tax=portfolio.dividend_tax,
            net_dividend_income=portfolio.net_dividend_income,
            stock_dividend_events=sum(1 for action in self.corporate_actions if action.action_type == CorporateActionType.STOCK_DIVIDEND.value),
            capitalization_events=sum(1 for action in self.corporate_actions if action.action_type == CorporateActionType.CAPITALIZATION.value),
            split_events=sum(1 for action in self.corporate_actions if action.action_type in {CorporateActionType.SPLIT.value, CorporateActionType.REVERSE_SPLIT.value}),
            rights_issue_events=sum(1 for action in self.corporate_actions if action.action_type == CorporateActionType.RIGHTS_ISSUE.value),
            cancelled_by_corporate_action_count=sum(1 for order in self.orders if order.status == BacktestOrderStatus.CANCELLED_CORPORATE_ACTION.value),
            result_quality=self._result_quality(),
            corporate_action_limitations=self._corporate_action_limitations(),
            blocked_t1_count=blocked[BacktestOrderStatus.BLOCKED_T1.value],
            blocked_suspension_count=blocked[BacktestOrderStatus.BLOCKED_SUSPENSION.value],
            blocked_price_limit_count=blocked[BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value],
            blocked_liquidity_count=blocked[BacktestOrderStatus.BLOCKED_LIQUIDITY.value],
            same_bar_conflict_count=self.same_bar_conflict_count,
            notice="历史模拟结果；历史表现不代表未来收益；不构成投资建议或真实委托。",
            limitations=[
                "成交和现金盈亏默认使用不复权原始价格。",
                "公司行为未完整建模，复权口径结果仅可用于研究。",
                "日线OHLC无法确定盘中先后顺序，同日止损止盈默认WORST_CASE。",
            ],
        )

    def _result_quality(self) -> str:
        if self.config.execution_price_adjust:
            return ResultQuality.RESEARCH_ONLY_ADJUSTED_PRICES.value
        incomplete = any(
            action.action_type in {CorporateActionType.RIGHTS_ISSUE.value, CorporateActionType.DELISTING.value}
            for action in self.corporate_actions
        )
        if incomplete:
            return ResultQuality.INCOMPLETE_CORPORATE_ACTIONS.value
        return ResultQuality.REALISTIC_WITH_MODELED_CORPORATE_ACTIONS.value

    def _corporate_action_limitations(self) -> list[str]:
        limitations = ["现金分红税费为配置化模型，未模拟个体持有期差异税率。"]
        if any(action.action_type == CorporateActionType.RIGHTS_ISSUE.value for action in self.corporate_actions):
            limitations.append("配股默认FAIL_CLOSED，未假设投资者必然足额认购。")
        if any(action.action_type == CorporateActionType.DELISTING.value for action in self.corporate_actions):
            limitations.append("退市结算未可靠建模，默认失败关闭。")
        return limitations


def calculate_metrics(
    *,
    initial_cash: Decimal,
    daily_equity: list[BacktestDailyEquity],
    fills: list[BacktestFill],
    portfolio: Portfolio,
    annualization_days: int,
    risk_free_rate: Decimal,
) -> dict[str, Any]:
    final_equity = daily_equity[-1].total_equity if daily_equity else initial_cash
    total_return = (final_equity - initial_cash) / initial_cash
    returns = [float(item.daily_return) for item in daily_equity]
    annualized_return = None
    if daily_equity:
        annualized_return = Decimal(str((float(final_equity / initial_cash) ** (annualization_days / len(daily_equity))) - 1)).quantize(Decimal("0.000001"))
    max_dd, dd_start, dd_end, recovery = _drawdown(daily_equity)
    sharpe = _sharpe(returns, annualization_days, float(risk_free_rate))
    downside = [value for value in returns if value < 0]
    sortino = _ratio(_mean(returns) * annualization_days - float(risk_free_rate), _std(downside) * math.sqrt(annualization_days))
    calmar = None if annualized_return is None or max_dd == 0 else (annualized_return / max_dd).quantize(Decimal("0.000001"))
    sell_pnls = [position.realized_pnl for position in portfolio.positions.values() if position.realized_pnl != 0]
    wins = [pnl for pnl in sell_pnls if pnl > 0]
    losses = [pnl for pnl in sell_pnls if pnl < 0]
    turnover = Decimal("0")
    if daily_equity:
        traded = sum((fill.trade_value for fill in fills), Decimal("0"))
        avg_equity = sum((item.total_equity for item in daily_equity), Decimal("0")) / Decimal(len(daily_equity))
        turnover = Decimal("0") if avg_equity == 0 else (traded / avg_equity).quantize(Decimal("0.000001"))
    return {
        "final_equity": final_equity,
        "total_return": total_return.quantize(Decimal("0.000001")),
        "annualized_return": annualized_return,
        "max_drawdown": max_dd,
        "max_drawdown_start": dd_start,
        "max_drawdown_end": dd_end,
        "recovery_date": recovery,
        "sharpe_ratio": _decimal_or_none(sharpe),
        "sortino_ratio": _decimal_or_none(sortino),
        "calmar_ratio": calmar,
        "win_rate": None if not sell_pnls else (Decimal(len(wins)) / Decimal(len(sell_pnls))).quantize(Decimal("0.000001")),
        "average_win": None if not wins else money(sum(wins, Decimal("0")) / Decimal(len(wins))),
        "average_loss": None if not losses else money(sum(losses, Decimal("0")) / Decimal(len(losses))),
        "payoff_ratio": None if not wins or not losses else abs((sum(wins, Decimal("0")) / Decimal(len(wins))) / (sum(losses, Decimal("0")) / Decimal(len(losses)))).quantize(Decimal("0.000001")),
        "profit_factor": None if not losses else abs(sum(wins, Decimal("0")) / sum(losses, Decimal("0"))).quantize(Decimal("0.000001")),
        "max_consecutive_wins": _max_streak(sell_pnls, positive=True),
        "max_consecutive_losses": _max_streak(sell_pnls, positive=False),
        "turnover": turnover,
        "average_exposure": Decimal("0") if not daily_equity else (sum((item.exposure for item in daily_equity), Decimal("0")) / Decimal(len(daily_equity))).quantize(Decimal("0.000001")),
        "average_holding_sessions": None,
    }


def save_backtest_run(run: BacktestRun) -> None:
    init_db()
    with SessionLocal() as session:
        existing = session.scalars(select(BacktestRunRecord).where(BacktestRunRecord.run_id == run.run_id)).first()
        if existing is not None:
            return
        session.add(
            BacktestRunRecord(
                run_id=run.run_id,
                status=run.status,
                config_json=stable_json(run.config.to_dict()),
                config_checksum=run.config.config_checksum,
                strategy_name=run.config.strategy_name,
                strategy_version=run.config.strategy_version,
                parameter_version=run.config.parameter_version,
                calendar_version=run.result.calendar_version,
                instrument_rules_version=run.result.instrument_rules_version,
                corporate_action_version=run.result.corporate_action_version,
                data_checksums_json=stable_json(run.result.data_checksums),
                code_version=run.result.code_version,
                started_at=datetime.combine(run.config.start_date, time(0, 0), tzinfo=TZ),
                completed_at=datetime.combine(run.config.end_date, time(23, 59), tzinfo=TZ),
                error_message=run.error_message,
                result_summary_json=stable_json(run.result.to_dict()),
            )
        )
        for order in run.orders:
            session.add(BacktestOrderRecord(**_order_record_payload(order)))
        for fill in run.fills:
            session.add(BacktestFillRecord(**_fill_record_payload(fill)))
        for item in run.daily_equity:
            session.add(BacktestDailyEquityRecord(run_id=run.run_id, **_daily_record_payload(item)))
        for session_date, positions in run.positions_by_day.items():
            for position in positions:
                session.add(BacktestPositionRecord(run_id=run.run_id, session_date=session_date.isoformat(), **_position_record_payload(position)))
        for action in getattr(run, "corporate_actions", tuple()):
            session.add(CorporateActionRecord(**_corporate_action_record_payload(action)))
        for entitlement in run.dividend_entitlements:
            session.add(DividendEntitlementRecord(run_id=run.run_id, **_entitlement_record_payload(entitlement)))
        for event in run.corporate_action_events:
            session.add(BacktestCorporateActionEventRecord(**_corporate_event_record_payload(event)))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def export_backtest_json(run: BacktestRun, path: str | Path) -> None:
    payload = {
        "run_id": run.run_id,
        "config": run.config.to_dict(),
        "result": run.result.to_dict(),
        "events": [event.to_dict() for event in run.events],
        "orders": [order.to_dict() for order in run.orders],
        "fills": [fill.to_dict() for fill in run.fills],
        "corporate_actions": [action.to_dict() for action in run.corporate_actions],
        "dividend_entitlements": [item.to_dict() for item in run.dividend_entitlements],
        "corporate_action_events": [event.to_dict() for event in run.corporate_action_events],
        "daily_equity": [item.to_dict() for item in run.daily_equity],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def export_backtest_csv(run: BacktestRun, directory: str | Path) -> None:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    _write_csv(target / "fills.csv", [fill.to_dict() for fill in run.fills])
    _write_csv(target / "daily_equity.csv", [item.to_dict() for item in run.daily_equity])
    _write_csv(target / "orders.csv", [order.to_dict() for order in run.orders])
    _write_csv(target / "corporate_action_events.csv", [event.to_dict() for event in run.corporate_action_events])


def load_backtest_bundle(path: str | Path) -> tuple[BacktestConfig, LocalTradingCalendar, dict[str, pd.DataFrame], dict[str, InstrumentRules], StrategyConfig, RiskPolicy, FeeConfig, SlippageConfig, tuple[CorporateAction, ...]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = BacktestConfig.from_dict(payload["config"])
    calendar_payload = payload["calendar"]
    calendar = LocalTradingCalendar(
        source=calendar_payload["source"],
        trading_day_set=frozenset(date.fromisoformat(value) for value in calendar_payload["trading_days"]),
        start_date=date.fromisoformat(calendar_payload["start_date"]),
        end_date=date.fromisoformat(calendar_payload["end_date"]),
        updated_at=datetime.fromisoformat(calendar_payload["updated_at"]),
        version=calendar_payload.get("version", ""),
    )
    data = {
        symbol: pd.DataFrame(rows).assign(date=lambda df: pd.to_datetime(df["date"]))
        for symbol, rows in payload["market_data"].items()
    }
    rules = {
        symbol: InstrumentRules(
            symbol=symbol,
            exchange=item["exchange"],
            board=item["board"],
            lot_size=int(item["lot_size"]),
            price_tick=Decimal(str(item["price_tick"])),
            price_limit_rule=Decimal(str(item["price_limit_rule"])),
            is_st=bool(item.get("is_st", False)),
            listing_date=date.fromisoformat(item["listing_date"]),
            delisting_date=date.fromisoformat(item["delisting_date"]) if item.get("delisting_date") else None,
            settlement_rule=item.get("settlement_rule", "T+1"),
            allow_odd_lot_sell=bool(item.get("allow_odd_lot_sell", True)),
            metadata_version=item.get("metadata_version", "fixture-rules-v1"),
        )
        for symbol, item in payload["instrument_rules"].items()
    }
    strategy = StrategyConfig(**payload.get("strategy_config", {}))
    risk = RiskPolicy()
    fee = FeeConfig()
    slip = SlippageConfig()
    corporate_actions = tuple(CorporateAction.from_dict(item) for item in payload.get("corporate_actions", []))
    return cfg, calendar, data, rules, strategy, risk, fee, slip, corporate_actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic historical backtest")
    parser.add_argument("--config", required=True)
    parser.add_argument("--export-json")
    parser.add_argument("--export-csv-dir")
    args = parser.parse_args(argv)
    cfg, calendar, data, rules, strategy, risk, fee, slip, corporate_actions = load_backtest_bundle(args.config)
    run = BacktestEngine(
        config=cfg,
        calendar=calendar,
        market_data=data,
        instrument_rules=rules,
        strategy_config=strategy,
        risk_policy=risk,
        fee_config=fee,
        slippage_config=slip,
        corporate_actions=corporate_actions,
        persist=False,
    ).run()
    if args.export_json:
        export_backtest_json(run, args.export_json)
    if args.export_csv_dir:
        export_backtest_csv(run, args.export_csv_dir)
    print(json.dumps(run.result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    for column in ["open", "high", "low", "close"]:
        out[column] = out[column].map(lambda value: Decimal(str(value)))
    out["volume"] = out["volume"].astype(int)
    if "suspended" not in out.columns:
        out["suspended"] = False
    return out.sort_values("date").reset_index(drop=True)


def _validate_inputs(
    config: BacktestConfig,
    calendar: LocalTradingCalendar,
    market_data: dict[str, pd.DataFrame],
    instrument_rules: dict[str, InstrumentRules],
) -> None:
    calendar.trading_days(config.start_date, config.end_date)
    missing_rules = [symbol for symbol in config.symbols if symbol not in instrument_rules]
    if missing_rules:
        raise BacktestError(f"missing instrument rules: {missing_rules}")
    for symbol in config.symbols:
        rules = instrument_rules[symbol]
        if config.start_date < rules.listing_date or (rules.delisting_date and config.end_date > rules.delisting_date):
            raise BacktestError(f"UNSUPPORTED_INSTRUMENT_RULE for {symbol}: listing range")
        if rules.settlement_rule != "T+1":
            raise BacktestError(f"UNSUPPORTED_INSTRUMENT_RULE for {symbol}: settlement")
        if symbol not in market_data:
            raise BacktestError(f"missing market data for {symbol}")
        dates = set(market_data[symbol]["date"].dt.date)
        for session in calendar.trading_days(config.start_date, config.end_date):
            if session not in dates:
                raise BacktestError(f"missing bar for {symbol} on {session}")


def _bar_at(df: pd.DataFrame, session: date) -> DailyBar:
    rows = df[df["date"].dt.date == session]
    if rows.empty:
        raise BacktestError(f"missing bar on {session}")
    row = rows.iloc[0]
    return DailyBar(
        session_date=session,
        open=Decimal(row.open),
        high=Decimal(row.high),
        low=Decimal(row.low),
        close=Decimal(row.close),
        volume=int(row.volume),
        suspended=bool(row.suspended),
    )


def _previous_close(df: pd.DataFrame, session: date) -> Decimal:
    previous = df[df["date"].dt.date < session]
    if previous.empty:
        raise BacktestError(f"missing previous close before {session}")
    return Decimal(previous.iloc[-1].close)


def _blocked_by_price_limit(side: str, bar: DailyBar, previous_close: Decimal, rules: InstrumentRules) -> bool:
    up_limit = legal_price(previous_close * (Decimal("1") + rules.price_limit_rule), rules, side=BacktestSide.BUY.value)
    down_limit = legal_price(previous_close * (Decimal("1") - rules.price_limit_rule), rules, side=BacktestSide.SELL.value)
    if side == BacktestSide.BUY.value:
        return bar.open >= up_limit and bar.low >= up_limit
    return bar.open <= down_limit and bar.high <= down_limit


def legal_price(price: Decimal, rules: InstrumentRules, *, side: str) -> Decimal:
    rounding = ROUND_CEILING if side == BacktestSide.BUY.value else ROUND_DOWN
    ticks = (price / rules.price_tick).to_integral_value(rounding=rounding)
    return money(ticks * rules.price_tick)


def price_tick_round(price: Decimal, tick: Decimal) -> Decimal:
    ticks = (price / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return ticks * tick


def floor_to_lot(quantity: int, lot_size: int) -> int:
    return max(0, (quantity // lot_size) * lot_size)


def _expiry_session(calendar: LocalTradingCalendar, created: date, sessions: int) -> date:
    current = created
    for _ in range(sessions):
        current = calendar.next_trading_day(current)
    return current


def _deterministic_decision(
    decision: RiskDecision,
    signal: Signal,
    account: AccountSnapshot,
    policy: RiskPolicy,
    session: date,
) -> RiskDecision:
    created_at = datetime.combine(session, time(15, 0), tzinfo=TZ)
    return replace(
        decision,
        decision_id=stable_id("risk", signal_identity(signal), account.account_id, policy.version, session.isoformat()),
        created_at=created_at,
    )


def _rules_version(rules: dict[str, InstrumentRules]) -> str:
    payload = {symbol: _decimal_dict(asdict(rule)) for symbol, rule in sorted(rules.items())}
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]


def _blocked_counts(orders: list[BacktestOrder]) -> dict[str, int]:
    return {status.value: sum(1 for order in orders if order.status == status.value) for status in BacktestOrderStatus}


def _drawdown(daily: list[BacktestDailyEquity]) -> tuple[Decimal, date | None, date | None, date | None]:
    if not daily:
        return Decimal("0"), None, None, None
    peak = daily[0].total_equity
    peak_date = daily[0].session_date
    max_dd = Decimal("0")
    start = end = recovery = None
    for item in daily:
        if item.total_equity > peak:
            peak = item.total_equity
            peak_date = item.session_date
            if end is not None and recovery is None:
                recovery = item.session_date
        dd = Decimal("0") if peak == 0 else (peak - item.total_equity) / peak
        if dd > max_dd:
            max_dd = dd.quantize(Decimal("0.000001"))
            start = peak_date
            end = item.session_date
            recovery = None
    return max_dd, start, end, recovery


def _mean(values: list[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0 or not math.isfinite(denominator):
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _sharpe(returns: list[float], annualization_days: int, risk_free_rate: float) -> float | None:
    return _ratio(_mean(returns) * annualization_days - risk_free_rate, _std(returns) * math.sqrt(annualization_days))


def _decimal_or_none(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _max_streak(pnls: list[Decimal], *, positive: bool) -> int:
    best = current = 0
    for pnl in pnls:
        if (pnl > 0) == positive:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_DOWN)


def code_version() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _decimal_dict(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, Decimal):
            out[key] = decimal_to_str(value)
        elif isinstance(value, date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _order_record_payload(order: BacktestOrder) -> dict[str, Any]:
    return {
        "backtest_order_id": order.backtest_order_id,
        "run_id": order.run_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "quantity": order.quantity,
        "remaining_quantity": order.remaining_quantity,
        "limit_price": None if order.limit_price is None else decimal_to_str(order.limit_price),
        "created_session": order.created_session.isoformat(),
        "earliest_execution_session": order.earliest_execution_session.isoformat(),
        "expiry_session": order.expiry_session.isoformat(),
        "status": order.status,
        "rejection_reason": order.rejection_reason,
        "source_signal_identity": order.source_signal_identity,
        "risk_decision_id": order.risk_decision_id,
        "corporate_action_id": order.corporate_action_id,
    }


def _fill_record_payload(fill: BacktestFill) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "quantity": fill.quantity,
        "raw_price": decimal_to_str(fill.raw_price),
        "execution_price": decimal_to_str(fill.execution_price),
        "trade_value": decimal_to_str(fill.trade_value),
        "commission": decimal_to_str(fill.commission),
        "tax": decimal_to_str(fill.tax),
        "other_fees": decimal_to_str(fill.other_fees),
        "slippage_cost": decimal_to_str(fill.slippage_cost),
        "session_date": fill.session_date.isoformat(),
    }


def _daily_record_payload(item: BacktestDailyEquity) -> dict[str, Any]:
    return {
        "session_date": item.session_date.isoformat(),
        "cash": decimal_to_str(item.cash),
        "market_value": decimal_to_str(item.market_value),
        "total_equity": decimal_to_str(item.total_equity),
        "daily_return": decimal_to_str(item.daily_return),
        "peak_equity": decimal_to_str(item.peak_equity),
        "drawdown": decimal_to_str(item.drawdown),
        "exposure": decimal_to_str(item.exposure),
    }


def _position_record_payload(position: BacktestPosition) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "total_quantity": position.total_quantity,
        "available_quantity": position.available_quantity,
        "locked_quantity": position.locked_quantity,
        "average_cost": decimal_to_str(position.average_cost),
        "last_price": decimal_to_str(position.last_price),
        "market_value": decimal_to_str(position.market_value),
        "unrealized_pnl": decimal_to_str(position.unrealized_pnl),
    }


def _corporate_action_record_payload(action: CorporateAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "symbol": action.symbol,
        "action_type": action.action_type,
        "announcement_date": action.announcement_date.isoformat(),
        "record_date": action.record_date.isoformat(),
        "ex_date": action.ex_date.isoformat(),
        "payment_date": None if action.payment_date is None else action.payment_date.isoformat(),
        "tradable_date": None if action.tradable_date is None else action.tradable_date.isoformat(),
        "payload_json": stable_json(action.to_dict()),
        "source": action.source,
        "source_version": action.source_version,
        "data_checksum": action.data_checksum,
    }


def _entitlement_record_payload(entitlement: DividendEntitlement) -> dict[str, Any]:
    return {
        "entitlement_id": entitlement.entitlement_id,
        "action_id": entitlement.action_id,
        "symbol": entitlement.symbol,
        "eligible_quantity": entitlement.eligible_quantity,
        "gross_cash": decimal_to_str(entitlement.gross_cash),
        "tax": decimal_to_str(entitlement.tax),
        "net_cash": decimal_to_str(entitlement.net_cash),
        "record_date": entitlement.record_date.isoformat(),
        "payment_date": entitlement.payment_date.isoformat(),
        "status": entitlement.status,
    }


def _corporate_event_record_payload(event: CorporateActionLedgerEvent) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "action_id": event.action_id,
        "symbol": event.symbol,
        "event_type": event.event_type,
        "session_date": event.session_date.isoformat(),
        "before_json": event.before_json,
        "after_json": event.after_json,
        "amount": decimal_to_str(event.amount),
    }


def corporate_action_checksum(actions: tuple[CorporateAction, ...]) -> str:
    payload = [action.to_dict() for action in sorted(actions, key=lambda item: item.action_id)]
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
