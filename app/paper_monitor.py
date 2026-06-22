from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .backtest import (
    BacktestOrder,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestPosition,
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
from .db import (
    MarketQuoteSnapshotRecord,
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperMarketSnapshotRecord,
    PaperOrderMarketEventRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    PaperShadowDecisionRecord,
    RiskDecisionRecord,
)
from .paper import Clock, PaperAccountStatus, PaperOrderStatus, SystemClock
from .realtime_quotes import QuoteSelectionService, RealTimeQuoteConfig
from .repositories import SqlAlchemyRepositoryFactory
from .risk import AccountSnapshot, PositionSnapshot, RiskEngine, RiskPolicy, RiskStatus, decimal_to_str, stable_id, stable_json
from .strategy import Signal, SignalType
from .transactions import TransactionRunner


logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
MONEY = Decimal("0.01")


ACTIVE_ORDER_STATUSES = {
    PaperOrderStatus.PAPER_PENDING.value,
    PaperOrderStatus.SUBMITTED.value,
    PaperOrderStatus.PARTIALLY_FILLED.value,
    PaperOrderStatus.BLOCKED_T1.value,
    PaperOrderStatus.BLOCKED_SUSPENSION.value,
    PaperOrderStatus.BLOCKED_PRICE_LIMIT.value,
    "BLOCKED_LIQUIDITY",
    PaperOrderStatus.BLOCKED_STALE_DATA.value,
}
TERMINAL_ORDER_STATUSES = {
    PaperOrderStatus.FILLED.value,
    PaperOrderStatus.CANCELLED.value,
    PaperOrderStatus.EXPIRED.value,
    PaperOrderStatus.REJECTED.value,
}


class PaperFaultInjectionPoint(StrEnum):
    BEFORE_FILL_INSERT = "BEFORE_FILL_INSERT"
    AFTER_FILL_INSERT = "AFTER_FILL_INSERT"
    BEFORE_ACCOUNT_UPDATE = "BEFORE_ACCOUNT_UPDATE"
    AFTER_ACCOUNT_UPDATE = "AFTER_ACCOUNT_UPDATE"
    BEFORE_POSITION_UPDATE = "BEFORE_POSITION_UPDATE"
    BEFORE_LEDGER_INSERT = "BEFORE_LEDGER_INSERT"
    BEFORE_OUTBOX_INSERT = "BEFORE_OUTBOX_INSERT"
    BEFORE_COMMIT = "BEFORE_COMMIT"


class PaperFaultInjector:
    def maybe_fail(self, point: PaperFaultInjectionPoint, context: dict[str, Any]) -> None:
        return None


@dataclass(frozen=True)
class PaperMarketSnapshot:
    quote_id: str | None
    symbol: str
    provider: str
    market_time: datetime
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    current_price: Decimal
    volume: int
    suspended: bool
    previous_close: Decimal
    price_limit_rate: Decimal
    data_checksum: str
    calendar_version: str
    fetched_at: datetime
    validated_at: datetime

    def __post_init__(self) -> None:
        for value in [self.market_time, self.fetched_at, self.validated_at]:
            if value.tzinfo is None:
                raise ValueError("paper market snapshot times must include timezone")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if self.previous_close <= 0:
            raise ValueError("previous_close must be positive")
        if self.price_limit_rate <= 0:
            raise ValueError("price_limit_rate must be positive")

    @property
    def market_event_id(self) -> str:
        return market_event_id(self)

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        provider: str,
        quote_id: str | None = None,
        market_time: datetime,
        trading_date: date,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        current_price: Decimal | None = None,
        volume: int,
        suspended: bool = False,
        previous_close: Decimal,
        price_limit_rate: Decimal = Decimal("0.10"),
        calendar_version: str,
        fetched_at: datetime,
        validated_at: datetime,
    ) -> "PaperMarketSnapshot":
        payload = {
            "symbol": symbol,
            "provider": provider,
            "quote_id": quote_id,
            "market_time": market_time.astimezone(TZ).isoformat(),
            "trading_date": trading_date.isoformat(),
            "open": decimal_to_str(open),
            "high": decimal_to_str(high),
            "low": decimal_to_str(low),
            "close": decimal_to_str(close),
            "current_price": decimal_to_str(current_price or close),
            "volume": volume,
            "suspended": suspended,
            "previous_close": decimal_to_str(previous_close),
            "price_limit_rate": decimal_to_str(price_limit_rate),
            "calendar_version": calendar_version,
        }
        checksum = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
        return cls(
            quote_id=quote_id,
            symbol=symbol,
            provider=provider,
            market_time=market_time,
            trading_date=trading_date,
            open=open,
            high=high,
            low=low,
            close=close,
            current_price=current_price or close,
            volume=volume,
            suspended=suspended,
            previous_close=previous_close,
            price_limit_rate=price_limit_rate,
            data_checksum=checksum,
            calendar_version=calendar_version,
            fetched_at=fetched_at,
            validated_at=validated_at,
        )


@dataclass(frozen=True)
class PaperMonitorConfig:
    enabled: bool = False
    batch_size: int = 50
    market_data_max_age_seconds: float = 60.0
    processing_max_attempts: int = 3
    conflict_retry_attempts: int = 2
    blocked_risk_policy: str = "keep_open"
    ledger_tolerance: Decimal = Decimal("0.01")
    valuation_adjust: str = ""
    settlement_require_all_prices: bool = True
    market_data_mode: str = "FIXTURE"
    shadow_mode: bool = True
    live_fail_closed: bool = True
    provider_priority: tuple[str, ...] = ("fixture", "recorded", "live_paper")
    provider_conflict_pct: Decimal = Decimal("0.03")

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("paper monitor batch_size must be positive")
        if self.market_data_max_age_seconds <= 0:
            raise ValueError("paper market data max age must be positive")
        if self.processing_max_attempts <= 0:
            raise ValueError("paper order processing attempts must be positive")
        if self.conflict_retry_attempts < 0:
            raise ValueError("paper conflict retry attempts must not be negative")
        if self.blocked_risk_policy not in {"keep_open", "reject"}:
            raise ValueError("paper blocked risk policy is invalid")
        if self.market_data_mode not in {"FIXTURE", "RECORDED", "LIVE_PAPER"}:
            raise ValueError("paper monitor market_data_mode is invalid")
        if self.market_data_mode == "LIVE_PAPER" and not self.shadow_mode:
            raise ValueError("LIVE_PAPER non-shadow market monitor is disabled in this project phase")

    @classmethod
    def from_settings(cls, settings) -> "PaperMonitorConfig":
        return cls(
            enabled=getattr(settings, "paper_market_monitor_enabled", False),
            batch_size=getattr(settings, "paper_market_monitor_batch_size", 50),
            market_data_max_age_seconds=getattr(settings, "paper_market_data_max_age_seconds", 60.0),
            processing_max_attempts=getattr(settings, "paper_order_processing_max_attempts", 3),
            conflict_retry_attempts=getattr(settings, "paper_order_conflict_retry_attempts", 2),
            blocked_risk_policy=getattr(settings, "paper_blocked_risk_policy", "keep_open"),
            ledger_tolerance=Decimal(str(getattr(settings, "paper_ledger_tolerance", 0.01))),
            valuation_adjust=getattr(settings, "paper_valuation_adjust", ""),
            settlement_require_all_prices=getattr(settings, "paper_settlement_require_all_prices", True),
            market_data_mode=getattr(settings, "market_data_mode", "FIXTURE"),
            shadow_mode=getattr(settings, "market_live_shadow_mode", True),
            live_fail_closed=getattr(settings, "market_live_fail_closed", True),
        )


