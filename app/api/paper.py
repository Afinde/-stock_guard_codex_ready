from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import (
    engine,
    MarketDataAdmissionHistoryRecord,
    MarketDataAdmissionResultRecord,
    MarketDataDegradationEventRecord,
    MarketDataProviderStatusRecord,
    MarketDataQualityDailyRecord,
    MarketDataShadowDailyReportRecord,
    MarketQuoteSnapshotRecord,
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    PaperShadowDecisionRecord,
    ProviderConnectivityTestRecord,
    ProviderShadowRunRecord,
    ProposalStatusHistoryRecord,
    ProposedOrderRecord,
    QuoteComparisonRecord,
    ScheduledTaskRunRecord,
    SessionLocal,
)
from ..paper import PaperAccountStatus, PaperOrderStatus, SystemClock
from ..paper_runtime import runtime_from_settings
from ..risk import stable_id, stable_json
from ..schema import assert_schema_ready_for_writes


router = APIRouter(
    prefix="/api/paper",
    tags=["paper trading"],
    responses={200: {"description": "PAPER_TRADING only. 模拟交易，不构成实际委托或收益保证。"}},
)

TZ = ZoneInfo("Asia/Shanghai")


class CreateAccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="Paper Account", max_length=120)
    initial_cash: Decimal = Field(gt=0)


class ProposalActionRequest(BaseModel):
    operator: str = Field(default="api", max_length=80)
    reason: str = Field(default="", max_length=500)
    account_id: str | None = Field(default=None, max_length=80)


def envelope(data: Any, **extra) -> dict:
    return {
        "environment": "PAPER_TRADING",
        "notice": "模拟交易，不构成实际委托或收益保证",
        **extra,
        "data": data,
    }


@router.post("/accounts")
def create_account(payload: CreateAccountRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    _require_schema_ready()
    now = _now()
    with SessionLocal() as session:
        existing = session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.account_id == payload.account_id)).first()
        if existing is not None:
            return envelope(_account(existing))
        account = PaperAccountRecord(
            account_id=payload.account_id,
            name=payload.name,
            status=PaperAccountStatus.ACTIVE.value,
            base_currency="CNY",
            initial_cash=_money(payload.initial_cash),
            cash_available=_money(payload.initial_cash),
            cash_frozen="0.00",
            market_value="0.00",
            total_equity=_money(payload.initial_cash),
            realized_pnl="0.00",
            unrealized_pnl="0.00",
            created_at=now,
            updated_at=now,
        )
        session.add(account)
        session.add(
            PaperLedgerEntryRecord(
                entry_id=stable_id("paper-ledger", payload.account_id, "INITIAL_DEPOSIT", idempotency_key or "create"),
                account_id=payload.account_id,
                event_type="INITIAL_DEPOSIT",
                amount=_money(payload.initial_cash),
                cash_available_after=_money(payload.initial_cash),
                cash_frozen_after="0.00",
                ref_id=idempotency_key or payload.account_id,
                payload_json="{}",
                occurred_at=now,
            )
        )
        session.commit()
        return envelope(_account(account))


@router.get("/accounts")
def accounts() -> dict:
    with SessionLocal() as session:
        rows = session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.created_at.desc())).all()
        return envelope([_account(row) for row in rows])


@router.get("/accounts/{account_id}")
def account(account_id: str) -> dict:
    with SessionLocal() as session:
        return envelope(_account(_account_row(session, account_id)))


@router.post("/accounts/{account_id}/pause")
def pause_account(account_id: str) -> dict:
    _require_schema_ready()
    with SessionLocal() as session:
        row = _account_row(session, account_id)
        row.status = PaperAccountStatus.PAUSED.value
        row.updated_at = _now()
        session.commit()
        return envelope(_account(row))


@router.post("/accounts/{account_id}/resume")
def resume_account(account_id: str) -> dict:
    _require_schema_ready()
    with SessionLocal() as session:
        row = _account_row(session, account_id)
        if row.status == PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value:
            raise HTTPException(status_code=409, detail="recovery-required account cannot be resumed by API")
        row.status = PaperAccountStatus.ACTIVE.value
        row.updated_at = _now()
        session.commit()
        return envelope(_account(row))


@router.get("/accounts/{account_id}/positions")
def positions(account_id: str) -> dict:
    with SessionLocal() as session:
        _account_row(session, account_id)
        rows = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account_id)).all()
        return envelope([_position(row) for row in rows])


