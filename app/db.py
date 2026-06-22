from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import get_settings
from .data_provider import utc_now


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_signals_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    signal_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    db_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now, index=True, nullable=True)
    market_trade_date: Mapped[str | None] = mapped_column(String(10), index=True, nullable=True)
    market_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    parameter_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameter_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_as_of_date: Mapped[str | None] = mapped_column(String(10), index=True, nullable=True)
    market_data_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_data_adjust: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signal_type: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    score_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidation_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_1_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_2_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_data_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_calendar_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    take_profit_1: Mapped[float] = mapped_column(Float)
    take_profit_2: Mapped[float] = mapped_column(Float)
    suggested_shares: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    shares: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RiskDecisionRecord(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        UniqueConstraint(
            "signal_identity",
            "account_snapshot_hash",
            "risk_policy_version",
            name="uq_risk_decision_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    signal_identity: Mapped[str] = mapped_column(String(240), index=True)
    account_snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    account_snapshot_json: Mapped[str] = mapped_column(Text)
    risk_policy_version: Mapped[str] = mapped_column(String(64), index=True)
    risk_policy_snapshot: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)
    requested_quantity: Mapped[int] = mapped_column(Integer)
    approved_quantity: Mapped[int] = mapped_column(Integer)
    approved_notional: Mapped[str] = mapped_column(String(32))
    risk_amount: Mapped[str] = mapped_column(String(32))
    rules_json: Mapped[str] = mapped_column(Text)
    rejection_reasons_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ProposedOrderRecord(Base):
    __tablename__ = "proposed_orders"
    __table_args__ = (
        UniqueConstraint("signal_identity", "risk_decision_id", name="uq_order_proposal_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    signal_identity: Mapped[str] = mapped_column(String(240), index=True)
    risk_decision_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    reference_price: Mapped[str] = mapped_column(String(32))
    stop_price: Mapped[str] = mapped_column(String(32))
    take_profit_1: Mapped[str] = mapped_column(String(32))
    take_profit_2: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class BacktestRunRecord(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    config_json: Mapped[str] = mapped_column(Text)
    config_checksum: Mapped[str] = mapped_column(String(64), index=True)
    strategy_name: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), index=True)
    parameter_version: Mapped[str] = mapped_column(String(64), index=True)
    calendar_version: Mapped[str] = mapped_column(String(64), index=True)
    instrument_rules_version: Mapped[str] = mapped_column(String(64), index=True)
    corporate_action_version: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    data_checksums_json: Mapped[str] = mapped_column(Text)
    code_version: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    result_summary_json: Mapped[str] = mapped_column(Text)


class BacktestOrderRecord(Base):
    __tablename__ = "backtest_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    remaining_quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_session: Mapped[str] = mapped_column(String(10), index=True)
    earliest_execution_session: Mapped[str] = mapped_column(String(10), index=True)
    expiry_session: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    source_signal_identity: Mapped[str] = mapped_column(String(240), index=True)
    risk_decision_id: Mapped[str] = mapped_column(String(80), index=True)
    corporate_action_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)


class BacktestFillRecord(Base):
    __tablename__ = "backtest_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    raw_price: Mapped[str] = mapped_column(String(32))
    execution_price: Mapped[str] = mapped_column(String(32))
    trade_value: Mapped[str] = mapped_column(String(32))
    commission: Mapped[str] = mapped_column(String(32))
    tax: Mapped[str] = mapped_column(String(32))
    other_fees: Mapped[str] = mapped_column(String(32))
    slippage_cost: Mapped[str] = mapped_column(String(32))
    session_date: Mapped[str] = mapped_column(String(10), index=True)


class BacktestDailyEquityRecord(Base):
    __tablename__ = "backtest_daily_equity"
    __table_args__ = (UniqueConstraint("run_id", "session_date", name="uq_backtest_daily_equity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    cash: Mapped[str] = mapped_column(String(32))
    market_value: Mapped[str] = mapped_column(String(32))
    total_equity: Mapped[str] = mapped_column(String(32))
    daily_return: Mapped[str] = mapped_column(String(32))
    peak_equity: Mapped[str] = mapped_column(String(32))
    drawdown: Mapped[str] = mapped_column(String(32))
    exposure: Mapped[str] = mapped_column(String(32))


class BacktestPositionRecord(Base):
    __tablename__ = "backtest_positions"
    __table_args__ = (UniqueConstraint("run_id", "session_date", "symbol", name="uq_backtest_position_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    total_quantity: Mapped[int] = mapped_column(Integer)
    available_quantity: Mapped[int] = mapped_column(Integer)
    locked_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_cost: Mapped[str] = mapped_column(String(32))
    last_price: Mapped[str] = mapped_column(String(32))
    market_value: Mapped[str] = mapped_column(String(32))
    unrealized_pnl: Mapped[str] = mapped_column(String(32))


class CorporateActionRecord(Base):
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    announcement_date: Mapped[str] = mapped_column(String(10), index=True)
    record_date: Mapped[str] = mapped_column(String(10), index=True)
    ex_date: Mapped[str] = mapped_column(String(10), index=True)
    payment_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tradable_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80))
    source_version: Mapped[str] = mapped_column(String(80))
    data_checksum: Mapped[str] = mapped_column(String(64), index=True)


class DividendEntitlementRecord(Base):
    __tablename__ = "dividend_entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entitlement_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    action_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    eligible_quantity: Mapped[int] = mapped_column(Integer)
    gross_cash: Mapped[str] = mapped_column(String(32))
    tax: Mapped[str] = mapped_column(String(32))
    net_cash: Mapped[str] = mapped_column(String(32))
    record_date: Mapped[str] = mapped_column(String(10), index=True)
    payment_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)


class BacktestCorporateActionEventRecord(Base):
    __tablename__ = "backtest_corporate_action_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    action_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    before_json: Mapped[str] = mapped_column(Text)
    after_json: Mapped[str] = mapped_column(Text)
    amount: Mapped[str] = mapped_column(String(32))


class PaperAccountRecord(Base):
    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), index=True)
    base_currency: Mapped[str] = mapped_column(String(8), default="CNY")
    initial_cash: Mapped[str] = mapped_column(String(32))
    cash_available: Mapped[str] = mapped_column(String(32))
    cash_frozen: Mapped[str] = mapped_column(String(32))
    market_value: Mapped[str] = mapped_column(String(32), default="0.00")
    total_equity: Mapped[str] = mapped_column(String(32))
    realized_pnl: Mapped[str] = mapped_column(String(32), default="0.00")
    unrealized_pnl: Mapped[str] = mapped_column(String(32), default="0.00")
    fees_paid_total: Mapped[str] = mapped_column(String(32), default="0.00")
    taxes_paid_total: Mapped[str] = mapped_column(String(32), default="0.00")
    peak_equity: Mapped[str] = mapped_column(String(32), default="0.00")
    drawdown: Mapped[str] = mapped_column(String(32), default="0.000000")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperPositionRecord(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_paper_position_account_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    total_quantity: Mapped[int] = mapped_column(Integer)
    available_quantity: Mapped[int] = mapped_column(Integer)
    today_bought_quantity: Mapped[int] = mapped_column(Integer, default=0)
    locked_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_cost: Mapped[str] = mapped_column(String(32))
    last_price: Mapped[str] = mapped_column(String(32), default="0.00")
    market_value: Mapped[str] = mapped_column(String(32), default="0.00")
    realized_pnl: Mapped[str] = mapped_column(String(32), default="0.00")
    unrealized_pnl: Mapped[str] = mapped_column(String(32), default="0.00")
    industry: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperOrderRecord(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint("proposal_id", "active_key", name="uq_paper_order_active_proposal"),
        UniqueConstraint("idempotency_key", name="uq_paper_order_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    active_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    remaining_quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    source_signal_identity: Mapped[str] = mapped_column(String(240), default="", index=True)
    risk_decision_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    earliest_execution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    processing_owner: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperFillRecord(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    paper_order_id: Mapped[str] = mapped_column(String(80), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    raw_price: Mapped[str] = mapped_column(String(32))
    execution_price: Mapped[str] = mapped_column(String(32))
    trade_value: Mapped[str] = mapped_column(String(32))
    commission: Mapped[str] = mapped_column(String(32))
    tax: Mapped[str] = mapped_column(String(32))
    other_fees: Mapped[str] = mapped_column(String(32))
    slippage_cost: Mapped[str] = mapped_column(String(32))
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    market_event_id: Mapped[str | None] = mapped_column(String(160), index=True, nullable=True)
    quote_id: Mapped[str | None] = mapped_column(String(160), index=True, nullable=True)
    fill_idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, index=True, nullable=True)
    market_data_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_data_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    market_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    calendar_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperLedgerEntryRecord(Base):
    __tablename__ = "paper_ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[str] = mapped_column(String(32), default="0.00")
    cash_available_after: Mapped[str] = mapped_column(String(32))
    cash_frozen_after: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    ref_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperAccountSnapshotRecord(Base):
    __tablename__ = "paper_account_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "session_date", name="uq_paper_account_snapshot_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(80), unique=True, index=True, nullable=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    trading_date: Mapped[str | None] = mapped_column(String(10), index=True, nullable=True)
    cash_available: Mapped[str] = mapped_column(String(32))
    cash_frozen: Mapped[str] = mapped_column(String(32))
    market_value: Mapped[str] = mapped_column(String(32))
    total_equity: Mapped[str] = mapped_column(String(32))
    realized_pnl_daily: Mapped[str] = mapped_column(String(32), default="0.00")
    realized_pnl_total: Mapped[str] = mapped_column(String(32), default="0.00")
    unrealized_pnl: Mapped[str] = mapped_column(String(32), default="0.00")
    fees_paid_daily: Mapped[str] = mapped_column(String(32), default="0.00")
    fees_paid_total: Mapped[str] = mapped_column(String(32), default="0.00")
    taxes_paid_daily: Mapped[str] = mapped_column(String(32), default="0.00")
    taxes_paid_total: Mapped[str] = mapped_column(String(32), default="0.00")
    peak_equity: Mapped[str] = mapped_column(String(32), default="0.00")
    drawdown: Mapped[str] = mapped_column(String(32), default="0.000000")
    exposure: Mapped[str] = mapped_column(String(32), default="0.000000")
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    market_data_checksums_json: Mapped[str] = mapped_column(Text, default="{}")
    calendar_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valuation_adjust: Mapped[str] = mapped_column(String(16), default="")
    stale_valuation_json: Mapped[str] = mapped_column(Text, default="{}")
    positions_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperMarketSnapshotRecord(Base):
    __tablename__ = "paper_market_snapshots"
    __table_args__ = (UniqueConstraint("market_event_id", name="uq_paper_market_snapshot_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_event_id: Mapped[str] = mapped_column(String(160), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open_price: Mapped[str] = mapped_column(String(32))
    high_price: Mapped[str] = mapped_column(String(32))
    low_price: Mapped[str] = mapped_column(String(32))
    close_price: Mapped[str] = mapped_column(String(32))
    current_price: Mapped[str] = mapped_column(String(32))
    previous_close: Mapped[str] = mapped_column(String(32))
    volume: Mapped[int] = mapped_column(Integer)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    price_limit_rate: Mapped[str] = mapped_column(String(32), default="0.10")
    data_checksum: Mapped[str] = mapped_column(String(64), index=True)
    calendar_version: Mapped[str] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class PaperOrderMarketEventRecord(Base):
    __tablename__ = "paper_order_market_events"
    __table_args__ = (UniqueConstraint("paper_order_id", "market_event_id", name="uq_paper_order_market_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_order_id: Mapped[str] = mapped_column(String(80), index=True)
    market_event_id: Mapped[str] = mapped_column(String(160), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    processing_status: Mapped[str] = mapped_column(String(24), index=True)
    outcome: Mapped[str] = mapped_column(String(64), default="")
    fill_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    error_type: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketQuoteSnapshotRecord(Base):
    __tablename__ = "market_quote_snapshots"
    __table_args__ = (
        UniqueConstraint("provider", "symbol", "market_time", "data_checksum", name="uq_market_quote_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quote_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    provider_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    exchange: Mapped[str] = mapped_column(String(16), index=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sequence: Mapped[str | None] = mapped_column(String(64), nullable=True)
    open_price: Mapped[str] = mapped_column(String(32))
    high_price: Mapped[str] = mapped_column(String(32))
    low_price: Mapped[str] = mapped_column(String(32))
    last_price: Mapped[str] = mapped_column(String(32))
    previous_close: Mapped[str | None] = mapped_column(String(32), nullable=True)
    volume: Mapped[int] = mapped_column(Integer)
    amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bid_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ask_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suspension_status: Mapped[str] = mapped_column(String(24), index=True)
    price_limit_up: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_limit_down: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_checksum: Mapped[str] = mapped_column(String(64), index=True)
    calendar_version: Mapped[str] = mapped_column(String(64), index=True)
    raw_schema_version: Mapped[str] = mapped_column(String(64))
    quality_status: Mapped[str] = mapped_column(String(24), index=True)
    quality_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class MarketDataProviderStatusRecord(Base):
    __tablename__ = "market_data_provider_status"
    __table_args__ = (UniqueConstraint("provider", "instance_id", name="uq_market_provider_instance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    instance_id: Mapped[str] = mapped_column(String(80), index=True)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_quote_market_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    average_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    p95_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    stale_symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    out_of_order_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), index=True)
    last_error_type: Mapped[str] = mapped_column(String(80), default="")
    last_error_message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PaperShadowDecisionRecord(Base):
    __tablename__ = "paper_shadow_decisions"
    __table_args__ = (UniqueConstraint("paper_order_id", "quote_id", name="uq_paper_shadow_order_quote"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    paper_order_id: Mapped[str] = mapped_column(String(80), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quote_id: Mapped[str] = mapped_column(String(160), index=True)
    market_event_id: Mapped[str] = mapped_column(String(160), index=True)
    provider: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    market_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    quote_checksum: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    risk_status: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    theoretical_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    theoretical_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    theoretical_fees: Mapped[str | None] = mapped_column(String(32), nullable=True)
    theoretical_outcome: Mapped[str] = mapped_column(String(64))
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    account_state_checksum: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class QuoteComparisonRecord(Base):
    __tablename__ = "quote_comparisons"
    __table_args__ = (UniqueConstraint("comparison_id", name="uq_quote_comparison_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comparison_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    live_provider: Mapped[str] = mapped_column(String(80), index=True)
    reference_provider: Mapped[str] = mapped_column(String(80), index=True)
    live_quote_id: Mapped[str] = mapped_column(String(160), index=True)
    reference_quote_id: Mapped[str] = mapped_column(String(160), index=True)
    price_diff_bps: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    quality_status: Mapped[str] = mapped_column(String(24), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class MarketDataQualityDailyRecord(Base):
    __tablename__ = "market_data_quality_daily"
    __table_args__ = (UniqueConstraint("trading_date", "provider", "symbol", name="uq_market_quality_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quote_received_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    stale_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rate: Mapped[str] = mapped_column(String(32), default="")
    out_of_order_rate: Mapped[str] = mapped_column(String(32), default="")
    missing_symbol_rate: Mapped[str] = mapped_column(String(32), default="")
    average_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p50_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p95_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p99_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_availability: Mapped[str] = mapped_column(String(32), default="")
    schema_error_count: Mapped[int] = mapped_column(Integer, default=0)
    price_conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    suspension_unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    limit_rule_unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class RecordedQuoteFileRecord(Base):
    __tablename__ = "recorded_quote_files"
    __table_args__ = (UniqueConstraint("recording_id", name="uq_recorded_quote_file_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recording_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    provider_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_checksum: Mapped[str] = mapped_column(String(64), index=True)
    quality_status: Mapped[str] = mapped_column(String(24), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ProviderShadowRunRecord(Base):
    __tablename__ = "provider_shadow_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_provider_shadow_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    provider_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    symbol_universe_version: Mapped[str] = mapped_column(String(80))
    configured_symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), index=True)
    quote_received_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    stale_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_quote_count: Mapped[int] = mapped_column(Integer, default=0)
    out_of_order_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_error_count: Mapped[int] = mapped_column(Integer, default=0)
    network_error_count: Mapped[int] = mapped_column(Integer, default=0)
    rate_limit_count: Mapped[int] = mapped_column(Integer, default=0)
    availability: Mapped[str] = mapped_column(String(32), default="")
    average_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p50_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p95_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p99_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_symbol_rate: Mapped[str] = mapped_column(String(32), default="")
    account_state_before_checksum: Mapped[str] = mapped_column(String(80), default="")
    account_state_after_checksum: Mapped[str] = mapped_column(String(80), default="")
    fills_before_count: Mapped[int] = mapped_column(Integer, default=0)
    fills_after_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(String(48), index=True)
    failure_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class MarketDataAdmissionResultRecord(Base):
    __tablename__ = "market_data_admission_results"
    __table_args__ = (UniqueConstraint("provider", "evaluated_at", name="uq_market_admission_result"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    complete_trading_days: Mapped[int] = mapped_column(Integer, default=0)
    failure_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    policy_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")


class MarketDataAdmissionHistoryRecord(Base):
    __tablename__ = "market_data_admission_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MarketDataDegradationEventRecord(Base):
    __tablename__ = "market_data_degradation_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_market_degradation_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(24), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    mode_from: Mapped[str] = mapped_column(String(32), default="LIVE_PAPER")
    mode_to: Mapped[str] = mapped_column(String(32), default="RECORDED")
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class MarketDataShadowDailyReportRecord(Base):
    __tablename__ = "market_data_shadow_daily_reports"
    __table_args__ = (UniqueConstraint("provider", "trading_date", name="uq_market_shadow_daily_report"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    report_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProviderConnectivityTestRecord(Base):
    __tablename__ = "provider_connectivity_tests"
    __table_args__ = (UniqueConstraint("test_id", name="uq_provider_connectivity_test_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(48), index=True)
    error_type: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    quote_received_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class ProposalStatusHistoryRecord(Base):
    __tablename__ = "proposal_status_history"
    __table_args__ = (UniqueConstraint("proposal_id", "to_status", "changed_at", name="uq_proposal_status_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(String(80), index=True)
    from_status: Mapped[str] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), index=True)
    operator: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ScheduledTaskRunRecord(Base):
    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        UniqueConstraint("task_key", name="uq_scheduled_task_key"),
        UniqueConstraint("idempotency_key", name="uq_scheduled_task_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[str | None] = mapped_column(String(80), unique=True, index=True, nullable=True)
    task_key: Mapped[str] = mapped_column(String(160), index=True)
    account_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    trading_date: Mapped[str | None] = mapped_column(String(10), index=True, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), index=True, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    error_type: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationOutboxRecord(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_outbox_dedupe"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    notification_type: Mapped[str] = mapped_column(String(48), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TaskLeaseRecord(Base):
    __tablename__ = "task_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)


class RuntimeRecoveryRunRecord(Base):
    __tablename__ = "runtime_recovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recovery_run_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")


class RuntimeRecoveryIssueRecord(Base):
    __tablename__ = "runtime_recovery_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    recovery_run_id: Mapped[str] = mapped_column(String(80), index=True)
    account_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    issue_type: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(24), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    migrate_signal_table()
    migrate_backtest_table()
    migrate_paper_runtime_tables()


def migrate_signal_table() -> None:
    inspector = inspect(engine)
    if "signals" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("signals")}
    columns = {
        "signal_generated_at": "DATETIME",
        "db_written_at": "DATETIME",
        "market_trade_date": "VARCHAR(10)",
        "market_fetched_at": "DATETIME",
        "strategy_name": "VARCHAR(64)",
        "strategy_version": "VARCHAR(32)",
        "parameter_version": "VARCHAR(64)",
        "parameter_snapshot": "TEXT",
        "market_as_of_date": "VARCHAR(10)",
        "market_data_source": "VARCHAR(64)",
        "market_data_adjust": "VARCHAR(16)",
        "signal_type": "VARCHAR(16)",
        "score_breakdown": "TEXT",
        "reasons": "TEXT",
        "invalidation_conditions": "TEXT",
        "reference_price": "FLOAT",
        "stop_loss_price": "FLOAT",
        "take_profit_1_price": "FLOAT",
        "take_profit_2_price": "FLOAT",
        "market_data_checksum": "VARCHAR(64)",
        "market_calendar_version": "VARCHAR(64)",
        "dedupe_key": "VARCHAR(160)",
    }
    with engine.begin() as connection:
        for column, ddl_type in columns.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE signals ADD COLUMN {column} {_ddl_type(ddl_type)}"))
        if settings.database_url.startswith("sqlite"):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_dedupe_key "
                    "ON signals (dedupe_key) WHERE dedupe_key IS NOT NULL"
                )
            )


def migrate_backtest_table() -> None:
    inspector = inspect(engine)
    if "backtest_runs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("backtest_runs")}
    with engine.begin() as connection:
        if "corporate_action_version" not in existing:
            connection.execute(text("ALTER TABLE backtest_runs ADD COLUMN corporate_action_version VARCHAR(64)"))
    if "backtest_orders" in inspector.get_table_names():
        existing_orders = {column["name"] for column in inspector.get_columns("backtest_orders")}
        with engine.begin() as connection:
            if "corporate_action_id" not in existing_orders:
                connection.execute(text("ALTER TABLE backtest_orders ADD COLUMN corporate_action_id VARCHAR(80)"))
    if "backtest_positions" in inspector.get_table_names():
        existing_positions = {column["name"] for column in inspector.get_columns("backtest_positions")}
        with engine.begin() as connection:
            if "locked_quantity" not in existing_positions:
                connection.execute(text("ALTER TABLE backtest_positions ADD COLUMN locked_quantity INTEGER DEFAULT 0"))


def migrate_paper_runtime_tables() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "scheduled_task_runs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("scheduled_task_runs")}
    columns = {
        "task_run_id": "VARCHAR(80)",
        "trading_date": "VARCHAR(10)",
        "scheduled_at": "DATETIME",
        "idempotency_key": "VARCHAR(160)",
        "lease_owner": "VARCHAR(80)",
        "error_type": "VARCHAR(80) DEFAULT ''",
    }
    with engine.begin() as connection:
        for column, ddl_type in columns.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE scheduled_task_runs ADD COLUMN {column} {_ddl_type(ddl_type)}"))
        if settings.database_url.startswith("sqlite"):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_task_idempotency "
                    "ON scheduled_task_runs (idempotency_key) WHERE idempotency_key IS NOT NULL"
                )
            )
    _add_columns_if_missing(
        inspector,
        "paper_accounts",
        {
            "fees_paid_total": "VARCHAR(32) DEFAULT '0.00'",
            "taxes_paid_total": "VARCHAR(32) DEFAULT '0.00'",
            "peak_equity": "VARCHAR(32) DEFAULT '0.00'",
            "drawdown": "VARCHAR(32) DEFAULT '0.000000'",
        },
    )
    _add_columns_if_missing(
        inspector,
        "paper_orders",
        {
            "submitted_at": "DATETIME",
            "earliest_execution_at": "DATETIME",
            "processing_owner": "VARCHAR(80)",
            "processing_started_at": "DATETIME",
        },
    )
    _add_columns_if_missing(
        inspector,
        "paper_fills",
        {
            "market_event_id": "VARCHAR(160)",
            "quote_id": "VARCHAR(160)",
            "fill_idempotency_key": "VARCHAR(200)",
            "market_data_checksum": "VARCHAR(64)",
            "market_data_provider": "VARCHAR(80)",
            "market_time": "DATETIME",
            "calendar_version": "VARCHAR(64)",
        },
    )
    _add_columns_if_missing(
        inspector,
        "paper_account_snapshots",
        {
            "snapshot_id": "VARCHAR(80)",
            "trading_date": "VARCHAR(10)",
            "realized_pnl_daily": "VARCHAR(32) DEFAULT '0.00'",
            "realized_pnl_total": "VARCHAR(32) DEFAULT '0.00'",
            "unrealized_pnl": "VARCHAR(32) DEFAULT '0.00'",
            "fees_paid_daily": "VARCHAR(32) DEFAULT '0.00'",
            "fees_paid_total": "VARCHAR(32) DEFAULT '0.00'",
            "taxes_paid_daily": "VARCHAR(32) DEFAULT '0.00'",
            "taxes_paid_total": "VARCHAR(32) DEFAULT '0.00'",
            "peak_equity": "VARCHAR(32) DEFAULT '0.00'",
            "drawdown": "VARCHAR(32) DEFAULT '0.000000'",
            "exposure": "VARCHAR(32) DEFAULT '0.000000'",
            "position_count": "INTEGER DEFAULT 0",
            "market_data_checksums_json": "TEXT DEFAULT '{}'",
            "calendar_version": "VARCHAR(64)",
            "valuation_adjust": "VARCHAR(16) DEFAULT ''",
            "stale_valuation_json": "TEXT DEFAULT '{}'",
        },
    )
    _add_columns_if_missing(
        inspector,
        "market_quote_snapshots",
        {
            "quality_reasons_json": "TEXT DEFAULT '[]'",
        },
    )
    _add_columns_if_missing(
        inspector,
        "market_data_provider_status",
        {
            "consecutive_successes": "INTEGER DEFAULT 0",
            "request_count": "INTEGER DEFAULT 0",
            "success_count": "INTEGER DEFAULT 0",
            "failure_count": "INTEGER DEFAULT 0",
            "p95_latency_ms": "FLOAT DEFAULT 0.0",
            "duplicate_quote_count": "INTEGER DEFAULT 0",
            "out_of_order_count": "INTEGER DEFAULT 0",
        },
    )
    _add_columns_if_missing(
        inspector,
        "paper_shadow_decisions",
        {
            "provider": "VARCHAR(80)",
            "market_time": "DATETIME",
            "quote_checksum": "VARCHAR(64)",
            "risk_status": "VARCHAR(32)",
            "theoretical_quantity": "INTEGER",
            "theoretical_price": "VARCHAR(32)",
            "theoretical_fees": "VARCHAR(32)",
            "blocked_reason": "TEXT DEFAULT ''",
            "account_state_checksum": "VARCHAR(64)",
        },
    )
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            if "paper_fills" in table_names:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_fill_idempotency "
                        "ON paper_fills (fill_idempotency_key) WHERE fill_idempotency_key IS NOT NULL"
                    )
                )
            if "paper_account_snapshots" in table_names:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_snapshot_id "
                        "ON paper_account_snapshots (snapshot_id) WHERE snapshot_id IS NOT NULL"
                    )
                )
            if "market_quote_snapshots" in table_names:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_quote_snapshot "
                        "ON market_quote_snapshots (provider, symbol, market_time, data_checksum)"
                    )
                )
            if "market_data_provider_status" in table_names:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_provider_instance "
                        "ON market_data_provider_status (provider, instance_id)"
                    )
                )
            if "paper_shadow_decisions" in table_names:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_shadow_order_quote "
                        "ON paper_shadow_decisions (paper_order_id, quote_id)"
                    )
                )


def _add_columns_if_missing(inspector, table_name: str, columns: dict[str, str]) -> None:
    if table_name not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as connection:
        for column, ddl_type in columns.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {_ddl_type(ddl_type)}"))


def _ddl_type(ddl_type: str) -> str:
    if engine.dialect.name == "postgresql":
        normalized = ddl_type.strip()
        upper = normalized.upper()
        if upper == "DATETIME":
            return "TIMESTAMP WITH TIME ZONE"
        if upper.startswith("DATETIME "):
            return "TIMESTAMP WITH TIME ZONE" + normalized[len("DATETIME") :]
    return ddl_type