class PaperMarketMonitorService:
    def __init__(
        self,
        *,
        session_factory,
        calendar: LocalTradingCalendar,
        clock: Clock | None = None,
        risk_engine: RiskEngine | None = None,
        risk_policy: RiskPolicy | None = None,
        fee_config: FeeConfig | None = None,
        slippage_config: SlippageConfig | None = None,
        config: PaperMonitorConfig | None = None,
        instance_id: str = "paper-monitor-local",
        transaction_runner: TransactionRunner | None = None,
        fault_injector: PaperFaultInjector | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.risk_engine = risk_engine or RiskEngine()
        self.risk_policy = risk_policy or RiskPolicy()
        self.config = config or PaperMonitorConfig()
        self.instance_id = instance_id
        self.transaction_runner = transaction_runner or TransactionRunner(session_factory=session_factory)
        self.fault_injector = fault_injector or PaperFaultInjector()
        self.matching = MatchingEngine(
            FeeModel(fee_config or FeeConfig()),
            SlippageModel(slippage_config or SlippageConfig()),
        )

    def run_once(self, *, trading_date: date, account_id: str | None = None) -> dict[str, Any]:
        now = self._now()
        if not self._is_matching_session(now):
            raise RuntimeError("current time is not an executable paper matching session")
        with self.session_factory() as session:
            orders = self._query_executable_orders(session, now, account_id)
            order_ids = [order.paper_order_id for order in orders]
            session.commit()
        processed = 0
        fills = 0
        blocked = 0
        for order_id in order_ids:
            try:
                result = self.process_order(order_id=order_id, trading_date=trading_date)
                processed += 1
                fills += 1 if result.get("fill_id") else 0
                blocked += 1 if result.get("outcome", "").startswith("BLOCKED") else 0
            except Exception:
                logger.exception("PAPER_TRADING market monitor failed for order %s", order_id)
        return {"processed": processed, "fills": fills, "blocked": blocked}

    def process_order(self, *, order_id: str, trading_date: date) -> dict[str, Any]:
        return self.transaction_runner.run(
            lambda session, attempt: self._process_order_tx(
                session,
                order_id,
                self._snapshot_for_order(order_id, trading_date, session=session),
            )
        )

    def save_market_snapshot(self, snapshot: PaperMarketSnapshot) -> None:
        with self.session_factory() as session:
            _save_market_snapshot(session, snapshot)
            session.commit()

    def _process_order_tx(self, session: Session, order_id: str, snapshot: PaperMarketSnapshot) -> dict[str, Any]:
        now = self._now()
        order = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.paper_order_id == order_id)).first()
        if order is None:
            raise RuntimeError(f"paper order not found: {order_id}")
        if order.status in TERMINAL_ORDER_STATUSES:
            return {"outcome": "TERMINAL"}
        if order.status not in ACTIVE_ORDER_STATUSES or order.remaining_quantity <= 0:
            return {"outcome": "NOT_EXECUTABLE"}
        account = session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.account_id == order.account_id)).first()
        if account is None or account.status != PaperAccountStatus.ACTIVE.value:
            return self._record_block(session, order, snapshot, "BLOCKED_ACCOUNT", "account is not active")
        self._validate_snapshot(snapshot, now)
        if self.config.market_data_mode != "FIXTURE" and self.config.shadow_mode:
            return self._record_shadow_decision(session, account, order, snapshot, now)
        event = self._declare_event(session, order, snapshot, now)
        if event.processing_status == "COMPLETED":
            return {"outcome": event.outcome, "fill_id": event.fill_id}
        if snapshot.suspended:
            return self._complete_block(session, event, order, PaperOrderStatus.BLOCKED_SUSPENSION.value, "suspended", now)
        position = _position_for_update(session, order.account_id, order.symbol)
        if order.side == BacktestSide.SELL.value and position.available_quantity + position.locked_quantity <= 0:
            return self._complete_block(session, event, order, PaperOrderStatus.BLOCKED_T1.value, "T+1 available quantity is zero", now)
        if order.side == BacktestSide.BUY.value:
            risk = self._risk_check(session, account, order, snapshot, position)
            _save_risk_decision(session, risk, account)
            if risk.status == RiskStatus.RISK_OFF.value:
                account.status = PaperAccountStatus.RISK_OFF.value
                return self._complete_block(session, event, order, PaperOrderStatus.BLOCKED_RISK.value, "risk off", now)
            if risk.status not in {RiskStatus.APPROVED.value, RiskStatus.REDUCED.value}:
                return self._complete_block(session, event, order, PaperOrderStatus.BLOCKED_RISK.value, "risk rejected", now)
            if risk.approved_quantity < self.risk_policy.lot_size:
                return self._complete_block(session, event, order, PaperOrderStatus.BLOCKED_RISK.value, "approved quantity below one lot", now)
            if risk.approved_quantity < order.remaining_quantity:
                order.remaining_quantity = max(0, risk.approved_quantity - (risk.approved_quantity % self.risk_policy.lot_size))
        bt_order = _to_backtest_order(order, snapshot.trading_date)
        portfolio = _portfolio_from_records(account, position)
        bar = DailyBar(
            session_date=snapshot.trading_date,
            open=snapshot.open,
            high=snapshot.high,
            low=snapshot.low,
            close=snapshot.current_price,
            volume=snapshot.volume,
            suspended=snapshot.suspended,
        )
        rules = InstrumentRules(
            symbol=order.symbol,
            exchange="SSE",
            board="MAIN",
            lot_size=self.risk_policy.lot_size,
            price_tick=Decimal("0.01"),
            price_limit_rule=snapshot.price_limit_rate,
            is_st=False,
            listing_date=date(1990, 1, 1),
            delisting_date=None,
        )
        fill = self.matching.execute(
            order=bt_order,
            bar=bar,
            previous_close=snapshot.previous_close,
            rules=rules,
            portfolio=portfolio,
            run_id=order.account_id,
        )
        if fill is None:
            mapped = _map_backtest_status(bt_order.status)
            return self._complete_block(session, event, order, mapped, bt_order.rejection_reason or bt_order.status, now)
        fill_key = stable_id("paper-fill", order.paper_order_id, snapshot.market_event_id, str(fill.quantity))
        existing_fill = session.scalars(select(PaperFillRecord).where(PaperFillRecord.fill_idempotency_key == fill_key)).first()
        if existing_fill is not None:
            event.processing_status = "COMPLETED"
            event.outcome = "DUPLICATE_FILL"
            event.fill_id = existing_fill.fill_id
            event.completed_at = now
            session.flush()
            return {"outcome": "DUPLICATE_FILL", "fill_id": existing_fill.fill_id}
        fill_id = stable_id("paper-fill-id", fill_key)
        self.fault_injector.maybe_fail(
            PaperFaultInjectionPoint.BEFORE_FILL_INSERT,
            {"order_id": order.paper_order_id, "fill_key": fill_key, "fill_id": fill_id},
        )
        paper_fill = PaperFillRecord(
            fill_id=fill_id,
            fill_idempotency_key=fill_key,
            market_event_id=snapshot.market_event_id,
            quote_id=snapshot.quote_id,
            paper_order_id=order.paper_order_id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill.quantity,
            raw_price=decimal_to_str(fill.raw_price),
            execution_price=decimal_to_str(fill.execution_price),
            trade_value=decimal_to_str(fill.trade_value),
            commission=decimal_to_str(fill.commission),
            tax=decimal_to_str(fill.tax),
            other_fees=decimal_to_str(fill.other_fees),
            slippage_cost=decimal_to_str(fill.slippage_cost),
            session_date=snapshot.trading_date.isoformat(),
            market_data_checksum=snapshot.data_checksum,
            market_data_provider=snapshot.provider,
            market_time=snapshot.market_time,
            calendar_version=snapshot.calendar_version,
            filled_at=now,
        )
        session.add(paper_fill)
        session.flush()
        self.fault_injector.maybe_fail(PaperFaultInjectionPoint.AFTER_FILL_INSERT, {"order_id": order.paper_order_id, "fill_id": paper_fill.fill_id})
        self._apply_fill(session, account, position, order, paper_fill, snapshot, now)
        order.remaining_quantity = bt_order.remaining_quantity
        order.status = PaperOrderStatus.FILLED.value if order.remaining_quantity == 0 else PaperOrderStatus.PARTIALLY_FILLED.value
        order.updated_at = now
        event.processing_status = "COMPLETED"
        event.outcome = "FILLED" if order.status == PaperOrderStatus.FILLED.value else "PARTIALLY_FILLED"
        event.fill_id = paper_fill.fill_id
        event.completed_at = now
        self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_OUTBOX_INSERT, {"order_id": order.paper_order_id, "fill_id": paper_fill.fill_id})
        _outbox(session, account.account_id, "PAPER_ORDER_FILLED", {"order_id": order.paper_order_id, "fill_id": paper_fill.fill_id}, now)
        self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_COMMIT, {"order_id": order.paper_order_id, "fill_id": paper_fill.fill_id})
        session.flush()
        return {"outcome": event.outcome, "fill_id": paper_fill.fill_id}

    def _apply_fill(
        self,
        session: Session,
        account: PaperAccountRecord,
        position: PaperPositionRecord,
        order: PaperOrderRecord,
        fill: PaperFillRecord,
        snapshot: PaperMarketSnapshot,
        now: datetime,
    ) -> None:
        trade_value = Decimal(fill.trade_value)
        commission = Decimal(fill.commission)
        tax = Decimal(fill.tax)
        other = Decimal(fill.other_fees)
        qty = int(fill.quantity)
        if order.side == BacktestSide.BUY.value:
            total_cost = money(trade_value + commission + other)
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_ACCOUNT_UPDATE, {"order_id": order.paper_order_id})
            account.cash_frozen = decimal_to_str(max(Decimal("0"), Decimal(account.cash_frozen) - total_cost))
            if order.remaining_quantity - qty <= 0:
                account.cash_available = decimal_to_str(Decimal(account.cash_available) + Decimal(account.cash_frozen))
                account.cash_frozen = "0.00"
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.AFTER_ACCOUNT_UPDATE, {"order_id": order.paper_order_id})
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_POSITION_UPDATE, {"order_id": order.paper_order_id})
            _position_buy(position, qty, total_cost, Decimal(fill.execution_price))
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_LEDGER_INSERT, {"order_id": order.paper_order_id})
            _ledger(session, account, "BUY_SETTLED", -trade_value, now, symbol=order.symbol, quantity=qty, ref_id=fill.fill_id)
            _ledger(session, account, "COMMISSION_CHARGED", -commission, now, ref_id=fill.fill_id)
        else:
            proceeds = money(trade_value - commission - tax - other)
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_ACCOUNT_UPDATE, {"order_id": order.paper_order_id})
            account.cash_available = decimal_to_str(Decimal(account.cash_available) + proceeds)
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.AFTER_ACCOUNT_UPDATE, {"order_id": order.paper_order_id})
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_POSITION_UPDATE, {"order_id": order.paper_order_id})
            _position_sell(position, qty, proceeds, Decimal(fill.execution_price))
            self.fault_injector.maybe_fail(PaperFaultInjectionPoint.BEFORE_LEDGER_INSERT, {"order_id": order.paper_order_id})
            _ledger(session, account, "SELL_SETTLED", trade_value, now, symbol=order.symbol, quantity=qty, ref_id=fill.fill_id)
            _ledger(session, account, "COMMISSION_CHARGED", -commission, now, ref_id=fill.fill_id)
            _ledger(session, account, "TAX_CHARGED", -tax, now, ref_id=fill.fill_id)
        account.fees_paid_total = decimal_to_str(Decimal(account.fees_paid_total or "0") + commission + other)
        account.taxes_paid_total = decimal_to_str(Decimal(account.taxes_paid_total or "0") + tax)
        _revalue_account(session, account, {snapshot.symbol: snapshot.current_price})
        account.version += 1
        position.version += 1

    def _risk_check(self, session: Session, account: PaperAccountRecord, order: PaperOrderRecord, snapshot: PaperMarketSnapshot, position: PaperPositionRecord):
        account_snapshot = _account_snapshot(session, account, self._now())
        signal = _paper_signal(order, snapshot)
        stop = snapshot.current_price * Decimal("0.95")
        return self.risk_engine.evaluate(
            signal=signal,
            account=account_snapshot,
            policy=self.risk_policy,
            reference_price=snapshot.current_price,
            stop_price=stop,
        )

    def _query_executable_orders(self, session: Session, now: datetime, account_id: str | None) -> list[PaperOrderRecord]:
        session.flush()
        return SqlAlchemyRepositoryFactory.from_session(session).paper_orders().claim_executable_orders(
            session,
            now=now,
            owner_id=self.instance_id,
            batch_size=self.config.batch_size,
            account_id=account_id,
            active_statuses=ACTIVE_ORDER_STATUSES,
        )

    def _snapshot_for_order(self, order_id: str, trading_date: date, session: Session | None = None) -> PaperMarketSnapshot:
        if session is not None:
            order = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.paper_order_id == order_id)).first()
            if order is None:
                raise RuntimeError(f"paper order not found: {order_id}")
            if self.config.market_data_mode == "FIXTURE":
                row = session.scalars(
                    select(PaperMarketSnapshotRecord)
                    .where(PaperMarketSnapshotRecord.symbol == order.symbol, PaperMarketSnapshotRecord.trading_date == trading_date.isoformat())
                    .order_by(PaperMarketSnapshotRecord.market_time.desc())
                    .limit(1)
                ).first()
                if row is None:
                    raise MarketDataError(f"missing paper market snapshot for {order.symbol} {trading_date}")
                return _snapshot_from_record(row)
            quote = QuoteSelectionService(
                session=session,
                clock=self.clock,
                config=RealTimeQuoteConfig(
                    max_age_seconds=self.config.market_data_max_age_seconds,
                    provider_priority=self.config.provider_priority,
                    provider_conflict_pct=self.config.provider_conflict_pct,
                ),
                expected_calendar_version=self.calendar.version,
            ).select_for_matching(order.symbol, trading_date)
            return _snapshot_from_quote_record(quote)
        with self.session_factory() as owned:
            return self._snapshot_for_order(order_id, trading_date, session=owned)

    def _validate_snapshot(self, snapshot: PaperMarketSnapshot, now: datetime) -> None:
        if snapshot.market_time.astimezone(TZ) > now:
            raise MarketDataError("paper market snapshot is from the future")
        if snapshot.trading_date != now.date():
            raise MarketDataError(f"paper market snapshot trading date mismatch: {snapshot.trading_date} != {now.date()}")
        if self.config.market_data_mode != "FIXTURE" and snapshot.previous_close <= 0:
            raise MarketDataError("realtime quote previous_close is required for matching")
        age = now - snapshot.validated_at.astimezone(TZ)
        if age > timedelta(seconds=self.config.market_data_max_age_seconds):
            raise MarketDataError("paper market snapshot is stale")
        if not self.calendar.is_trading_day(snapshot.trading_date):
            raise MarketDataError("paper market snapshot is not a trading day")

    def _declare_event(self, session: Session, order: PaperOrderRecord, snapshot: PaperMarketSnapshot, now: datetime) -> PaperOrderMarketEventRecord:
        existing = session.scalars(
            select(PaperOrderMarketEventRecord).where(
                PaperOrderMarketEventRecord.paper_order_id == order.paper_order_id,
                PaperOrderMarketEventRecord.market_event_id == snapshot.market_event_id,
            )
        ).first()
        if existing is not None:
            return existing
        event = PaperOrderMarketEventRecord(
            paper_order_id=order.paper_order_id,
            market_event_id=snapshot.market_event_id,
            account_id=order.account_id,
            symbol=order.symbol,
            processing_status="PROCESSING",
            created_at=now,
        )
        session.add(event)
        session.flush()
        return event

    def _record_shadow_decision(
        self,
        session: Session,
        account: PaperAccountRecord,
        order: PaperOrderRecord,
        snapshot: PaperMarketSnapshot,
        now: datetime,
    ) -> dict[str, Any]:
        quote_id = snapshot.quote_id or snapshot.market_event_id
        existing = session.scalars(
            select(PaperShadowDecisionRecord).where(
                PaperShadowDecisionRecord.paper_order_id == order.paper_order_id,
                PaperShadowDecisionRecord.quote_id == quote_id,
            )
        ).first()
        if existing is not None:
            return {"outcome": existing.theoretical_outcome, "shadow": True, "decision_id": existing.decision_id}
        theory = self._shadow_theory(session, account, order, snapshot, now)
        state_checksum = _account_state_checksum(session, account.account_id)
        payload = {
            "environment": "PAPER_TRADING",
            "shadow": True,
            "order_id": order.paper_order_id,
            "account_id": account.account_id,
            "symbol": order.symbol,
            "quote_id": quote_id,
            "market_event_id": snapshot.market_event_id,
            "provider": snapshot.provider,
            "market_time": snapshot.market_time.astimezone(TZ).isoformat(),
            "data_checksum": snapshot.data_checksum,
            "risk_status": theory["risk_status"],
            "theoretical_quantity": theory["theoretical_quantity"],
            "theoretical_price": theory["theoretical_price"],
            "theoretical_fees": theory["theoretical_fees"],
            "theoretical_outcome": theory["theoretical_outcome"],
            "blocked_reason": theory["blocked_reason"],
            "account_state_checksum": state_checksum,
            "notice": "SHADOW模式仅审计，不修改订单、成交、现金、持仓或账本",
        }
        row = PaperShadowDecisionRecord(
            decision_id=stable_id("paper-shadow", order.paper_order_id, quote_id),
            paper_order_id=order.paper_order_id,
            account_id=account.account_id,
            symbol=order.symbol,
            quote_id=quote_id,
            market_event_id=snapshot.market_event_id,
            provider=snapshot.provider,
            market_time=snapshot.market_time,
            quote_checksum=snapshot.data_checksum,
            risk_status=theory["risk_status"],
            theoretical_quantity=theory["theoretical_quantity"],
            theoretical_price=theory["theoretical_price"],
            theoretical_fees=theory["theoretical_fees"],
            theoretical_outcome=theory["theoretical_outcome"],
            blocked_reason=theory["blocked_reason"],
            account_state_checksum=state_checksum,
            payload_json=stable_json(payload),
            created_at=now,
        )
        session.add(row)
        _outbox(session, account.account_id, "SHADOW_MARKET_MONITOR", payload, now)
        session.flush()
        return {"outcome": row.theoretical_outcome, "shadow": True, "decision_id": row.decision_id}

    def _shadow_theory(
        self,
        session: Session,
        account: PaperAccountRecord,
        order: PaperOrderRecord,
        snapshot: PaperMarketSnapshot,
        now: datetime,
    ) -> dict[str, Any]:
        position = session.scalars(
            select(PaperPositionRecord).where(PaperPositionRecord.account_id == order.account_id, PaperPositionRecord.symbol == order.symbol)
        ).first()
        risk_status = "NOT_APPLICABLE"
        if order.side == BacktestSide.BUY.value:
            risk = self._risk_check_shadow(session, account, order, snapshot)
            risk_status = risk.status
            if risk.status == RiskStatus.RISK_OFF.value:
                return _shadow_result("BLOCKED_RISK", 0, snapshot.current_price, Decimal("0"), "risk off", risk_status)
            if risk.status not in {RiskStatus.APPROVED.value, RiskStatus.REDUCED.value}:
                return _shadow_result("BLOCKED_RISK", 0, snapshot.current_price, Decimal("0"), "risk rejected", risk_status)
        if order.side == BacktestSide.SELL.value and (position is None or position.available_quantity + position.locked_quantity <= 0):
            return _shadow_result(PaperOrderStatus.BLOCKED_T1.value, 0, snapshot.current_price, Decimal("0"), "T+1 available quantity is zero", risk_status)
        bt_order = _to_backtest_order(order, snapshot.trading_date)
        portfolio = _portfolio_from_records_no_create(account, position)
        bar = DailyBar(
            session_date=snapshot.trading_date,
            open=snapshot.open,
            high=snapshot.high,
            low=snapshot.low,
            close=snapshot.current_price,
            volume=snapshot.volume,
            suspended=snapshot.suspended,
        )
        rules = InstrumentRules(
            symbol=order.symbol,
            exchange="SSE",
            board="MAIN",
            lot_size=self.risk_policy.lot_size,
            price_tick=Decimal("0.01"),
            price_limit_rule=snapshot.price_limit_rate,
            is_st=False,
            listing_date=date(1990, 1, 1),
            delisting_date=None,
        )
        fill = self.matching.execute(
            order=bt_order,
            bar=bar,
            previous_close=snapshot.previous_close,
            rules=rules,
            portfolio=portfolio,
            run_id=order.account_id,
        )
        if fill is None:
            return _shadow_result(_map_backtest_status(bt_order.status), 0, snapshot.current_price, Decimal("0"), bt_order.rejection_reason or bt_order.status, risk_status)
        return _shadow_result(
            "SHADOW_THEORETICAL_FILL",
            fill.quantity,
            fill.execution_price,
            fill.commission + fill.tax + fill.other_fees,
            "",
            risk_status,
        )

    def _risk_check_shadow(self, session: Session, account: PaperAccountRecord, order: PaperOrderRecord, snapshot: PaperMarketSnapshot):
        account_snapshot = _account_snapshot(session, account, self._now())
        signal = _paper_signal(order, snapshot)
        stop = snapshot.current_price * Decimal("0.95")
        return self.risk_engine.evaluate(
            signal=signal,
            account=account_snapshot,
            policy=self.risk_policy,
            reference_price=snapshot.current_price,
            stop_price=stop,
        )

    def _record_block(self, session: Session, order: PaperOrderRecord, snapshot: PaperMarketSnapshot, status: str, reason: str) -> dict[str, Any]:
        event = self._declare_event(session, order, snapshot, self._now())
        return self._complete_block(session, event, order, status, reason, self._now())

    def _complete_block(self, session: Session, event: PaperOrderMarketEventRecord, order: PaperOrderRecord, status: str, reason: str, now: datetime) -> dict[str, Any]:
        order.status = status
        order.rejection_reason = reason
        order.updated_at = now
        event.processing_status = "COMPLETED"
        event.outcome = status
        event.error_type = status
        event.error_message = reason
        event.completed_at = now
        _outbox(session, order.account_id, "PAPER_ORDER_REJECTED", {"order_id": order.paper_order_id, "reason": reason}, now)
        session.flush()
        return {"outcome": status}

    def _is_matching_session(self, now: datetime) -> bool:
        try:
            if not self.calendar.is_trading_day(now.date()):
                return False
        except Exception:
            return False
        current = now.astimezone(TZ).time()
        return time(9, 30) <= current < time(11, 30) or time(13, 0) <= current < time(15, 0)

    def _now(self) -> datetime:
        return self.clock.now().astimezone(TZ)


