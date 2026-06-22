from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .backtest import (
    BacktestOrder,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestSide,
    DailyBar,
    FeeConfig,
    FeeModel,
    InstrumentRules,
    MatchingEngine,
    Portfolio,
    SlippageConfig,
    SlippageModel,
)
from .data_provider import LocalTradingCalendar, MarketDataError
from .risk import (
    AccountSnapshot,
    PositionSnapshot,
    ProposedOrder,
    ProposedOrderStatus,
    RiskDecision,
    RiskEngine,
    RiskPolicy,
    RiskStatus,
    decimal_to_str,
    stable_id,
    stable_json,
)
from .strategy import Signal, SignalType


MONEY = Decimal("0.01")
TZ = ZoneInfo("Asia/Shanghai")


class PaperTradingError(RuntimeError):
    pass


class PaperAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    PAUSED_RECOVERY_REQUIRED = "PAUSED_RECOVERY_REQUIRED"
    RISK_OFF = "RISK_OFF"
    CLOSED = "CLOSED"


class PaperOrderStatus(StrEnum):
    PAPER_PENDING = "PAPER_PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    BLOCKED_T1 = "BLOCKED_T1"
    BLOCKED_SUSPENSION = "BLOCKED_SUSPENSION"
    BLOCKED_PRICE_LIMIT = "BLOCKED_PRICE_LIMIT"
    BLOCKED_RISK = "BLOCKED_RISK"
    BLOCKED_STALE_DATA = "BLOCKED_STALE_DATA"


class PaperOrderType(StrEnum):
    MARKET_ON_NEXT_OPEN = "MARKET_ON_NEXT_OPEN"
    LIMIT = "LIMIT"


class PaperLedgerEventType(StrEnum):
    INITIAL_DEPOSIT = "INITIAL_DEPOSIT"
    CASH_FROZEN = "CASH_FROZEN"
    CASH_RELEASED = "CASH_RELEASED"
    BUY_SETTLED = "BUY_SETTLED"
    SELL_SETTLED = "SELL_SETTLED"
    COMMISSION_CHARGED = "COMMISSION_CHARGED"
    TAX_CHARGED = "TAX_CHARGED"
    POSITION_FROZEN = "POSITION_FROZEN"
    POSITION_RELEASED = "POSITION_RELEASED"
    DIVIDEND_RECEIVED = "DIVIDEND_RECEIVED"
    CORPORATE_ACTION_ADJUSTMENT = "CORPORATE_ACTION_ADJUSTMENT"
    DAILY_MARK_TO_MARKET = "DAILY_MARK_TO_MARKET"


class NotificationType(StrEnum):
    NEW_PROPOSAL = "NEW_PROPOSAL"
    PROPOSAL_EXPIRING = "PROPOSAL_EXPIRING"
    PROPOSAL_ACCEPTED = "PROPOSAL_ACCEPTED"
    PAPER_ORDER_FILLED = "PAPER_ORDER_FILLED"
    PAPER_ORDER_REJECTED = "PAPER_ORDER_REJECTED"
    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    TAKE_PROFIT_TRIGGERED = "TAKE_PROFIT_TRIGGERED"
    RISK_OFF_TRIGGERED = "RISK_OFF_TRIGGERED"
    STALE_DATA = "STALE_DATA"
    DAILY_REPORT = "DAILY_REPORT"


class ScheduledTaskType(StrEnum):
    SESSION_START = "SESSION_START"
    PRE_MARKET_SCAN = "PRE_MARKET_SCAN"
    MARKET_MONITOR = "MARKET_MONITOR"
    MIDDAY_CHECK = "MIDDAY_CHECK"
    PRE_CLOSE_CHECK = "PRE_CLOSE_CHECK"
    SESSION_CLOSE = "SESSION_CLOSE"
    DAILY_SETTLEMENT = "DAILY_SETTLEMENT"
    DAILY_REPORT = "DAILY_REPORT"
    NOTIFICATION_DELIVERY = "NOTIFICATION_DELIVERY"
    RECOVERY_CHECK = "RECOVERY_CHECK"


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self.timezone)


class TestClock:
    __test__ = False

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("clock time must include timezone")
        self.value = value

    def now(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("clock time must include timezone")
        self.value = value

    def advance(self, delta: timedelta) -> None:
        self.value = self.value + delta


@dataclass
class PaperAccount:
    account_id: str
    name: str
    initial_cash: Decimal
    cash_available: Decimal
    cash_frozen: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    status: str = PaperAccountStatus.ACTIVE.value
    base_currency: str = "CNY"
    peak_equity: Decimal = Decimal("0")
    consecutive_losses: int = 0
    created_at: datetime = datetime(1970, 1, 1, tzinfo=TZ)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=TZ)
    version: int = 1

    def __post_init__(self) -> None:
        _require_tz(self.created_at)
        _require_tz(self.updated_at)
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be greater than 0")
        if self.cash_available < 0 or self.cash_frozen < 0:
            raise ValueError("cash fields must not be negative")
        if self.status not in {item.value for item in PaperAccountStatus}:
            raise ValueError("invalid paper account status")
        if self.peak_equity == Decimal("0"):
            self.peak_equity = self.total_equity

    @property
    def total_equity(self) -> Decimal:
        return money(self.cash_available + self.cash_frozen + self.market_value)

    def assert_can_buy(self) -> None:
        if self.status != PaperAccountStatus.ACTIVE.value:
            raise PaperTradingError(f"paper account status blocks new buys: {self.status}")

    def touch(self, now: datetime) -> None:
        _require_tz(now)
        self.updated_at = now
        self.version += 1


