from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from ..auth import CurrentUser, require_admin, require_user
from ..db import (
    DataIngestionRunRecord,
    DailyBarRecord,
    IndustrySnapshotRecord,
    MarketQuoteSnapshotRecord,
    ProviderHealthStatusRecord,
    ScheduledTaskRunRecord,
    SessionLocal,
    StockNewsRecord,
)
from ..risk import stable_id
from .v1 import _job, ok, page

router = APIRouter(prefix="/api/v1", tags=["market"], dependencies=[Depends(require_user)])


@router.get("/market/quotes")
def market_quotes(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1), symbol: str | None = None) -> dict[str, Any]:
    page_size = min(max(page_size, 1), 100)
    with SessionLocal() as session:
        query = select(MarketQuoteSnapshotRecord).order_by(MarketQuoteSnapshotRecord.market_time.desc(), MarketQuoteSnapshotRecord.symbol.asc())
        if symbol:
            query = query.where(MarketQuoteSnapshotRecord.symbol == symbol.upper())
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_quote(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/market/quotes/{symbol}")
def market_quote(request: Request, symbol: str) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.scalars(select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.symbol == symbol.upper()).order_by(MarketQuoteSnapshotRecord.market_time.desc()).limit(1)).first()
    return ok(request, {"status": "EMPTY" if row is None else "OK", "quote": None if row is None else _quote(row)})


@router.get("/market/bars/{symbol}")
def market_bars(request: Request, symbol: str, limit: int = 250) -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    with SessionLocal() as session:
        rows = session.scalars(select(DailyBarRecord).where(DailyBarRecord.symbol == symbol.upper()).order_by(DailyBarRecord.trading_date.desc()).limit(limit)).all()
    return ok(request, {"symbol": symbol.upper(), "status": "EMPTY" if not rows else "OK", "items": [_bar(row) for row in reversed(rows)]})


@router.get("/market/news")
def market_news(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1), symbol: str | None = None) -> dict[str, Any]:
    page_size = min(max(page_size, 1), 100)
    with SessionLocal() as session:
        query = select(StockNewsRecord).order_by(StockNewsRecord.published_at.desc().nullslast(), StockNewsRecord.received_at.desc())
        if symbol:
            query = query.where(StockNewsRecord.symbol == symbol.upper())
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_news(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/market/industries")
def market_industries(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    page_size = min(max(page_size, 1), 100)
    with SessionLocal() as session:
        query = select(IndustrySnapshotRecord).order_by(IndustrySnapshotRecord.market_time.desc(), IndustrySnapshotRecord.industry_name.asc())
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_industry(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/market/provider-status")
def market_provider_status(request: Request) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(select(ProviderHealthStatusRecord).order_by(ProviderHealthStatusRecord.updated_at.desc())).all()
    return ok(request, {"status": "NOT_CONFIGURED" if not rows else "OK", "items": [_provider(row) for row in rows]})


@router.get("/system/ingestion-runs")
def ingestion_runs(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    page_size = min(max(page_size, 1), 100)
    with SessionLocal() as session:
        total = session.scalar(select(func.count(DataIngestionRunRecord.id))) or 0
        rows = session.scalars(select(DataIngestionRunRecord).order_by(DataIngestionRunRecord.started_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_run(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.post("/admin/data-jobs/{job_type}/run")
def admin_run_data_job(job_type: str, request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    normalized = job_type.replace("-", "_").upper()
    allowed = {"MARKET_SPOT_SYNC", "DAILY_BAR_SYNC", "STOCK_NEWS_SYNC", "INDUSTRY_SYNC", "FINANCIAL_SYNC", "INSTRUMENT_SYNC"}
    if normalized not in allowed:
        raise HTTPException(status_code=422, detail="invalid job type")
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        existing = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_type == normalized, ScheduledTaskRunRecord.status.in_(["QUEUED", "RUNNING"]))).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="job already queued")
        job_id = stable_id("data-job", normalized, now.isoformat())
        row = ScheduledTaskRunRecord(task_run_id=job_id, task_key=job_id, task_type=normalized, session_date=now.date().isoformat(), status="QUEUED", attempt=1, started_at=now)
        session.add(row)
        session.commit()
        session.refresh(row)
        return ok(request, _job(row))


def _quote(row: MarketQuoteSnapshotRecord) -> dict[str, Any]:
    return {"provider": row.provider, "symbol": row.symbol, "market_time": _iso(row.market_time), "received_at": _iso(row.received_at), "quality_status": row.quality_status, "open": row.open_price, "high": row.high_price, "low": row.low_price, "close": row.last_price, "volume": row.volume, "amount": row.amount, "checksum": row.data_checksum}


def _bar(row: DailyBarRecord) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "symbol": row.symbol,
        "trading_date": row.trading_date,
        "adjust": row.adjust,
        "open": None if row.open_price is None else str(row.open_price),
        "high": None if row.high_price is None else str(row.high_price),
        "low": None if row.low_price is None else str(row.low_price),
        "close": None if row.close_price is None else str(row.close_price),
        "volume": row.volume,
        "amount": None if row.amount is None else str(row.amount),
        "checksum": row.data_checksum,
    }


def _news(row: StockNewsRecord) -> dict[str, Any]:
    return {"provider": row.provider, "symbol": row.symbol, "title": row.title, "summary": row.summary, "published_at": _iso(row.published_at), "received_at": _iso(row.received_at), "checksum": row.checksum}


def _industry(row: IndustrySnapshotRecord) -> dict[str, Any]:
    return {"provider": row.provider, "industry_name": row.industry_name, "market_time": _iso(row.market_time), "received_at": _iso(row.received_at), "quality_status": row.quality_status, "change_pct": None if row.change_pct is None else str(row.change_pct), "turnover": None if row.turnover is None else str(row.turnover), "leading_stock": row.leading_stock, "checksum": row.checksum}


def _provider(row: ProviderHealthStatusRecord) -> dict[str, Any]:
    return {"provider": row.provider, "status": row.status, "last_success_at": _iso(row.last_success_at), "last_failure_at": _iso(row.last_failure_at), "updated_at": _iso(row.updated_at)}


def _run(row: DataIngestionRunRecord) -> dict[str, Any]:
    return {"run_id": row.run_id, "job_type": row.job_type, "provider": row.provider, "status": row.status, "started_at": _iso(row.started_at), "completed_at": _iso(row.completed_at), "success_count": row.success_count, "duplicate_count": row.duplicate_count, "invalid_count": row.invalid_count, "error_count": row.error_count}


def _iso(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()