class PaperSettlementService:
    def __init__(
        self,
        *,
        session_factory,
        calendar: LocalTradingCalendar,
        clock: Clock | None = None,
        config: PaperMonitorConfig | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.config = config or PaperMonitorConfig()

    def settle(self, *, trading_date: date, account_id: str | None = None) -> dict[str, Any]:
        with self.session_factory() as session:
            results = []
            accounts = session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.account_id.asc())).all()
            for account in accounts:
                if account_id and account.account_id != account_id:
                    continue
                existing = session.scalars(
                    select(PaperAccountSnapshotRecord).where(
                        PaperAccountSnapshotRecord.account_id == account.account_id,
                        PaperAccountSnapshotRecord.trading_date == trading_date.isoformat(),
                    )
                ).first()
                if existing is not None:
                    results.append({"account_id": account.account_id, "snapshot_id": existing.snapshot_id, "existing": True})
                    continue
                try:
                    snapshot = self._settle_account(session, account, trading_date)
                except Exception:
                    session.commit()
                    raise
                results.append({"account_id": account.account_id, "snapshot_id": snapshot.snapshot_id, "existing": False})
            session.commit()
            return {"settled": results}

    def _settle_account(self, session: Session, account: PaperAccountRecord, trading_date: date) -> PaperAccountSnapshotRecord:
        if not self.calendar.is_trading_day(trading_date):
            raise RuntimeError("settlement trading_date is not a trading day")
        positions = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account.account_id, PaperPositionRecord.total_quantity > 0)).all()
        checksums: dict[str, str] = {}
        stale: dict[str, str] = {}
        prices: dict[str, Decimal] = {}
        for position in positions:
            row = session.scalars(
                select(PaperMarketSnapshotRecord)
                .where(PaperMarketSnapshotRecord.symbol == position.symbol, PaperMarketSnapshotRecord.trading_date == trading_date.isoformat())
                .order_by(PaperMarketSnapshotRecord.market_time.desc())
                .limit(1)
            ).first()
            if row is None:
                account.status = PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
                raise RuntimeError(f"missing settlement close snapshot for {position.symbol}")
            snap = _snapshot_from_record(row)
            checksums[position.symbol] = snap.data_checksum
            prices[position.symbol] = snap.close
            position.last_price = decimal_to_str(snap.close)
            position.market_value = decimal_to_str(money(snap.close * Decimal(position.total_quantity)))
            position.unrealized_pnl = decimal_to_str(money((snap.close - Decimal(position.average_cost)) * Decimal(position.total_quantity)))
        _revalue_account(session, account, prices)
        self._assert_balanced(session, account)
        now = self.clock.now().astimezone(TZ)
        market_value = Decimal(account.market_value)
        total_equity = Decimal(account.total_equity)
        exposure = Decimal("0") if total_equity == 0 else (market_value / total_equity).quantize(Decimal("0.000001"))
        snapshot = PaperAccountSnapshotRecord(
            snapshot_id=stable_id("paper-snapshot", account.account_id, trading_date.isoformat()),
            account_id=account.account_id,
            session_date=trading_date.isoformat(),
            trading_date=trading_date.isoformat(),
            cash_available=account.cash_available,
            cash_frozen=account.cash_frozen,
            market_value=account.market_value,
            total_equity=account.total_equity,
            realized_pnl_daily="0.00",
            realized_pnl_total=account.realized_pnl,
            unrealized_pnl=account.unrealized_pnl,
            fees_paid_daily="0.00",
            fees_paid_total=account.fees_paid_total or "0.00",
            taxes_paid_daily="0.00",
            taxes_paid_total=account.taxes_paid_total or "0.00",
            peak_equity=account.peak_equity or account.total_equity,
            drawdown=account.drawdown or "0.000000",
            exposure=decimal_to_str(exposure),
            position_count=len(positions),
            market_data_checksums_json=stable_json(checksums),
            calendar_version=self.calendar.version,
            valuation_adjust=self.config.valuation_adjust,
            stale_valuation_json=stable_json(stale),
            positions_json=stable_json([_position_payload(position) for position in positions]),
            created_at=now,
        )
        session.add(snapshot)
        _outbox(session, account.account_id, "DAILY_REPORT", {"snapshot_id": snapshot.snapshot_id, "trading_date": trading_date.isoformat()}, now)
        session.flush()
        return snapshot

    def _assert_balanced(self, session: Session, account: PaperAccountRecord) -> None:
        total = money(Decimal(account.cash_available) + Decimal(account.cash_frozen) + Decimal(account.market_value))
        if abs(total - Decimal(account.total_equity)) > self.config.ledger_tolerance:
            account.status = PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
            raise RuntimeError("account equity is not balanced")
        active_buy = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.account_id == account.account_id, PaperOrderRecord.side == "BUY", PaperOrderRecord.status.in_(list(ACTIVE_ORDER_STATUSES)))).all()
        if Decimal(account.cash_frozen) > 0 and not active_buy:
            account.status = PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
            raise RuntimeError("frozen cash does not match active buy orders")
        active_sell = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.account_id == account.account_id, PaperOrderRecord.side == "SELL", PaperOrderRecord.status.in_(list(ACTIVE_ORDER_STATUSES)))).all()
        locked = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account.account_id, PaperPositionRecord.locked_quantity > 0)).all()
        if locked and not active_sell:
            account.status = PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value
            raise RuntimeError("frozen position does not match active sell orders")