@dataclass
class PaperPosition:
    account_id: str
    symbol: str
    total_quantity: int = 0
    available_quantity: int = 0
    today_bought_quantity: int = 0
    locked_quantity: int = 0
    average_cost: Decimal = Decimal("0")
    last_price: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    industry: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if min(self.total_quantity, self.available_quantity, self.today_bought_quantity, self.locked_quantity) < 0:
            raise ValueError("position quantity buckets must not be negative")
        if self.available_quantity + self.today_bought_quantity + self.locked_quantity > self.total_quantity:
            raise ValueError("position quantity buckets exceed total")

    def mark(self, price: Decimal) -> None:
        self.last_price = money(price)
        self.market_value = money(price * Decimal(self.total_quantity))
        self.unrealized_pnl = money((price - self.average_cost) * Decimal(self.total_quantity))
        self.version += 1

    def buy(self, quantity: int, total_cost: Decimal, price: Decimal) -> None:
        new_quantity = self.total_quantity + quantity
        weighted_cost = self.average_cost * Decimal(self.total_quantity) + total_cost
        self.average_cost = Decimal("0") if new_quantity == 0 else (weighted_cost / Decimal(new_quantity)).quantize(Decimal("0.0001"))
        self.total_quantity = new_quantity
        self.today_bought_quantity += quantity
        self.mark(price)

    def freeze_for_sell(self, quantity: int) -> None:
        if quantity <= 0 or quantity > self.available_quantity:
            raise PaperTradingError("sell quantity exceeds available position")
        self.available_quantity -= quantity
        self.locked_quantity += quantity
        self.version += 1

    def release_frozen(self, quantity: int) -> None:
        release = min(quantity, self.locked_quantity)
        self.locked_quantity -= release
        self.available_quantity += release
        self.version += 1

    def sell(self, quantity: int, proceeds_after_fee: Decimal, price: Decimal) -> None:
        if quantity <= 0 or quantity > self.total_quantity:
            raise PaperTradingError("sell quantity exceeds position")
        cost_basis = self.average_cost * Decimal(quantity)
        self.realized_pnl = money(self.realized_pnl + proceeds_after_fee - cost_basis)
        self.total_quantity -= quantity
        if self.locked_quantity >= quantity:
            self.locked_quantity -= quantity
        else:
            remaining = quantity - self.locked_quantity
            self.locked_quantity = 0
            self.available_quantity = max(0, self.available_quantity - remaining)
        if self.total_quantity == 0:
            self.average_cost = Decimal("0")
        self.mark(price if self.total_quantity else Decimal("0"))

    def release_t1(self) -> int:
        released = self.today_bought_quantity
        self.available_quantity += released
        self.today_bought_quantity = 0
        if released:
            self.version += 1
        return released


@dataclass
class PaperOrder:
    paper_order_id: str
    account_id: str
    symbol: str
    side: str
    order_type: str
    quantity: int
    remaining_quantity: int
    created_at: datetime
    expires_at: datetime
    status: str = PaperOrderStatus.PAPER_PENDING.value
    proposal_id: str | None = None
    limit_price: Decimal | None = None
    source_signal_identity: str = ""
    risk_decision_id: str = ""
    rejection_reason: str = ""
    idempotency_key: str = ""
    active_key: str | None = None
    version: int = 1

    def to_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = decimal_to_str(value)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        payload["paper_trading"] = True
        return payload


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    paper_order_id: str
    account_id: str
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
    filled_at: datetime

    def to_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = decimal_to_str(value)
            if isinstance(value, (datetime, date)):
                payload[key] = value.isoformat()
        return payload


@dataclass(frozen=True)
class PaperLedgerEntry:
    entry_id: str
    account_id: str
    event_type: str
    amount: Decimal
    cash_available_after: Decimal
    cash_frozen_after: Decimal
    occurred_at: datetime
    symbol: str | None = None
    quantity: int = 0
    ref_id: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = decimal_to_str(value)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload


@dataclass(frozen=True)
class ProposalStatusChange:
    proposal_id: str
    from_status: str
    to_status: str
    operator: str
    reason: str
    changed_at: datetime


@dataclass(frozen=True)
class ScheduledTaskRun:
    task_key: str
    account_id: str
    task_type: str
    session_date: date
    status: str
    attempt: int
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str = ""


@dataclass
class NotificationOutboxMessage:
    message_id: str
    dedupe_key: str
    account_id: str
    notification_type: str
    payload: dict
    status: str = "PENDING"
    retry_count: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class PaperTradingConfig:
    order_expiry_minutes: int = 240
    signal_max_age_minutes: int = 24 * 60
    volume_participation_rate: Decimal = Decimal("0.10")
    stop_loss_pct: Decimal = Decimal("0.05")
    take_profit_1_pct: Decimal = Decimal("0.05")
    take_profit_2_pct: Decimal = Decimal("0.08")
    trailing_stop_pct: Decimal = Decimal("0.08")
    max_holding_days: int = 20