@router.get("/accounts/{account_id}/orders")
def orders(account_id: str) -> dict:
    with SessionLocal() as session:
        _account_row(session, account_id)
        rows = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.account_id == account_id)).all()
        return envelope([_order(row) for row in rows])


@router.get("/accounts/{account_id}/fills")
def fills(account_id: str) -> dict:
    with SessionLocal() as session:
        _account_row(session, account_id)
        rows = session.scalars(select(PaperFillRecord).where(PaperFillRecord.account_id == account_id)).all()
        return envelope([_fill(row) for row in rows])


@router.get("/accounts/{account_id}/ledger")
def ledger(account_id: str) -> dict:
    with SessionLocal() as session:
        _account_row(session, account_id)
        rows = session.scalars(select(PaperLedgerEntryRecord).where(PaperLedgerEntryRecord.account_id == account_id)).all()
        return envelope([_ledger(row) for row in rows])


@router.get("/accounts/{account_id}/snapshots")
def snapshots(account_id: str) -> dict:
    with SessionLocal() as session:
        _account_row(session, account_id)
        rows = session.scalars(select(PaperAccountSnapshotRecord).where(PaperAccountSnapshotRecord.account_id == account_id)).all()
        return envelope([_snapshot(row) for row in rows])


@router.get("/proposals")
def proposals() -> dict:
    with SessionLocal() as session:
        rows = session.scalars(select(ProposedOrderRecord).order_by(ProposedOrderRecord.created_at.desc())).all()
        return envelope([_proposal(row) for row in rows])


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: str) -> dict:
    with SessionLocal() as session:
        return envelope(_proposal(_proposal_row(session, proposal_id)))


@router.post("/proposals/{proposal_id}/review")
def review_proposal(proposal_id: str, payload: ProposalActionRequest) -> dict:
    _require_schema_ready()
    return _transition_proposal(proposal_id, "REVIEWED", payload)


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str, payload: ProposalActionRequest) -> dict:
    _require_schema_ready()
    return _transition_proposal(proposal_id, "REJECTED", payload)


@router.post("/proposals/{proposal_id}/cancel")
def cancel_proposal(proposal_id: str, payload: ProposalActionRequest) -> dict:
    _require_schema_ready()
    return _transition_proposal(proposal_id, "CANCELLED", payload)


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: str,
    payload: ProposalActionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    _require_schema_ready()
    key = idempotency_key or stable_id("paper-accept", proposal_id, payload.account_id or "")
    now = _now()
    with SessionLocal() as session:
        proposal = _proposal_row(session, proposal_id)
        account_id = payload.account_id or _first_account_id(session)
        account = _account_row(session, account_id)
        existing = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.idempotency_key == key)).first()
        if existing is not None:
            return envelope(_order(existing))
        if proposal.status not in {"PROPOSED", "REVIEWED"}:
            raise HTTPException(status_code=409, detail=f"proposal status cannot be accepted: {proposal.status}")
        if _aware(proposal.expires_at) < now:
            proposal.status = "EXPIRED"
            _status_history(session, proposal.proposal_id, "PROPOSED", "EXPIRED", payload.operator, "expired", now)
            session.commit()
            raise HTTPException(status_code=409, detail="expired proposal cannot be accepted")
        if account.status in {PaperAccountStatus.RISK_OFF.value, PaperAccountStatus.PAUSED.value, PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value, PaperAccountStatus.CLOSED.value}:
            raise HTTPException(status_code=409, detail=f"account status blocks new buy: {account.status}")
        quantity = int(proposal.quantity)
        if quantity < 100:
            raise HTTPException(status_code=409, detail="approved quantity below one lot")
        freeze = Decimal(proposal.reference_price) * Decimal(quantity) * Decimal("1.03")
        if Decimal(account.cash_available) < freeze:
            raise HTTPException(status_code=409, detail="insufficient paper cash")
        account.cash_available = _money(Decimal(account.cash_available) - freeze)
        account.cash_frozen = _money(Decimal(account.cash_frozen) + freeze)
        account.total_equity = _money(Decimal(account.cash_available) + Decimal(account.cash_frozen) + Decimal(account.market_value))
        account.updated_at = now
        old_status = proposal.status
        proposal.status = "ACCEPTED"
        _status_history(session, proposal.proposal_id, old_status, "ACCEPTED", payload.operator, payload.reason or "manual accept", now)
        order = PaperOrderRecord(
            paper_order_id=stable_id("paper-order", account_id, proposal.proposal_id, key),
            account_id=account_id,
            proposal_id=proposal.proposal_id,
            active_key=proposal.proposal_id,
            idempotency_key=key,
            symbol=proposal.symbol,
            side=proposal.side,
            order_type="MARKET_ON_NEXT_OPEN",
            quantity=quantity,
            remaining_quantity=quantity,
            limit_price=None,
            status=PaperOrderStatus.PAPER_PENDING.value,
            rejection_reason="",
            source_signal_identity=proposal.signal_identity,
            risk_decision_id=proposal.risk_decision_id,
            created_at=now,
            expires_at=now + timedelta(hours=24),
            updated_at=now,
        )
        session.add(order)
        session.add(
            PaperLedgerEntryRecord(
                entry_id=stable_id("paper-ledger", account_id, "CASH_FROZEN", proposal.proposal_id, key),
                account_id=account_id,
                event_type="CASH_FROZEN",
                amount=_money(freeze),
                cash_available_after=account.cash_available,
                cash_frozen_after=account.cash_frozen,
                ref_id=order.paper_order_id,
                payload_json=stable_json({"environment": "PAPER_TRADING"}),
                occurred_at=now,
            )
        )
        _outbox(session, account_id, "PROPOSAL_ACCEPTED", {"proposal_id": proposal.proposal_id, "order_id": order.paper_order_id}, now)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.idempotency_key == key)).first()
            if existing is None:
                raise
            return envelope(_order(existing))
        return envelope(_order(order))