def market_event_id(snapshot: PaperMarketSnapshot) -> str:
    return stable_id(
        "paper-market-event",
        snapshot.provider,
        snapshot.symbol,
        snapshot.quote_id or "",
        snapshot.trading_date.isoformat(),
        snapshot.market_time.astimezone(TZ).isoformat(),
        snapshot.data_checksum,
        "MARKET_MONITOR",
    )


def _save_market_snapshot(session: Session, snapshot: PaperMarketSnapshot) -> PaperMarketSnapshotRecord:
    existing = session.scalars(select(PaperMarketSnapshotRecord).where(PaperMarketSnapshotRecord.market_event_id == snapshot.market_event_id)).first()
    if existing is not None:
        return existing
    row = PaperMarketSnapshotRecord(
        market_event_id=snapshot.market_event_id,
        provider=snapshot.provider,
        symbol=snapshot.symbol,
        trading_date=snapshot.trading_date.isoformat(),
        market_time=snapshot.market_time,
        open_price=decimal_to_str(snapshot.open),
        high_price=decimal_to_str(snapshot.high),
        low_price=decimal_to_str(snapshot.low),
        close_price=decimal_to_str(snapshot.close),
        current_price=decimal_to_str(snapshot.current_price),
        previous_close=decimal_to_str(snapshot.previous_close),
        volume=snapshot.volume,
        suspended=snapshot.suspended,
        price_limit_rate=decimal_to_str(snapshot.price_limit_rate),
        data_checksum=snapshot.data_checksum,
        calendar_version=snapshot.calendar_version,
        fetched_at=snapshot.fetched_at,
        validated_at=snapshot.validated_at,
        payload_json=stable_json(
            {
                "symbol": snapshot.symbol,
                "provider": snapshot.provider,
                "market_time": snapshot.market_time.astimezone(TZ).isoformat(),
                "trading_date": snapshot.trading_date.isoformat(),
                "open": decimal_to_str(snapshot.open),
                "high": decimal_to_str(snapshot.high),
                "low": decimal_to_str(snapshot.low),
                "close": decimal_to_str(snapshot.close),
                "current_price": decimal_to_str(snapshot.current_price),
                "volume": snapshot.volume,
                "suspended": snapshot.suspended,
                "previous_close": decimal_to_str(snapshot.previous_close),
                "price_limit_rate": decimal_to_str(snapshot.price_limit_rate),
                "data_checksum": snapshot.data_checksum,
                "calendar_version": snapshot.calendar_version,
                "fetched_at": snapshot.fetched_at.astimezone(TZ).isoformat(),
                "validated_at": snapshot.validated_at.astimezone(TZ).isoformat(),
            }
        ),
    )
    session.add(row)
    session.flush()
    return row