class PaperTradingService:
    def __init__(
        self,
        *,
        calendar: LocalTradingCalendar,
        clock: Clock | None = None,
        risk_engine: RiskEngine | None = None,
        risk_policy: RiskPolicy | None = None,
        fee_config: FeeConfig | None = None,
        slippage_config: SlippageConfig | None = None,
        config: PaperTradingConfig | None = None,
    ) -> None:
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.risk_engine = risk_engine or RiskEngine()
        self.risk_policy = risk_policy or RiskPolicy()
        self.config = config or PaperTradingConfig()
        self.matching = MatchingEngine(
            FeeModel(fee_config or FeeConfig()),
            SlippageModel(
                slippage_config
                or SlippageConfig(max_volume_participation_rate=self.config.volume_participation_rate)
            ),
        )
        self.accounts: dict[str, PaperAccount] = {}
        self.positions: dict[tuple[str, str], PaperPosition] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.fills: dict[str, PaperFill] = {}
        self.ledger: list[PaperLedgerEntry] = []
        self.proposal_status: dict[str, str] = {}
        self.proposal_history: list[ProposalStatusChange] = []
        self.task_runs: dict[str, ScheduledTaskRun] = {}
        self.outbox: dict[str, NotificationOutboxMessage] = {}
        self._fill_keys: set[str] = set()

    def create_account(self, *, account_id: str, name: str, initial_cash: Decimal) -> PaperAccount:
        now = self._now()
        if account_id in self.accounts:
            return self.accounts[account_id]
        account = PaperAccount(
            account_id=account_id,
            name=name,
            initial_cash=money(initial_cash),
            cash_available=money(initial_cash),
            created_at=now,
            updated_at=now,
        )
        self.accounts[account_id] = account
        self._ledger(account, PaperLedgerEventType.INITIAL_DEPOSIT.value, money(initial_cash), now, ref_id=account_id)
        return account

    def account_snapshot(self, account_id: str) -> AccountSnapshot:
        account = self._account(account_id)
        positions = tuple(
            PositionSnapshot(
                symbol=position.symbol,
                quantity=position.total_quantity,
                available_quantity=position.available_quantity,
                average_cost=position.average_cost,
                current_price=position.last_price,
                market_value=position.market_value,
                industry=position.industry,
            )
            for key, position in sorted(self.positions.items())
            if key[0] == account_id and position.total_quantity > 0
        )
        return AccountSnapshot(
            account_id=account.account_id,
            as_of=self._now(),
            total_equity=max(account.total_equity, Decimal("0.01")),
            available_cash=account.cash_available,
            market_value=account.market_value,
            frozen_cash=account.cash_frozen,
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=account.unrealized_pnl,
            peak_equity=max(account.peak_equity, account.total_equity, Decimal("0.01")),
            consecutive_losses=account.consecutive_losses,
            positions=positions,
        )

    def transition_proposal(
        self,
        proposal: ProposedOrder,
        *,
        to_status: str,
        operator: str,
        reason: str,
    ) -> str:
        now = self._now()
        current = self.proposal_status.get(proposal.proposal_id, proposal.status)
        allowed = {
            ProposedOrderStatus.PROPOSED.value: {
                ProposedOrderStatus.REVIEWED.value,
                ProposedOrderStatus.ACCEPTED.value,
                ProposedOrderStatus.REJECTED.value,
                ProposedOrderStatus.EXPIRED.value,
                ProposedOrderStatus.CANCELLED.value,
            },
            ProposedOrderStatus.REVIEWED.value: {
                ProposedOrderStatus.ACCEPTED.value,
                ProposedOrderStatus.REJECTED.value,
                ProposedOrderStatus.EXPIRED.value,
                ProposedOrderStatus.CANCELLED.value,
            },
            ProposedOrderStatus.ACCEPTED.value: set(),
            ProposedOrderStatus.REJECTED.value: set(),
            ProposedOrderStatus.EXPIRED.value: set(),
            ProposedOrderStatus.CANCELLED.value: set(),
        }
        if current == to_status:
            return current
        if to_status not in allowed[current]:
            raise PaperTradingError(f"invalid proposal transition: {current}->{to_status}")
        self.proposal_status[proposal.proposal_id] = to_status
        self.proposal_history.append(ProposalStatusChange(proposal.proposal_id, current, to_status, operator, reason, now))
        return to_status

    def accept_proposal(
        self,
        *,
        account_id: str,
        proposal: ProposedOrder,
        signal: Signal,
        operator: str,
        idempotency_key: str,
        reason: str = "manual accepted for paper trading",
    ) -> PaperOrder:
        account = self._account(account_id)
        now = self._now()
        account.assert_can_buy()
        if now > proposal.expires_at:
            self.transition_proposal(proposal, to_status=ProposedOrderStatus.EXPIRED.value, operator=operator, reason="expired")
            raise PaperTradingError("expired proposal cannot be accepted")
        if signal.signal_type in {SignalType.DATA_ERROR, SignalType.RISK_OFF}:
            raise PaperTradingError("DATA_ERROR or RISK_OFF signal cannot be accepted")
        if signal.signal_type != SignalType.BUY_WATCH:
            raise PaperTradingError("paper buy requires BUY_WATCH and manual confirmation")
        if self._signal_is_stale(signal, now):
            raise PaperTradingError("stale signal cannot be accepted")
        existing = self._order_by_idempotency(idempotency_key)
        if existing is not None:
            return existing
        if any(order.proposal_id == proposal.proposal_id and order.status in _ACTIVE_ORDER_STATUSES for order in self.orders.values()):
            raise PaperTradingError("active paper order already exists for proposal")
        quantity = proposal.quantity
        estimated_cash = money(proposal.reference_price * Decimal(quantity) * Decimal("1.03"))
        if estimated_cash > account.cash_available:
            raise PaperTradingError("insufficient paper cash to freeze")
        account.cash_available = money(account.cash_available - estimated_cash)
        account.cash_frozen = money(account.cash_frozen + estimated_cash)
        account.touch(now)
        self._ledger(account, PaperLedgerEventType.CASH_FROZEN.value, estimated_cash, now, ref_id=proposal.proposal_id)
        order = PaperOrder(
            paper_order_id=stable_id("paper-order", account_id, proposal.proposal_id, idempotency_key),
            account_id=account_id,
            proposal_id=proposal.proposal_id,
            active_key=proposal.proposal_id,
            idempotency_key=idempotency_key,
            symbol=proposal.symbol,
            side=BacktestSide.BUY.value,
            order_type=PaperOrderType.MARKET_ON_NEXT_OPEN.value,
            quantity=quantity,
            remaining_quantity=quantity,
            created_at=now,
            expires_at=now + timedelta(minutes=self.config.order_expiry_minutes),
            source_signal_identity=proposal.signal_identity,
            risk_decision_id=proposal.risk_decision_id,
        )
        self.orders[order.paper_order_id] = order
        self.transition_proposal(proposal, to_status=ProposedOrderStatus.ACCEPTED.value, operator=operator, reason=reason)
        self._outbox(account_id, NotificationType.PROPOSAL_ACCEPTED.value, {"proposal_id": proposal.proposal_id})
        return order

    def create_sell_order(
        self,
        *,
        account_id: str,
        symbol: str,
        quantity: int,
        reason: str,
        limit_price: Decimal | None = None,
    ) -> PaperOrder:
        account = self._account(account_id)
        now = self._now()
        position = self._position(account_id, symbol)
        active = [
            order
            for order in self.orders.values()
            if order.account_id == account_id
            and order.symbol == symbol
            and order.side == BacktestSide.SELL.value
            and order.status in _ACTIVE_ORDER_STATUSES
        ]
        if active:
            return sorted(active, key=lambda item: item.created_at)[0]
        if quantity > position.total_quantity:
            raise PaperTradingError("sell quantity exceeds position")
        if quantity <= position.available_quantity:
            position.freeze_for_sell(quantity)
            self._ledger(account, PaperLedgerEventType.POSITION_FROZEN.value, Decimal("0.00"), now, symbol=symbol, quantity=quantity, ref_id=reason)
        order = PaperOrder(
            paper_order_id=stable_id("paper-sell", account_id, symbol, str(now), reason),
            account_id=account_id,
            proposal_id=None,
            active_key=None,
            idempotency_key=stable_id("paper-sell-idem", account_id, symbol, str(now), reason),
            symbol=symbol,
            side=BacktestSide.SELL.value,
            order_type=PaperOrderType.MARKET_ON_NEXT_OPEN.value if limit_price is None else PaperOrderType.LIMIT.value,
            quantity=quantity,
            remaining_quantity=quantity,
            limit_price=limit_price,
            created_at=now,
            expires_at=now + timedelta(minutes=self.config.order_expiry_minutes),
            source_signal_identity=reason,
        )
        self.orders[order.paper_order_id] = order
        return order

    def process_market_event(
        self,
        *,
        account_id: str,
        symbol: str,
        bar: DailyBar,
        previous_close: Decimal,
        rules: InstrumentRules,
        market_data_as_of: date,
        event_id: str,
    ) -> list[PaperFill]:
        self._assert_trading_session(bar.session_date)
        if market_data_as_of != bar.session_date:
            self._block_symbol_orders(account_id, symbol, PaperOrderStatus.BLOCKED_STALE_DATA.value, "stale market data")
            self._outbox(account_id, NotificationType.STALE_DATA.value, {"symbol": symbol, "actual": market_data_as_of.isoformat(), "expected": bar.session_date.isoformat()})
            raise MarketDataError(f"stale market data: expected {bar.session_date}, actual {market_data_as_of}")
        fills: list[PaperFill] = []
        for order in sorted(self.orders.values(), key=lambda item: (item.created_at, item.paper_order_id)):
            if order.account_id != account_id or order.symbol != symbol or order.status not in _ACTIVE_ORDER_STATUSES:
                continue
            key = f"{order.paper_order_id}:{event_id}"
            if key in self._fill_keys:
                continue
            if self._now() > order.expires_at:
                order.status = PaperOrderStatus.EXPIRED.value
                order.rejection_reason = "paper order expired"
                self._release_order(order)
                continue
            if order.side == BacktestSide.BUY.value and not self._risk_allows(order, rules):
                order.status = PaperOrderStatus.BLOCKED_RISK.value
                order.rejection_reason = "risk recheck blocked paper order"
                self._release_order(order)
                self._outbox(account_id, NotificationType.PAPER_ORDER_REJECTED.value, {"order_id": order.paper_order_id, "reason": order.rejection_reason})
                continue
            fill = self._execute_order(order, bar, previous_close, rules)
            self._fill_keys.add(key)
            if fill is None:
                continue
            fills.append(fill)
        return fills

    def release_t1(self, *, account_id: str, session_date: date) -> None:
        self._assert_trading_session(session_date)
        account = self._account(account_id)
        now = self._now()
        for (owner, _symbol), position in sorted(self.positions.items()):
            if owner != account_id:
                continue
            released = position.release_t1()
            if released:
                self._ledger(account, PaperLedgerEventType.POSITION_RELEASED.value, Decimal("0.00"), now, symbol=position.symbol, quantity=released, ref_id=session_date.isoformat())

    def monitor_positions(self, *, account_id: str, prices: dict[str, Decimal], session_date: date) -> list[PaperOrder]:
        self._assert_trading_session(session_date)
        created: list[PaperOrder] = []
        for (owner, symbol), position in sorted(self.positions.items()):
            if owner != account_id or position.total_quantity <= 0:
                continue
            price = prices[symbol]
            position.mark(price)
            reason = ""
            if price <= money(position.average_cost * (Decimal("1") - self.config.stop_loss_pct)):
                reason = "STOP_LOSS_TRIGGERED"
            elif price >= money(position.average_cost * (Decimal("1") + self.config.take_profit_2_pct)):
                reason = "TAKE_PROFIT_TRIGGERED"
            if not reason:
                continue
            active = [
                order
                for order in self.orders.values()
                if order.account_id == account_id
                and order.symbol == symbol
                and order.side == BacktestSide.SELL.value
                and order.status in _ACTIVE_ORDER_STATUSES
            ]
            if active:
                created.append(sorted(active, key=lambda item: item.created_at)[0])
                continue
            if position.available_quantity <= 0:
                self._outbox(account_id, NotificationType[reason].value, {"symbol": symbol, "blocked": "T+1"})
                continue
            order = self.create_sell_order(account_id=account_id, symbol=symbol, quantity=position.available_quantity, reason=reason)
            self._outbox(account_id, NotificationType[reason].value, {"symbol": symbol, "order_id": order.paper_order_id})
            created.append(order)
        return created

    def daily_settlement(self, *, account_id: str, prices: dict[str, Decimal], session_date: date) -> dict:
        self._assert_trading_session(session_date)
        account = self._account(account_id)
        for (owner, symbol), position in self.positions.items():
            if owner == account_id and symbol in prices:
                position.mark(prices[symbol])
        self._revalue_account(account)
        now = self._now()
        self._ledger(account, PaperLedgerEventType.DAILY_MARK_TO_MARKET.value, Decimal("0.00"), now, ref_id=session_date.isoformat())
        payload = {
            "account_id": account_id,
            "session_date": session_date.isoformat(),
            "cash_available": decimal_to_str(account.cash_available),
            "cash_frozen": decimal_to_str(account.cash_frozen),
            "market_value": decimal_to_str(account.market_value),
            "total_equity": decimal_to_str(account.total_equity),
        }
        self._outbox(account_id, NotificationType.DAILY_REPORT.value, payload)
        return payload

    def run_task(self, *, account_id: str, task_type: str, session_date: date, retry: bool = False) -> ScheduledTaskRun:
        self._assert_trading_session(session_date)
        key = stable_id("paper-task", account_id, task_type, session_date.isoformat())
        if key in self.task_runs and self.task_runs[key].status == "SUCCESS" and not retry:
            return self.task_runs[key]
        previous = self.task_runs.get(key)
        attempt = 1 if previous is None else previous.attempt + 1
        started = self._now()
        run = ScheduledTaskRun(key, account_id, task_type, session_date, "SUCCESS", attempt, started, self._now())
        self.task_runs[key] = run
        if task_type == ScheduledTaskType.SESSION_START.value:
            self.release_t1(account_id=account_id, session_date=session_date)
        if task_type == ScheduledTaskType.DAILY_REPORT.value:
            self.daily_settlement(account_id=account_id, prices={}, session_date=session_date)
        return run

    def dispatch_outbox(self, sender: Callable[[NotificationOutboxMessage], None]) -> None:
        for message in sorted(self.outbox.values(), key=lambda item: item.message_id):
            if message.status == "SENT":
                continue
            try:
                sender(message)
                message.status = "SENT"
                message.last_error = ""
            except Exception as exc:  # notification failure must not roll back trades
                message.retry_count += 1
                message.last_error = str(exc)
                message.status = "FAILED"

    def recover_account(self, *, account_id: str) -> PaperAccount:
        account = self._account(account_id)
        expected = Decimal("0")
        for entry in self.ledger:
            if entry.account_id == account_id and entry.event_type == PaperLedgerEventType.INITIAL_DEPOSIT.value:
                expected += entry.amount
        if expected and expected != account.initial_cash:
            account.status = PaperAccountStatus.PAUSED.value
        for order in self.orders.values():
            if order.account_id == account_id and order.status == PaperOrderStatus.SUBMITTED.value:
                order.status = PaperOrderStatus.PAPER_PENDING.value
        return account

    def fixed_flow_report(self) -> dict:
        return {
            "accounts": [account.account_id for account in self.accounts.values()],
            "orders": [order.to_dict() for order in self.orders.values()],
            "fills": [fill.to_dict() for fill in self.fills.values()],
            "ledger_events": [entry.event_type for entry in self.ledger],
            "outbox_types": [message.notification_type for message in self.outbox.values()],
        }

    def _execute_order(self, order: PaperOrder, bar: DailyBar, previous_close: Decimal, rules: InstrumentRules) -> PaperFill | None:
        account = self._account(order.account_id)
        bt_order = BacktestOrder(
            backtest_order_id=order.paper_order_id,
            run_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            order_type=BacktestOrderType.MARKET_ON_NEXT_OPEN.value if order.order_type == PaperOrderType.MARKET_ON_NEXT_OPEN.value else BacktestOrderType.LIMIT.value,
            quantity=order.quantity,
            remaining_quantity=order.remaining_quantity,
            limit_price=order.limit_price,
            created_session=bar.session_date,
            earliest_execution_session=bar.session_date,
            expiry_session=bar.session_date,
            status=BacktestOrderStatus.PENDING.value,
            source_signal_identity=order.source_signal_identity,
            risk_decision_id=order.risk_decision_id,
        )
        portfolio = self._matching_portfolio(account, order.symbol)
        fill = self.matching.execute(order=bt_order, bar=bar, previous_close=previous_close, rules=rules, portfolio=portfolio, run_id=order.account_id)
        if bt_order.status in {
            BacktestOrderStatus.BLOCKED_T1.value,
            BacktestOrderStatus.BLOCKED_SUSPENSION.value,
            BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value,
            BacktestOrderStatus.EXPIRED.value,
            BacktestOrderStatus.REJECTED.value,
        }:
            order.status = _PAPER_STATUS_BY_BACKTEST[bt_order.status]
            order.rejection_reason = bt_order.rejection_reason
            if order.side == BacktestSide.BUY.value or order.status in {PaperOrderStatus.EXPIRED.value, PaperOrderStatus.REJECTED.value}:
                self._release_order(order)
            return None
        if fill is None:
            return None
        paper_fill = PaperFill(
            fill_id=fill.fill_id,
            paper_order_id=order.paper_order_id,
            account_id=order.account_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            raw_price=fill.raw_price,
            execution_price=fill.execution_price,
            trade_value=fill.trade_value,
            commission=fill.commission,
            tax=fill.tax,
            other_fees=fill.other_fees,
            slippage_cost=fill.slippage_cost,
            session_date=fill.session_date,
            filled_at=self._now(),
        )
        self.fills[paper_fill.fill_id] = paper_fill
        order.remaining_quantity = bt_order.remaining_quantity
        order.status = (
            PaperOrderStatus.FILLED.value
            if bt_order.status == BacktestOrderStatus.FILLED.value
            else PaperOrderStatus.PARTIALLY_FILLED.value
        )
        self._apply_fill(account, order, paper_fill)
        self._outbox(account.account_id, NotificationType.PAPER_ORDER_FILLED.value, {"order_id": order.paper_order_id, "fill_id": paper_fill.fill_id})
        return paper_fill

    def _apply_fill(self, account: PaperAccount, order: PaperOrder, fill: PaperFill) -> None:
        now = self._now()
        position = self._position(account.account_id, fill.symbol)
        if fill.side == BacktestSide.BUY.value:
            total_cost = money(fill.trade_value + fill.commission + fill.other_fees)
            release = max(Decimal("0.00"), money(account.cash_frozen - total_cost))
            account.cash_frozen = money(account.cash_frozen - total_cost - release)
            account.cash_available = money(account.cash_available + release)
            position.buy(fill.quantity, total_cost, fill.execution_price)
            self._ledger(account, PaperLedgerEventType.CASH_RELEASED.value, release, now, ref_id=order.paper_order_id)
            self._ledger(account, PaperLedgerEventType.BUY_SETTLED.value, -fill.trade_value, now, symbol=fill.symbol, quantity=fill.quantity, ref_id=fill.fill_id)
            self._ledger(account, PaperLedgerEventType.COMMISSION_CHARGED.value, -fill.commission, now, ref_id=fill.fill_id)
        else:
            proceeds = money(fill.trade_value - fill.commission - fill.tax - fill.other_fees)
            position.sell(fill.quantity, proceeds, fill.execution_price)
            account.cash_available = money(account.cash_available + proceeds)
            self._ledger(account, PaperLedgerEventType.SELL_SETTLED.value, fill.trade_value, now, symbol=fill.symbol, quantity=fill.quantity, ref_id=fill.fill_id)
            self._ledger(account, PaperLedgerEventType.COMMISSION_CHARGED.value, -fill.commission, now, ref_id=fill.fill_id)
            self._ledger(account, PaperLedgerEventType.TAX_CHARGED.value, -fill.tax, now, ref_id=fill.fill_id)
        self._revalue_account(account)
        account.touch(now)

    def _release_order(self, order: PaperOrder) -> None:
        account = self._account(order.account_id)
        now = self._now()
        if order.side == BacktestSide.BUY.value and account.cash_frozen > 0:
            release = account.cash_frozen
            account.cash_available = money(account.cash_available + release)
            account.cash_frozen = Decimal("0.00")
            self._ledger(account, PaperLedgerEventType.CASH_RELEASED.value, release, now, ref_id=order.paper_order_id)
        if order.side == BacktestSide.SELL.value:
            position = self._position(order.account_id, order.symbol)
            position.release_frozen(order.remaining_quantity)
            self._ledger(account, PaperLedgerEventType.POSITION_RELEASED.value, Decimal("0.00"), now, symbol=order.symbol, quantity=order.remaining_quantity, ref_id=order.paper_order_id)

    def _matching_portfolio(self, account: PaperAccount, symbol: str) -> Portfolio:
        position = self.positions.get((account.account_id, symbol))
        portfolio = Portfolio(cash_available=account.cash_available + account.cash_frozen)
        if position is not None:
            bt_position = portfolio.position(symbol)
            bt_position.total_quantity = position.total_quantity
            bt_position.available_quantity = position.available_quantity + position.locked_quantity
            bt_position.average_cost = position.average_cost
            bt_position.last_price = position.last_price
            bt_position.market_value = position.market_value
        return portfolio

    def _risk_allows(self, order: PaperOrder, rules: InstrumentRules) -> bool:
        account = self._account(order.account_id)
        if account.status != PaperAccountStatus.ACTIVE.value:
            return False
        signal = _signal_stub(order)
        decision: RiskDecision = self.risk_engine.evaluate(
            signal=signal,
            account=self.account_snapshot(order.account_id),
            policy=self.risk_policy,
            reference_price=order.limit_price or Decimal("1"),
            stop_price=(order.limit_price or Decimal("1")) * Decimal("0.95"),
            industry=None,
        )
        if decision.status == RiskStatus.RISK_OFF.value:
            account.status = PaperAccountStatus.RISK_OFF.value
            self._outbox(account.account_id, NotificationType.RISK_OFF_TRIGGERED.value, {"order_id": order.paper_order_id})
            return False
        if decision.status not in {RiskStatus.APPROVED.value, RiskStatus.REDUCED.value}:
            return False
        if decision.approved_quantity < rules.lot_size:
            return False
        if decision.approved_quantity < order.remaining_quantity:
            order.remaining_quantity = decision.approved_quantity
        return True

    def _ledger(
        self,
        account: PaperAccount,
        event_type: str,
        amount: Decimal,
        occurred_at: datetime,
        *,
        symbol: str | None = None,
        quantity: int = 0,
        ref_id: str = "",
        payload: dict | None = None,
    ) -> None:
        entry = PaperLedgerEntry(
            entry_id=stable_id("paper-ledger", account.account_id, event_type, str(len(self.ledger)), ref_id, str(occurred_at)),
            account_id=account.account_id,
            event_type=event_type,
            amount=money(amount),
            cash_available_after=account.cash_available,
            cash_frozen_after=account.cash_frozen,
            occurred_at=occurred_at,
            symbol=symbol,
            quantity=quantity,
            ref_id=ref_id,
            payload=payload or {},
        )
        self.ledger.append(entry)

    def _outbox(self, account_id: str, notification_type: str, payload: dict) -> NotificationOutboxMessage:
        dedupe = stable_id("paper-notify", account_id, notification_type, stable_json(payload))
        if dedupe in self.outbox:
            return self.outbox[dedupe]
        message = NotificationOutboxMessage(
            message_id=stable_id("paper-message", dedupe),
            dedupe_key=dedupe,
            account_id=account_id,
            notification_type=notification_type,
            payload=json.loads(stable_json(payload)),
        )
        self.outbox[dedupe] = message
        return message

    def _revalue_account(self, account: PaperAccount) -> None:
        account.market_value = money(sum((p.market_value for (owner, _), p in self.positions.items() if owner == account.account_id), Decimal("0")))
        account.realized_pnl = money(sum((p.realized_pnl for (owner, _), p in self.positions.items() if owner == account.account_id), Decimal("0")))
        account.unrealized_pnl = money(sum((p.unrealized_pnl for (owner, _), p in self.positions.items() if owner == account.account_id), Decimal("0")))
        if account.total_equity > account.peak_equity:
            account.peak_equity = account.total_equity

    def _block_symbol_orders(self, account_id: str, symbol: str, status: str, reason: str) -> None:
        for order in self.orders.values():
            if order.account_id == account_id and order.symbol == symbol and order.status in _ACTIVE_ORDER_STATUSES:
                order.status = status
                order.rejection_reason = reason
                self._release_order(order)

    def _signal_is_stale(self, signal: Signal, now: datetime) -> bool:
        generated = signal.signal_generated_at.astimezone(TZ)
        return now.astimezone(TZ) - generated > timedelta(minutes=self.config.signal_max_age_minutes)

    def _assert_trading_session(self, session_date: date) -> None:
        try:
            trading_day = self.calendar.is_trading_day(session_date)
        except MarketDataError as exc:
            raise PaperTradingError(f"not a trading day or calendar coverage missing: {exc}") from exc
        if not trading_day:
            raise PaperTradingError(f"not a trading day: {session_date}")

    def _account(self, account_id: str) -> PaperAccount:
        try:
            return self.accounts[account_id]
        except KeyError as exc:
            raise PaperTradingError(f"unknown paper account: {account_id}") from exc

    def _position(self, account_id: str, symbol: str) -> PaperPosition:
        key = (account_id, symbol)
        if key not in self.positions:
            self.positions[key] = PaperPosition(account_id=account_id, symbol=symbol)
        return self.positions[key]

    def _order_by_idempotency(self, key: str) -> PaperOrder | None:
        return next((order for order in self.orders.values() if order.idempotency_key == key), None)

    def _now(self) -> datetime:
        now = self.clock.now()
        _require_tz(now)
        return now.astimezone(TZ)