@router.get("/orders/{order_id}")
def order(order_id: str) -> dict:
    with SessionLocal() as session:
        return envelope(_order(_order_row(session, order_id)))


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict:
    _require_schema_ready()
    now = _now()
    with SessionLocal() as session:
        order = _order_row(session, order_id)
        if order.status not in {"PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"}:
            raise HTTPException(status_code=409, detail=f"order cannot be cancelled: {order.status}")
        account = _account_row(session, order.account_id)
        if order.side == "BUY" and Decimal(account.cash_frozen) > 0:
            release = Decimal(account.cash_frozen)
            account.cash_available = _money(Decimal(account.cash_available) + release)
            account.cash_frozen = "0.00"
            session.add(
                PaperLedgerEntryRecord(
                    entry_id=stable_id("paper-ledger", order.account_id, "CASH_RELEASED", order.paper_order_id),
                    account_id=order.account_id,
                    event_type="CASH_RELEASED",
                    amount=_money(release),
                    cash_available_after=account.cash_available,
                    cash_frozen_after=account.cash_frozen,
                    ref_id=order.paper_order_id,
                    payload_json="{}",
                    occurred_at=now,
                )
            )
        if order.side == "SELL":
            position = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == order.account_id, PaperPositionRecord.symbol == order.symbol)).first()
            if position is not None and position.locked_quantity > 0:
                release_qty = min(position.locked_quantity, order.remaining_quantity)
                position.locked_quantity -= release_qty
                position.available_quantity += release_qty
        order.status = PaperOrderStatus.CANCELLED.value
        order.updated_at = now
        session.commit()
        return envelope(_order(order))


@router.get("/runtime/status")
def runtime_status() -> dict:
    try:
        return runtime_from_settings(get_settings()).status()
    except Exception as exc:
        return {
            "environment": "PAPER_TRADING",
            "healthy": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }


@router.get("/runtime/tasks")
def runtime_tasks() -> dict:
    with SessionLocal() as session:
        rows = session.scalars(select(ScheduledTaskRunRecord).order_by(ScheduledTaskRunRecord.started_at.desc())).all()
        return envelope([_task(row) for row in rows])


@router.get("/market-data/status")
def market_data_status() -> dict:
    settings = get_settings()
    with SessionLocal() as session:
        providers = session.scalars(select(MarketDataProviderStatusRecord).order_by(MarketDataProviderStatusRecord.updated_at.desc())).all()
        return envelope(
            {
                "data_mode": settings.market_data_mode,
                "mode": "SHADOW" if settings.market_live_shadow_mode else "DISABLED",
                "live_enabled": settings.market_live_enabled,
                "providers": [_provider_status(row) for row in providers],
            },
            mode="SHADOW",
        )