def _snapshot_from_record(row: PaperMarketSnapshotRecord) -> PaperMarketSnapshot:
    return PaperMarketSnapshot(
        quote_id=None,
        symbol=row.symbol,
        provider=row.provider,
        market_time=_aware(row.market_time),
        trading_date=date.fromisoformat(row.trading_date),
        open=Decimal(row.open_price),
        high=Decimal(row.high_price),
        low=Decimal(row.low_price),
        close=Decimal(row.close_price),
        current_price=Decimal(row.current_price),
        volume=row.volume,
        suspended=bool(row.suspended),
        previous_close=Decimal(row.previous_close),
        price_limit_rate=Decimal(row.price_limit_rate),
        data_checksum=row.data_checksum,
        calendar_version=row.calendar_version,
        fetched_at=_aware(row.fetched_at),
        validated_at=_aware(row.validated_at),
    )


def _snapshot_from_quote_record(row: MarketQuoteSnapshotRecord) -> PaperMarketSnapshot:
    if row.quality_status != "VALID":
        raise MarketDataError(f"quote quality blocks matching: {row.quality_status}")
    if row.previous_close is None:
        raise MarketDataError("quote previous_close is required for matching")
    return PaperMarketSnapshot(
        quote_id=row.quote_id,
        symbol=row.symbol,
        provider=row.provider,
        market_time=_aware(row.market_time),
        trading_date=date.fromisoformat(row.trading_date),
        open=Decimal(row.open_price),
        high=Decimal(row.high_price),
        low=Decimal(row.low_price),
        close=Decimal(row.last_price),
        current_price=Decimal(row.last_price),
        volume=row.volume,
        suspended=row.suspension_status != "TRADING",
        previous_close=Decimal(row.previous_close),
        price_limit_rate=_price_limit_rate(row),
        data_checksum=row.data_checksum,
        calendar_version=row.calendar_version,
        fetched_at=_aware(row.received_at),
        validated_at=_aware(row.validated_at),
    )