def run_fixed_simulation_flow() -> dict:
    days = frozenset({date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)})
    calendar = LocalTradingCalendar(
        source="paper-fixture",
        trading_day_set=days,
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 7),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="paper-fixture-v1",
    )
    clock = TestClock(datetime(2026, 1, 5, 15, 10, tzinfo=TZ))
    service = PaperTradingService(
        calendar=calendar,
        clock=clock,
        fee_config=FeeConfig(minimum_commission=Decimal("0")),
        config=PaperTradingConfig(order_expiry_minutes=24 * 60),
    )
    service.create_account(account_id="paper-demo", name="Demo", initial_cash=Decimal("100000"))
    signal = _flow_signal()
    decision = RiskEngine().evaluate(
        signal=signal,
        account=service.account_snapshot("paper-demo"),
        policy=RiskPolicy(),
        reference_price=Decimal("10"),
        stop_price=Decimal("9.50"),
    )
    proposal = ProposedOrder(
        proposal_id=stable_id("proposal", signal.symbol, signal.signal_type, signal.parameter_version),
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
        symbol=signal.symbol,
        side="BUY",
        quantity=decision.approved_quantity,
        reference_price=Decimal("10"),
        stop_price=Decimal("9.50"),
        take_profit_1=Decimal("10.50"),
        take_profit_2=Decimal("10.80"),
        signal_identity=decision.signal_identity,
        risk_decision_id=decision.decision_id,
        status=ProposedOrderStatus.PROPOSED.value,
    )
    service.accept_proposal(account_id="paper-demo", proposal=proposal, signal=signal, operator="tester", idempotency_key="flow-accept")
    clock.set(datetime(2026, 1, 6, 9, 31, tzinfo=TZ))
    rules = InstrumentRules("600519", "SSE", "MAIN", 100, Decimal("0.01"), Decimal("0.10"), False, date(2001, 1, 1), None)
    service.process_market_event(
        account_id="paper-demo",
        symbol="600519",
        bar=DailyBar(date(2026, 1, 6), Decimal("10.00"), Decimal("10.20"), Decimal("9.90"), Decimal("10.10"), 10000),
        previous_close=Decimal("10.00"),
        rules=rules,
        market_data_as_of=date(2026, 1, 6),
        event_id="buy-open",
    )
    clock.set(datetime(2026, 1, 7, 9, 20, tzinfo=TZ))
    service.run_task(account_id="paper-demo", task_type=ScheduledTaskType.SESSION_START.value, session_date=date(2026, 1, 7))
    service.monitor_positions(account_id="paper-demo", prices={"600519": Decimal("10.90")}, session_date=date(2026, 1, 7))
    service.process_market_event(
        account_id="paper-demo",
        symbol="600519",
        bar=DailyBar(date(2026, 1, 7), Decimal("10.90"), Decimal("11.00"), Decimal("10.70"), Decimal("10.80"), 10000),
        previous_close=Decimal("10.10"),
        rules=rules,
        market_data_as_of=date(2026, 1, 7),
        event_id="sell-open",
    )
    service.daily_settlement(account_id="paper-demo", prices={"600519": Decimal("10.80")}, session_date=date(2026, 1, 7))
    return service.fixed_flow_report()