@router.get("/market-data/providers")
def market_data_providers(limit: int = 50) -> dict:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with SessionLocal() as session:
        rows = session.scalars(select(MarketDataProviderStatusRecord).order_by(MarketDataProviderStatusRecord.updated_at.desc()).limit(limit)).all()
        return envelope([_provider_status(row) for row in rows], mode="SHADOW")


@router.get("/market-data/quotes")
def market_data_quotes(symbol: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(MarketQuoteSnapshotRecord).order_by(MarketQuoteSnapshotRecord.market_time.desc()).limit(limit)
        if symbol:
            query = query.where(MarketQuoteSnapshotRecord.symbol == symbol.upper())
        rows = session.scalars(query).all()
        return envelope([_quote(row) for row in rows], mode="SHADOW")


@router.get("/market-data/quality")
def market_data_quality(limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        rows = session.scalars(select(MarketDataQualityDailyRecord).order_by(MarketDataQualityDailyRecord.updated_at.desc()).limit(limit)).all()
        return envelope([_quality(row) for row in rows], mode="SHADOW")


@router.get("/market-data/shadow-decisions")
def market_data_shadow_decisions(account_id: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(PaperShadowDecisionRecord).order_by(PaperShadowDecisionRecord.created_at.desc()).limit(limit)
        if account_id:
            query = query.where(PaperShadowDecisionRecord.account_id == account_id)
        rows = session.scalars(query).all()
        return envelope([_shadow_decision(row) for row in rows], mode="SHADOW")


@router.get("/market-data/comparisons")
def market_data_comparisons(limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        rows = session.scalars(select(QuoteComparisonRecord).order_by(QuoteComparisonRecord.created_at.desc()).limit(limit)).all()
        return envelope([_comparison(row) for row in rows], mode="SHADOW")


@router.get("/market-data/connectivity")
def market_data_connectivity(limit: int = 50) -> dict:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with SessionLocal() as session:
        rows = session.scalars(select(ProviderConnectivityTestRecord).order_by(ProviderConnectivityTestRecord.started_at.desc()).limit(limit)).all()
        return envelope([_connectivity_test(row) for row in rows], mode="SHADOW")


@router.get("/market-data/shadow-runs")
def market_data_shadow_runs(provider: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(ProviderShadowRunRecord).order_by(ProviderShadowRunRecord.started_at.desc()).limit(limit)
        if provider:
            query = query.where(ProviderShadowRunRecord.provider == provider)
        rows = session.scalars(query).all()
        return envelope([_shadow_run(row) for row in rows], mode="SHADOW")


@router.get("/market-data/shadow-runs/{run_id}")
def market_data_shadow_run(run_id: str) -> dict:
    with SessionLocal() as session:
        row = session.scalars(select(ProviderShadowRunRecord).where(ProviderShadowRunRecord.run_id == run_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="provider shadow run not found")
        return envelope(_shadow_run(row), mode="SHADOW")


@router.get("/market-data/admission")
def market_data_admission(provider: str | None = None, limit: int = 50) -> dict:
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with SessionLocal() as session:
        query = select(MarketDataAdmissionResultRecord).order_by(MarketDataAdmissionResultRecord.evaluated_at.desc()).limit(limit)
        if provider:
            query = query.where(MarketDataAdmissionResultRecord.provider == provider)
        rows = session.scalars(query).all()
        return envelope([_admission_result(row) for row in rows], mode="SHADOW")


@router.get("/market-data/admission/history")
def market_data_admission_history(provider: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(MarketDataAdmissionHistoryRecord).order_by(MarketDataAdmissionHistoryRecord.changed_at.desc()).limit(limit)
        if provider:
            query = query.where(MarketDataAdmissionHistoryRecord.provider == provider)
        rows = session.scalars(query).all()
        return envelope([_admission_history(row) for row in rows], mode="SHADOW")


@router.get("/market-data/degradation-events")
def market_data_degradation_events(provider: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(MarketDataDegradationEventRecord).order_by(MarketDataDegradationEventRecord.created_at.desc()).limit(limit)
        if provider:
            query = query.where(MarketDataDegradationEventRecord.provider == provider)
        rows = session.scalars(query).all()
        return envelope([_degradation_event(row) for row in rows], mode="SHADOW")


@router.get("/market-data/daily-reports")
def market_data_daily_reports(provider: str | None = None, limit: int = 100) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    with SessionLocal() as session:
        query = select(MarketDataShadowDailyReportRecord).order_by(MarketDataShadowDailyReportRecord.created_at.desc()).limit(limit)
        if provider:
            query = query.where(MarketDataShadowDailyReportRecord.provider == provider)
        rows = session.scalars(query).all()
        return envelope([_daily_report(row) for row in rows], mode="SHADOW")


@router.get("/runtime/tasks/{task_run_id}")
def runtime_task(task_run_id: str) -> dict:
    with SessionLocal() as session:
        row = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_run_id == task_run_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="paper runtime task not found")
        return envelope(_task(row))


@router.post("/runtime/tasks/run")
def run_runtime_task(task_type: str, trading_date: str, account_id: str | None = None) -> dict:
    _require_schema_ready()
    settings = get_settings()
    if not getattr(settings, "paper_allow_manual_task_trigger", False):
        raise HTTPException(status_code=403, detail="manual paper runtime task trigger disabled")
    result = runtime_from_settings(settings).run_task(task_type=task_type, trading_date=datetime.fromisoformat(trading_date).date(), account_id=account_id)
    return envelope(_task(result))


def _transition_proposal(proposal_id: str, to_status: str, payload: ProposalActionRequest) -> dict:
    now = _now()
    with SessionLocal() as session:
        row = _proposal_row(session, proposal_id)
        allowed = {
            "PROPOSED": {"REVIEWED", "REJECTED", "CANCELLED"},
            "REVIEWED": {"REJECTED", "CANCELLED"},
            "ACCEPTED": set(),
            "REJECTED": set(),
            "EXPIRED": set(),
            "CANCELLED": set(),
        }
        if to_status not in allowed.get(row.status, set()):
            raise HTTPException(status_code=409, detail=f"invalid proposal transition: {row.status}->{to_status}")
        old = row.status
        row.status = to_status
        _status_history(session, row.proposal_id, old, to_status, payload.operator, payload.reason, now)
        session.commit()
        return envelope(_proposal(row))


def _account_row(session, account_id: str) -> PaperAccountRecord:
    row = session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.account_id == account_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    return row


def _proposal_row(session, proposal_id: str) -> ProposedOrderRecord:
    row = session.scalars(select(ProposedOrderRecord).where(ProposedOrderRecord.proposal_id == proposal_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="paper proposal not found")
    return row


def _order_row(session, order_id: str) -> PaperOrderRecord:
    row = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.paper_order_id == order_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="paper order not found")
    return row


def _first_account_id(session) -> str:
    row = session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.created_at.asc()).limit(1)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    return row.account_id


def _status_history(session, proposal_id: str, old: str, new: str, operator: str, reason: str, now: datetime) -> None:
    session.add(ProposalStatusHistoryRecord(proposal_id=proposal_id, from_status=old, to_status=new, operator=operator, reason=reason, changed_at=now))


def _outbox(session, account_id: str, notification_type: str, payload: dict, now: datetime) -> None:
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


def _account(row: PaperAccountRecord) -> dict:
    return {
        "account_id": row.account_id,
        "name": row.name,
        "status": row.status,
        "base_currency": row.base_currency,
        "cash_available": row.cash_available,
        "cash_frozen": row.cash_frozen,
        "market_value": row.market_value,
        "total_equity": row.total_equity,
        "paper_trading": True,
    }


def _position(row: PaperPositionRecord) -> dict:
    return {
        "symbol": row.symbol,
        "total_quantity": row.total_quantity,
        "available_quantity": row.available_quantity,
        "today_bought_quantity": row.today_bought_quantity,
        "locked_quantity": row.locked_quantity,
        "average_cost": row.average_cost,
        "market_value": row.market_value,
    }


def _order(row: PaperOrderRecord) -> dict:
    return {
        "paper_order_id": row.paper_order_id,
        "account_id": row.account_id,
        "proposal_id": row.proposal_id,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "quantity": row.quantity,
        "remaining_quantity": row.remaining_quantity,
        "status": row.status,
        "rejection_reason": row.rejection_reason,
        "paper_trading": True,
    }


def _fill(row: PaperFillRecord) -> dict:
    return {"fill_id": row.fill_id, "paper_order_id": row.paper_order_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "execution_price": row.execution_price}


def _ledger(row: PaperLedgerEntryRecord) -> dict:
    return {"entry_id": row.entry_id, "event_type": row.event_type, "amount": row.amount, "symbol": row.symbol, "quantity": row.quantity, "payload": json.loads(row.payload_json or "{}")}


def _snapshot(row: PaperAccountSnapshotRecord) -> dict:
    return {"account_id": row.account_id, "session_date": row.session_date, "total_equity": row.total_equity, "positions": json.loads(row.positions_json or "[]")}


def _proposal(row: ProposedOrderRecord) -> dict:
    return {"proposal_id": row.proposal_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "status": row.status, "expires_at": _iso(row.expires_at)}


def _task(row: ScheduledTaskRunRecord) -> dict:
    return {"task_run_id": row.task_run_id, "task_key": row.task_key, "task_type": row.task_type, "account_id": row.account_id, "trading_date": row.trading_date or row.session_date, "status": row.status, "attempt": row.attempt}


def _provider_status(row: MarketDataProviderStatusRecord) -> dict:
    return {
        "provider": row.provider,
        "instance_id": row.instance_id,
        "status": row.status,
        "last_request_at": _iso(row.last_request_at),
        "last_success_at": _iso(row.last_success_at),
        "last_quote_market_time": _iso(row.last_quote_market_time),
        "consecutive_failures": row.consecutive_failures,
        "consecutive_successes": row.consecutive_successes,
        "request_count": row.request_count,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
        "stale_quote_count": row.stale_symbol_count,
        "invalid_quote_count": row.invalid_quote_count,
        "duplicate_quote_count": row.duplicate_quote_count,
        "out_of_order_count": row.out_of_order_count,
        "average_latency_ms": row.average_latency_ms,
        "p95_latency_ms": row.p95_latency_ms,
        "last_error_type": row.last_error_type,
        "updated_at": _iso(row.updated_at),
    }


def _quote(row: MarketQuoteSnapshotRecord) -> dict:
    return {
        "quote_id": row.quote_id,
        "provider": row.provider,
        "provider_version": row.provider_version,
        "symbol": row.symbol,
        "exchange": row.exchange,
        "trading_date": row.trading_date,
        "market_time": _iso(row.market_time),
        "received_at": _iso(row.received_at),
        "validated_at": _iso(row.validated_at),
        "last_price": row.last_price,
        "previous_close": row.previous_close,
        "volume": row.volume,
        "suspension_status": row.suspension_status,
        "price_limit_up": row.price_limit_up,
        "price_limit_down": row.price_limit_down,
        "data_checksum": row.data_checksum,
        "calendar_version": row.calendar_version,
        "quality_status": row.quality_status,
        "quality_reasons": json.loads(row.quality_reasons_json or "[]"),
    }


def _quality(row: MarketDataQualityDailyRecord) -> dict:
    return {
        "trading_date": row.trading_date,
        "provider": row.provider,
        "symbol": row.symbol,
        "quote_received_count": row.quote_received_count,
        "valid_quote_count": row.valid_quote_count,
        "stale_quote_count": row.stale_quote_count,
        "invalid_quote_count": row.invalid_quote_count,
        "duplicate_rate": row.duplicate_rate,
        "out_of_order_rate": row.out_of_order_rate,
        "missing_symbol_rate": row.missing_symbol_rate,
        "provider_availability": row.provider_availability,
        "schema_error_count": row.schema_error_count,
        "price_conflict_count": row.price_conflict_count,
        "suspension_unknown_count": row.suspension_unknown_count,
        "limit_rule_unknown_count": row.limit_rule_unknown_count,
    }


def _shadow_decision(row: PaperShadowDecisionRecord) -> dict:
    return {
        "shadow_decision_id": row.decision_id,
        "account_id": row.account_id,
        "order_id": row.paper_order_id,
        "symbol": row.symbol,
        "quote_id": row.quote_id,
        "market_event_id": row.market_event_id,
        "provider": row.provider,
        "market_time": _iso(row.market_time),
        "quote_checksum": row.quote_checksum,
        "risk_status": row.risk_status,
        "theoretical_quantity": row.theoretical_quantity,
        "theoretical_price": row.theoretical_price,
        "theoretical_fees": row.theoretical_fees,
        "theoretical_outcome": row.theoretical_outcome,
        "blocked_reason": row.blocked_reason,
        "account_state_checksum": row.account_state_checksum,
        "created_at": _iso(row.created_at),
    }


def _comparison(row: QuoteComparisonRecord) -> dict:
    return {
        "comparison_id": row.comparison_id,
        "trading_date": row.trading_date,
        "symbol": row.symbol,
        "live_provider": row.live_provider,
        "reference_provider": row.reference_provider,
        "price_diff_bps": row.price_diff_bps,
        "latency_ms": row.latency_ms,
        "quality_status": row.quality_status,
        "created_at": _iso(row.created_at),
    }


def _connectivity_test(row: ProviderConnectivityTestRecord) -> dict:
    return {
        "test_id": row.test_id,
        "provider": row.provider,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "status": row.status,
        "error_type": row.error_type,
        "message": row.message,
        "symbol_count": row.symbol_count,
        "quote_received_count": row.quote_received_count,
        "payload": _safe_json(row.payload_json),
    }


def _shadow_run(row: ProviderShadowRunRecord) -> dict:
    return {
        "run_id": row.run_id,
        "provider": row.provider,
        "provider_version": row.provider_version,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "trading_date": row.trading_date,
        "symbol_universe_version": row.symbol_universe_version,
        "configured_symbol_count": row.configured_symbol_count,
        "status": row.status,
        "quote_received_count": row.quote_received_count,
        "valid_quote_count": row.valid_quote_count,
        "invalid_quote_count": row.invalid_quote_count,
        "stale_quote_count": row.stale_quote_count,
        "duplicate_quote_count": row.duplicate_quote_count,
        "out_of_order_count": row.out_of_order_count,
        "schema_error_count": row.schema_error_count,
        "network_error_count": row.network_error_count,
        "rate_limit_count": row.rate_limit_count,
        "availability": row.availability,
        "average_latency_ms": row.average_latency_ms,
        "p95_latency_ms": row.p95_latency_ms,
        "missing_symbol_rate": row.missing_symbol_rate,
        "account_state_unchanged": row.account_state_before_checksum == row.account_state_after_checksum,
        "fills_created": max(0, row.fills_after_count - row.fills_before_count),
        "result": row.result,
        "failure_reasons": _safe_json(row.failure_reasons_json, []),
    }


def _admission_result(row: MarketDataAdmissionResultRecord) -> dict:
    return {
        "provider": row.provider,
        "evaluated_at": _iso(row.evaluated_at),
        "status": row.status,
        "complete_trading_days": row.complete_trading_days,
        "failure_reasons": _safe_json(row.failure_reasons_json, []),
        "metrics": _safe_json(row.metrics_json),
        "policy_snapshot": _safe_json(row.policy_snapshot_json),
    }


def _admission_history(row: MarketDataAdmissionHistoryRecord) -> dict:
    return {
        "provider": row.provider,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "reason": row.reason,
        "changed_at": _iso(row.changed_at),
    }


def _degradation_event(row: MarketDataDegradationEventRecord) -> dict:
    return {
        "event_id": row.event_id,
        "provider": row.provider,
        "event_type": row.event_type,
        "severity": row.severity,
        "reason": row.reason,
        "mode_from": row.mode_from,
        "mode_to": row.mode_to,
        "requires_manual_review": row.requires_manual_review,
        "created_at": _iso(row.created_at),
        "payload": _safe_json(row.payload_json),
    }


def _daily_report(row: MarketDataShadowDailyReportRecord) -> dict:
    return {
        "provider": row.provider,
        "trading_date": row.trading_date,
        "status": row.status,
        "report": _safe_json(row.report_json),
        "created_at": _iso(row.created_at),
    }


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _now() -> datetime:
    return SystemClock("Asia/Shanghai").now()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def _iso(value: datetime | None) -> str:
    return "" if value is None else _aware(value).isoformat()


def _safe_json(value: str | None, default: Any | None = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {} if default is None else default


def _require_schema_ready() -> None:
    settings = get_settings()
    if settings.deployment_profile == "ECS_LITE" and not settings.enable_paper_order_write:
        raise HTTPException(status_code=403, detail="CAPABILITY_DISABLED")
    try:
        assert_schema_ready_for_writes(engine)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