def _price_limit_rate(row: MarketQuoteSnapshotRecord) -> Decimal:
    previous = Decimal(row.previous_close or "0")
    if previous <= 0 or row.price_limit_up is None:
        raise MarketDataError("quote price limit fields are required for matching")
    return ((Decimal(row.price_limit_up) - previous) / previous).quantize(Decimal("0.0001"))


def _to_backtest_order(order: PaperOrderRecord, session_date: date) -> BacktestOrder:
    return BacktestOrder(
        backtest_order_id=order.paper_order_id,
        run_id=order.account_id,
        symbol=order.symbol,
        side=order.side,
        order_type=BacktestOrderType.MARKET_ON_NEXT_OPEN.value if order.order_type == "MARKET_ON_NEXT_OPEN" else BacktestOrderType.LIMIT.value,
        quantity=order.quantity,
        remaining_quantity=order.remaining_quantity,
        limit_price=Decimal(order.limit_price) if order.limit_price else None,
        created_session=session_date,
        earliest_execution_session=session_date,
        expiry_session=session_date,
    )


def _portfolio_from_records(account: PaperAccountRecord, position: PaperPositionRecord) -> Portfolio:
    portfolio = Portfolio(cash_available=Decimal(account.cash_available) + Decimal(account.cash_frozen))
    bt_position = BacktestPosition(
        symbol=position.symbol,
        total_quantity=position.total_quantity,
        available_quantity=position.available_quantity + position.locked_quantity,
        locked_quantity=0,
        today_bought_quantity=position.today_bought_quantity,
        average_cost=Decimal(position.average_cost),
        last_price=Decimal(position.last_price or "0"),
        market_value=Decimal(position.market_value or "0"),
    )
    portfolio.positions[position.symbol] = bt_position
    return portfolio