def _flow_signal() -> Signal:
    now = datetime(2026, 1, 5, 15, 5, tzinfo=TZ)
    return Signal(
        symbol="600519",
        action=SignalType.BUY_WATCH.value,
        score=90,
        price=10,
        stop_price=9.5,
        take_profit_1=10.5,
        take_profit_2=10.8,
        suggested_shares=500,
        reason="fixture",
        market_trade_date=date(2026, 1, 5),
        market_fetched_at=now,
        signal_generated_at=now,
        strategy_name="multi_factor_v1",
        strategy_version="1.0.0",
        parameter_version="fixture",
        parameter_snapshot="{}",
        market_as_of_date=date(2026, 1, 5),
        market_data_source="fixture",
        market_data_adjust="qfq",
        signal_type=SignalType.BUY_WATCH.value,
        score_breakdown={},
        reasons=["fixture"],
        invalidation_conditions=["fixture"],
        reference_price=10,
        stop_loss_price=9.5,
        take_profit_1_price=10.5,
        take_profit_2_price=10.8,
        market_data_checksum="checksum",
        market_calendar_version="paper-fixture-v1",
    )


def _signal_stub(order: PaperOrder) -> Signal:
    now = datetime(2026, 1, 1, 15, 0, tzinfo=TZ)
    return Signal(
        symbol=order.symbol,
        action=SignalType.BUY_WATCH.value,
        score=100,
        price=1,
        stop_price=0.95,
        take_profit_1=1.05,
        take_profit_2=1.08,
        suggested_shares=order.remaining_quantity,
        reason="paper risk recheck",
        market_trade_date=now.date(),
        market_fetched_at=now,
        signal_generated_at=now,
        strategy_name="paper",
        strategy_version="1.0.0",
        parameter_version="paper",
        parameter_snapshot="{}",
        market_as_of_date=now.date(),
        market_data_source="paper",
        market_data_adjust="",
        signal_type=SignalType.BUY_WATCH.value,
        score_breakdown={},
        reasons=["paper risk recheck"],
        invalidation_conditions=[],
        reference_price=1,
        stop_loss_price=0.95,
        take_profit_1_price=1.05,
        take_profit_2_price=1.08,
        market_data_checksum="paper",
        market_calendar_version="paper",
    )


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY)


def _require_tz(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone")


_ACTIVE_ORDER_STATUSES = {
    PaperOrderStatus.PAPER_PENDING.value,
    PaperOrderStatus.SUBMITTED.value,
    PaperOrderStatus.PARTIALLY_FILLED.value,
}


_PAPER_STATUS_BY_BACKTEST = {
    BacktestOrderStatus.BLOCKED_T1.value: PaperOrderStatus.BLOCKED_T1.value,
    BacktestOrderStatus.BLOCKED_SUSPENSION.value: PaperOrderStatus.BLOCKED_SUSPENSION.value,
    BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value: PaperOrderStatus.BLOCKED_PRICE_LIMIT.value,
    BacktestOrderStatus.EXPIRED.value: PaperOrderStatus.EXPIRED.value,
    BacktestOrderStatus.REJECTED.value: PaperOrderStatus.REJECTED.value,
}


if __name__ == "__main__":
    print(json.dumps(run_fixed_simulation_flow(), ensure_ascii=False, sort_keys=True))