def _portfolio_from_records_no_create(account: PaperAccountRecord, position: PaperPositionRecord | None) -> Portfolio:
    portfolio = Portfolio(cash_available=Decimal(account.cash_available) + Decimal(account.cash_frozen))
    if position is not None:
        portfolio.positions[position.symbol] = BacktestPosition(
            symbol=position.symbol,
            total_quantity=position.total_quantity,
            available_quantity=position.available_quantity + position.locked_quantity,
            locked_quantity=0,
            today_bought_quantity=position.today_bought_quantity,
            average_cost=Decimal(position.average_cost),
            last_price=Decimal(position.last_price or "0"),
            market_value=Decimal(position.market_value or "0"),
        )
    return portfolio


def _shadow_result(outcome: str, quantity: int, price: Decimal, fees: Decimal, reason: str, risk_status: str) -> dict[str, Any]:
    return {
        "theoretical_outcome": outcome,
        "theoretical_quantity": quantity,
        "theoretical_price": decimal_to_str(price),
        "theoretical_fees": decimal_to_str(fees),
        "blocked_reason": reason,
        "risk_status": risk_status,
    }


def _position_for_update(session: Session, account_id: str, symbol: str) -> PaperPositionRecord:
    position = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account_id, PaperPositionRecord.symbol == symbol)).first()
    if position is not None:
        return position
    position = PaperPositionRecord(
        account_id=account_id,
        symbol=symbol,
        total_quantity=0,
        available_quantity=0,
        today_bought_quantity=0,
        locked_quantity=0,
        average_cost="0.00",
        last_price="0.00",
        market_value="0.00",
        realized_pnl="0.00",
        unrealized_pnl="0.00",
    )
    session.add(position)
    session.flush()
    return position


def _account_state_checksum(session: Session, account_id: str) -> str:
    account = session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.account_id == account_id)).first()
    orders = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.account_id == account_id).order_by(PaperOrderRecord.paper_order_id.asc())).all()
    positions = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account_id).order_by(PaperPositionRecord.symbol.asc())).all()
    ledger = session.scalars(select(PaperLedgerEntryRecord).where(PaperLedgerEntryRecord.account_id == account_id).order_by(PaperLedgerEntryRecord.entry_id.asc())).all()
    payload = {
        "account": None
        if account is None
        else {
            "status": account.status,
            "cash_available": account.cash_available,
            "cash_frozen": account.cash_frozen,
            "market_value": account.market_value,
            "total_equity": account.total_equity,
        },
        "orders": [
            {
                "id": order.paper_order_id,
                "status": order.status,
                "remaining_quantity": order.remaining_quantity,
                "rejection_reason": order.rejection_reason,
            }
            for order in orders
        ],
        "positions": [
            {
                "symbol": position.symbol,
                "total_quantity": position.total_quantity,
                "available_quantity": position.available_quantity,
                "locked_quantity": position.locked_quantity,
            }
            for position in positions
        ],
        "ledger": [entry.entry_id for entry in ledger],
    }
    return stable_id("paper-account-state", stable_json(payload))


def _position_buy(position: PaperPositionRecord, quantity: int, total_cost: Decimal, price: Decimal) -> None:
    new_qty = position.total_quantity + quantity
    old_cost = Decimal(position.average_cost) * Decimal(position.total_quantity)
    position.average_cost = decimal_to_str((old_cost + total_cost) / Decimal(new_qty))
    position.total_quantity = new_qty
    position.today_bought_quantity += quantity
    position.last_price = decimal_to_str(price)
    position.market_value = decimal_to_str(money(price * Decimal(new_qty)))
    position.unrealized_pnl = decimal_to_str(money((price - Decimal(position.average_cost)) * Decimal(new_qty)))


def _position_sell(position: PaperPositionRecord, quantity: int, proceeds_after_fee: Decimal, price: Decimal) -> None:
    if quantity > position.total_quantity:
        raise RuntimeError("sell quantity exceeds position")
    cost = Decimal(position.average_cost) * Decimal(quantity)
    position.realized_pnl = decimal_to_str(Decimal(position.realized_pnl or "0") + proceeds_after_fee - cost)
    position.total_quantity -= quantity
    if position.locked_quantity >= quantity:
        position.locked_quantity -= quantity
    else:
        rest = quantity - position.locked_quantity
        position.locked_quantity = 0
        position.available_quantity = max(0, position.available_quantity - rest)
    if position.total_quantity == 0:
        position.average_cost = "0.00"
    position.last_price = decimal_to_str(price if position.total_quantity else Decimal("0"))
    position.market_value = decimal_to_str(money(price * Decimal(position.total_quantity)))
    position.unrealized_pnl = decimal_to_str(money((price - Decimal(position.average_cost)) * Decimal(position.total_quantity))) if position.total_quantity else "0.00"


def _revalue_account(session: Session, account: PaperAccountRecord, prices: dict[str, Decimal]) -> None:
    positions = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account.account_id)).all()
    for position in positions:
        if position.symbol in prices and position.total_quantity > 0:
            price = prices[position.symbol]
            position.last_price = decimal_to_str(price)
            position.market_value = decimal_to_str(money(price * Decimal(position.total_quantity)))
            position.unrealized_pnl = decimal_to_str(money((price - Decimal(position.average_cost)) * Decimal(position.total_quantity)))
    market_value = money(sum((Decimal(p.market_value or "0") for p in positions), Decimal("0")))
    account.market_value = decimal_to_str(market_value)
    account.unrealized_pnl = decimal_to_str(sum((Decimal(p.unrealized_pnl or "0") for p in positions), Decimal("0")))
    account.realized_pnl = decimal_to_str(sum((Decimal(p.realized_pnl or "0") for p in positions), Decimal("0")))
    total = money(Decimal(account.cash_available) + Decimal(account.cash_frozen) + market_value)
    account.total_equity = decimal_to_str(total)
    peak = max(Decimal(account.peak_equity or "0"), total)
    account.peak_equity = decimal_to_str(peak)
    account.drawdown = decimal_to_str(Decimal("0") if peak == 0 else ((peak - total) / peak).quantize(Decimal("0.000001")))


def _account_snapshot(session: Session, account: PaperAccountRecord, now: datetime) -> AccountSnapshot:
    positions = tuple(
        PositionSnapshot(
            symbol=p.symbol,
            quantity=p.total_quantity,
            available_quantity=p.available_quantity,
            average_cost=Decimal(p.average_cost),
            current_price=Decimal(p.last_price or "0"),
            market_value=Decimal(p.market_value or "0"),
            industry=p.industry,
        )
        for p in session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account.account_id, PaperPositionRecord.total_quantity > 0)).all()
    )
    return AccountSnapshot(
        account_id=account.account_id,
        as_of=now,
        total_equity=max(Decimal(account.total_equity), Decimal("0.01")),
        available_cash=Decimal(account.cash_available),
        market_value=Decimal(account.market_value or "0"),
        frozen_cash=Decimal(account.cash_frozen or "0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal(account.unrealized_pnl or "0"),
        peak_equity=max(Decimal(account.peak_equity or "0"), Decimal(account.total_equity), Decimal("0.01")),
        consecutive_losses=0,
        positions=positions,
    )


def _paper_signal(order: PaperOrderRecord, snapshot: PaperMarketSnapshot) -> Signal:
    return Signal(
        symbol=order.symbol,
        action=SignalType.BUY_WATCH.value,
        score=100,
        price=float(snapshot.current_price),
        stop_price=float(snapshot.current_price * Decimal("0.95")),
        take_profit_1=float(snapshot.current_price * Decimal("1.05")),
        take_profit_2=float(snapshot.current_price * Decimal("1.08")),
        suggested_shares=order.remaining_quantity,
        reason="paper fill risk recheck",
        market_trade_date=snapshot.trading_date,
        market_fetched_at=snapshot.fetched_at,
        signal_generated_at=snapshot.validated_at,
        strategy_name="paper_runtime",
        strategy_version="1.0.0",
        parameter_version="paper_runtime",
        parameter_snapshot="{}",
        market_as_of_date=snapshot.trading_date,
        market_data_source=snapshot.provider,
        market_data_adjust="",
        signal_type=SignalType.BUY_WATCH.value,
        score_breakdown={},
        reasons=["paper fill risk recheck"],
        invalidation_conditions=[],
        reference_price=float(snapshot.current_price),
        stop_loss_price=float(snapshot.current_price * Decimal("0.95")),
        take_profit_1_price=float(snapshot.current_price * Decimal("1.05")),
        take_profit_2_price=float(snapshot.current_price * Decimal("1.08")),
        market_data_checksum=snapshot.data_checksum,
        market_calendar_version=snapshot.calendar_version,
    )


def _save_risk_decision(session: Session, decision, account: PaperAccountRecord) -> None:
    existing = session.scalars(select(RiskDecisionRecord).where(RiskDecisionRecord.decision_id == decision.decision_id)).first()
    if existing is not None:
        return
    session.add(
        RiskDecisionRecord(
            decision_id=decision.decision_id,
            signal_identity=decision.signal_identity,
            account_snapshot_hash=decision.account_snapshot_hash,
            account_snapshot_json=stable_json({"account_id": account.account_id}),
            risk_policy_version=decision.risk_policy_version,
            risk_policy_snapshot="{}",
            status=decision.status,
            requested_quantity=decision.requested_quantity,
            approved_quantity=decision.approved_quantity,
            approved_notional=decimal_to_str(decision.approved_notional),
            risk_amount=decimal_to_str(decision.risk_amount),
            rules_json=stable_json([rule.to_dict() for rule in decision.rules]),
            rejection_reasons_json=stable_json(list(decision.rejection_reasons)),
            created_at=decision.created_at,
        )
    )


def _ledger(session: Session, account: PaperAccountRecord, event_type: str, amount: Decimal, occurred_at: datetime, *, symbol: str | None = None, quantity: int = 0, ref_id: str = "") -> None:
    session.add(
        PaperLedgerEntryRecord(
            entry_id=stable_id("paper-ledger", account.account_id, event_type, ref_id, str(quantity), decimal_to_str(amount)),
            account_id=account.account_id,
            event_type=event_type,
            amount=decimal_to_str(money(amount)),
            cash_available_after=account.cash_available,
            cash_frozen_after=account.cash_frozen,
            symbol=symbol,
            quantity=quantity,
            ref_id=ref_id,
            payload_json="{}",
            occurred_at=occurred_at,
        )
    )


def _outbox(session: Session, account_id: str, notification_type: str, payload: dict[str, Any], now: datetime) -> None:
    payload = {"environment": "PAPER_TRADING", **payload}
    dedupe = stable_id("paper-notify", account_id, notification_type, stable_json(payload))
    if session.scalars(select(NotificationOutboxRecord).where(NotificationOutboxRecord.dedupe_key == dedupe)).first() is not None:
        return
    session.add(
        NotificationOutboxRecord(
            message_id=stable_id("paper-message", dedupe),
            dedupe_key=dedupe,
            account_id=account_id,
            notification_type=notification_type,
            payload_json=stable_json(payload),
            status="PENDING",
            retry_count=0,
            last_error="",
            created_at=now,
            updated_at=now,
        )
    )


def _map_backtest_status(status: str) -> str:
    mapping = {
        BacktestOrderStatus.BLOCKED_T1.value: PaperOrderStatus.BLOCKED_T1.value,
        BacktestOrderStatus.BLOCKED_SUSPENSION.value: PaperOrderStatus.BLOCKED_SUSPENSION.value,
        BacktestOrderStatus.BLOCKED_PRICE_LIMIT.value: PaperOrderStatus.BLOCKED_PRICE_LIMIT.value,
        BacktestOrderStatus.BLOCKED_LIQUIDITY.value: "BLOCKED_LIQUIDITY",
        BacktestOrderStatus.REJECTED.value: PaperOrderStatus.REJECTED.value,
        BacktestOrderStatus.EXPIRED.value: PaperOrderStatus.EXPIRED.value,
    }
    return mapping.get(status, status)


def _position_payload(position: PaperPositionRecord) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "total_quantity": position.total_quantity,
        "available_quantity": position.available_quantity,
        "locked_quantity": position.locked_quantity,
        "average_cost": position.average_cost,
        "market_value": position.market_value,
    }


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)
