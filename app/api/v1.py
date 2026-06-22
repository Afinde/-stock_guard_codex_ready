from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from ..config import get_settings
from ..db import (
    BacktestDailyEquityRecord,
    BacktestFillRecord,
    BacktestOrderRecord,
    BacktestRunRecord,
    MarketDataAdmissionResultRecord,
    MarketDataProviderStatusRecord,
    MarketQuoteSnapshotRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    ScheduledTaskRunRecord,
    SessionLocal,
    SignalRecord,
    engine,
)
from ..risk import stable_id
from ..schema import schema_status, validate_schema_against_metadata


router = APIRouter(prefix="/api/v1", tags=["v1"])
TZ = ZoneInfo("Asia/Shanghai")
_dashboard_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def ok(request: Request, data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "request_id": _request_id(request), "environment": "PAPER_TRADING"}


def page(items: list[dict[str, Any]], *, page_no: int, page_size: int, total: int) -> dict[str, Any]:
    return {"items": items, "page": page_no, "page_size": page_size, "total": total}


def capabilities() -> dict[str, bool]:
    settings = get_settings()
    return {
        "server_backtest": settings.enable_server_backtest,
        "light_scan": settings.enable_light_scan,
        "paper_order_write": settings.enable_paper_order_write,
        "live_provider": settings.enable_live_provider and settings.market_live_enabled,
        "live_order": settings.enable_live_order,
        "websocket": settings.enable_websocket,
    }


@router.get("/capabilities")
def api_capabilities(request: Request) -> dict[str, Any]:
    return ok(request, capabilities())


@router.get("/dashboard/summary")
def dashboard_summary(request: Request) -> dict[str, Any]:
    settings = get_settings()
    now = time.monotonic()
    if settings.dashboard_cache_seconds and _dashboard_cache["value"] and now < _dashboard_cache["expires_at"]:
        return ok(request, _dashboard_cache["value"])
    with SessionLocal() as session:
        provider = session.scalars(select(MarketDataProviderStatusRecord).order_by(MarketDataProviderStatusRecord.updated_at.desc()).limit(1)).first()
        latest_signal = session.scalars(select(SignalRecord).order_by(SignalRecord.generated_at.desc()).limit(1)).first()
        account = session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.created_at.asc()).limit(1)).first()
        total_signals = session.scalar(select(func.count(SignalRecord.id))) or 0
        buy_watch_count = session.scalar(select(func.count(SignalRecord.id)).where(SignalRecord.signal_type.in_(["BUY_WATCH", "BUY_CONFIRM"]))) or 0
        hold_count = session.scalar(select(func.count(SignalRecord.id)).where(SignalRecord.signal_type == "HOLD")) or 0
        sell_watch_count = session.scalar(select(func.count(SignalRecord.id)).where(SignalRecord.signal_type.in_(["SELL", "REDUCE"]))) or 0
        open_order_count = session.scalar(select(func.count(PaperOrderRecord.id)).where(PaperOrderRecord.status.in_(["PAPER_PENDING", "SUBMITTED", "PARTIALLY_FILLED"]))) or 0
        position_count = session.scalar(select(func.count(PaperPositionRecord.id))) or 0
        latest_job = session.scalars(select(ScheduledTaskRunRecord).order_by(ScheduledTaskRunRecord.started_at.desc()).limit(1)).first()
        migration = validate_schema_against_metadata(engine)
        data = {
            "environment": "PAPER_TRADING",
            "deployment_profile": settings.deployment_profile,
            "app_version": settings.app_version,
            "data_mode": settings.market_data_mode,
            "provider_status": "NOT_CONFIGURED" if provider is None else provider.status,
            "latest_scan_at": _iso(latest_signal.generated_at) if latest_signal else "",
            "latest_trading_date": latest_signal.market_trade_date if latest_signal else "UNKNOWN",
            "total_signals": total_signals,
            "buy_watch_count": buy_watch_count,
            "hold_count": hold_count,
            "sell_watch_count": sell_watch_count,
            "paper_equity": account.total_equity if account else None,
            "paper_cash": account.cash_available if account else None,
            "paper_market_value": account.market_value if account else None,
            "paper_exposure": None if not account or Decimal(account.total_equity or "0") == 0 else str((Decimal(account.market_value) / Decimal(account.total_equity)).quantize(Decimal("0.000001"))),
            "paper_total_return": None if not account or Decimal(account.initial_cash or "0") == 0 else str(((Decimal(account.total_equity) - Decimal(account.initial_cash)) / Decimal(account.initial_cash)).quantize(Decimal("0.000001"))),
            "paper_max_drawdown": account.drawdown if account else None,
            "open_order_count": open_order_count,
            "position_count": position_count,
            "latest_job": None if latest_job is None else _job(latest_job),
            "migration_status": {"current_revision": migration.current_revision, "head_revision": migration.target_revision, "migration_required": migration.migration_required},
            "capabilities": capabilities(),
        }
    _dashboard_cache.update({"expires_at": now + settings.dashboard_cache_seconds, "value": data})
    return ok(request, data)


@router.get("/signals")
def signals(
    request: Request,
    page_no: int = Query(1, ge=1, alias="page"),
    page_size: int = Query(20, ge=1),
    symbol: str | None = None,
    signal_type: str | None = None,
    strategy_version: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    minimum_score: float | None = None,
    sort_by: str = "generated_at",
    sort_order: str = "desc",
) -> dict[str, Any]:
    page_size = _page_size(page_size)
    allowed_sort = {"generated_at": SignalRecord.generated_at, "score": SignalRecord.score, "symbol": SignalRecord.symbol}
    if sort_by not in allowed_sort:
        raise HTTPException(status_code=422, detail="invalid sort_by")
    order_col = allowed_sort[sort_by].asc() if sort_order.lower() == "asc" else allowed_sort[sort_by].desc()
    with SessionLocal() as session:
        query = select(SignalRecord)
        if symbol:
            query = query.where(SignalRecord.symbol == symbol.upper())
        if signal_type:
            query = query.where(SignalRecord.signal_type == signal_type)
        if strategy_version:
            query = query.where(SignalRecord.strategy_version == strategy_version)
        if date_from:
            query = query.where(SignalRecord.generated_at >= datetime.fromisoformat(date_from))
        if date_to:
            query = query.where(SignalRecord.generated_at <= datetime.fromisoformat(date_to))
        if minimum_score is not None:
            query = query.where(SignalRecord.score >= minimum_score)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(order_col, SignalRecord.id.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_signal(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/signals/{signal_id}")
def signal_detail(request: Request, signal_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(SignalRecord, signal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="signal not found")
        return ok(request, _signal(row))


@router.get("/stocks/{symbol}/overview")
def stock_overview(request: Request, symbol: str) -> dict[str, Any]:
    normalized = symbol.upper()
    with SessionLocal() as session:
        latest_signal = session.scalars(select(SignalRecord).where(SignalRecord.symbol == normalized).order_by(SignalRecord.generated_at.desc()).limit(1)).first()
        quote = session.scalars(select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.symbol == normalized).order_by(MarketQuoteSnapshotRecord.market_time.desc()).limit(1)).first()
        return ok(request, {"symbol": normalized, "latest_signal": None if latest_signal is None else _signal(latest_signal), "latest_quote": None if quote is None else _quote_bar(quote), "status": "EMPTY" if latest_signal is None and quote is None else "OK"})


@router.get("/stocks/{symbol}/bars")
def stock_bars(request: Request, symbol: str, start_date: str | None = None, end_date: str | None = None, limit: int = 250, adjust: str = "") -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    normalized = symbol.upper()
    with SessionLocal() as session:
        query = select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.symbol == normalized, MarketQuoteSnapshotRecord.quality_status == "VALID")
        if start_date:
            query = query.where(MarketQuoteSnapshotRecord.trading_date >= start_date)
        if end_date:
            query = query.where(MarketQuoteSnapshotRecord.trading_date <= end_date)
        rows = session.scalars(query.order_by(MarketQuoteSnapshotRecord.trading_date.desc(), MarketQuoteSnapshotRecord.market_time.desc()).limit(limit)).all()
    bars = [_quote_bar(row) for row in reversed(rows)]
    return ok(request, {"symbol": normalized, "adjust": adjust, "status": "EMPTY" if not bars else "OK", "items": bars})


@router.get("/stocks/{symbol}/signals")
def stock_signals(request: Request, symbol: str, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    return signals(request, page_no=page_no, page_size=page_size, symbol=symbol)


@router.get("/backtests")
def backtests(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    page_size = _page_size(page_size)
    with SessionLocal() as session:
        total = session.scalar(select(func.count(BacktestRunRecord.id))) or 0
        rows = session.scalars(select(BacktestRunRecord).order_by(BacktestRunRecord.started_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_backtest(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/backtests/{backtest_id}")
def backtest_detail(request: Request, backtest_id: str) -> dict[str, Any]:
    row = _backtest_row(backtest_id)
    return ok(request, _backtest(row))


@router.get("/backtests/{backtest_id}/equity-curve")
def backtest_equity_curve(request: Request, backtest_id: str, limit: int = 1000) -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    with SessionLocal() as session:
        rows = session.scalars(select(BacktestDailyEquityRecord).where(BacktestDailyEquityRecord.run_id == backtest_id).order_by(BacktestDailyEquityRecord.session_date.asc()).limit(limit)).all()
    return ok(request, {"backtest_id": backtest_id, "research_only": True, "items": [{"date": row.session_date, "total_equity": row.total_equity, "drawdown": row.drawdown} for row in rows]})


@router.get("/backtests/{backtest_id}/trades")
def backtest_trades(request: Request, backtest_id: str, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    page_size = _page_size(page_size)
    with SessionLocal() as session:
        order_ids = select(BacktestOrderRecord.backtest_order_id).where(BacktestOrderRecord.run_id == backtest_id)
        query = select(BacktestFillRecord).where(BacktestFillRecord.order_id.in_(order_ids))
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(BacktestFillRecord.session_date.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([{"fill_id": row.fill_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "execution_price": row.execution_price, "session_date": row.session_date} for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/paper/accounts")
def paper_accounts(request: Request) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(select(PaperAccountRecord).order_by(PaperAccountRecord.created_at.asc())).all()
    return ok(request, {"paper_trading": True, "items": [_account(row) for row in rows]})


@router.get("/paper/accounts/{account_id}")
def paper_account(request: Request, account_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.scalars(select(PaperAccountRecord).where(PaperAccountRecord.account_id == account_id)).first()
        if row is None:
            return ok(request, {"status": "EMPTY", "account_id": account_id, "paper_trading": True})
        return ok(request, _account(row))


@router.get("/paper/accounts/{account_id}/positions")
def paper_positions(request: Request, account_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(select(PaperPositionRecord).where(PaperPositionRecord.account_id == account_id)).all()
    return ok(request, {"account_id": account_id, "items": [_position(row) for row in rows]})


@router.get("/paper/accounts/{account_id}/orders")
def paper_orders(request: Request, account_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(select(PaperOrderRecord).where(PaperOrderRecord.account_id == account_id).order_by(PaperOrderRecord.created_at.desc())).all()
    return ok(request, {"account_id": account_id, "items": [_order(row) for row in rows]})


@router.get("/paper/accounts/{account_id}/fills")
def paper_fills(request: Request, account_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(select(PaperFillRecord).where(PaperFillRecord.account_id == account_id).order_by(PaperFillRecord.filled_at.desc())).all()
    return ok(request, {"account_id": account_id, "items": [_fill(row) for row in rows]})


@router.get("/paper/accounts/{account_id}/equity-curve")
def paper_equity_curve(request: Request, account_id: str, limit: int = 250) -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    with SessionLocal() as session:
        rows = session.scalars(select(PaperAccountSnapshotRecord).where(PaperAccountSnapshotRecord.account_id == account_id).order_by(PaperAccountSnapshotRecord.session_date.asc()).limit(limit)).all()
    return ok(request, {"account_id": account_id, "items": [{"date": row.trading_date or row.session_date, "total_equity": row.total_equity, "drawdown": row.drawdown, "simulated": True} for row in rows]})


@router.get("/paper/accounts/{account_id}/ledger-summary")
def paper_ledger_summary(request: Request, account_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        count = session.scalar(select(func.count(PaperLedgerEntryRecord.id)).where(PaperLedgerEntryRecord.account_id == account_id)) or 0
        latest = session.scalars(select(PaperLedgerEntryRecord).where(PaperLedgerEntryRecord.account_id == account_id).order_by(PaperLedgerEntryRecord.occurred_at.desc()).limit(1)).first()
    return ok(request, {"account_id": account_id, "entry_count": count, "latest_entry_at": "" if latest is None else _iso(latest.occurred_at), "paper_trading": True})


@router.get("/system/status")
def system_status(request: Request) -> dict[str, Any]:
    settings = get_settings()
    migration = validate_schema_against_metadata(engine)
    stat = os.statvfs(".")
    db_size = os.path.getsize(engine.url.database) if engine.url.database and os.path.exists(engine.url.database) else 0
    with SessionLocal() as session:
        provider = session.scalars(select(MarketDataProviderStatusRecord).order_by(MarketDataProviderStatusRecord.updated_at.desc()).limit(1)).first()
        admission = session.scalars(select(MarketDataAdmissionResultRecord).order_by(MarketDataAdmissionResultRecord.evaluated_at.desc()).limit(1)).first()
        last_job = session.scalars(select(ScheduledTaskRunRecord).order_by(ScheduledTaskRunRecord.started_at.desc()).limit(1)).first()
    return ok(request, {"app_version": settings.app_version, "environment": "PAPER_TRADING", "deployment_profile": settings.deployment_profile, "database_dialect": engine.dialect.name, "current_revision": migration.current_revision, "head_revision": migration.target_revision, "migration_required": migration.migration_required, "data_mode": settings.market_data_mode, "provider_status": "NOT_CONFIGURED" if provider is None else provider.status, "admission_status": "NOT_CONFIGURED" if admission is None else admission.status, "last_scan": "", "last_backtest": "", "last_settlement": "", "disk_usage": {"free_bytes": stat.f_bavail * stat.f_frsize}, "database_size": db_size, "capabilities": capabilities(), "recent_errors": [], "latest_job": None if last_job is None else _job(last_job)})


@router.get("/system/jobs")
def system_jobs(request: Request, page_no: int = Query(1, ge=1, alias="page"), page_size: int = Query(20, ge=1)) -> dict[str, Any]:
    page_size = _page_size(page_size)
    with SessionLocal() as session:
        total = session.scalar(select(func.count(ScheduledTaskRunRecord.id))) or 0
        rows = session.scalars(select(ScheduledTaskRunRecord).order_by(ScheduledTaskRunRecord.started_at.desc()).offset((page_no - 1) * page_size).limit(page_size)).all()
    return ok(request, page([_job(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.get("/system/jobs/{job_id}")
def system_job(request: Request, job_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_run_id == job_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return ok(request, _job(row))


@router.post("/system/jobs/scan")
def create_scan_job(request: Request) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_light_scan:
        raise HTTPException(status_code=403, detail="CAPABILITY_DISABLED")
    with SessionLocal() as session:
        existing = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_type == "WATCHLIST_SCAN", ScheduledTaskRunRecord.status.in_(["QUEUED", "RUNNING"]))).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="scan job already queued")
        now = datetime.now(TZ)
        job_id = stable_id("local-job", "WATCHLIST_SCAN", now.isoformat())
        row = ScheduledTaskRunRecord(task_run_id=job_id, task_key=job_id, task_type="WATCHLIST_SCAN", session_date=now.date().isoformat(), status="QUEUED", attempt=1, started_at=now)
        session.add(row)
        session.commit()
        return ok(request, _job(row))


@router.post("/backtests")
def create_backtest_disabled() -> None:
    if not get_settings().enable_server_backtest:
        raise HTTPException(status_code=403, detail="CAPABILITY_DISABLED")
    raise HTTPException(status_code=501, detail="server backtest creation is not implemented in this deployment")


def _page_size(value: int) -> int:
    max_size = min(get_settings().max_page_size, 100)
    if value > max_size:
        raise HTTPException(status_code=422, detail=f"page_size must be <= {max_size}")
    return value


def _signal(row: SignalRecord) -> dict[str, Any]:
    return {"signal_id": row.id, "symbol": row.symbol, "name": row.symbol, "signal_type": row.signal_type or row.action, "total_score": row.score, "score_breakdown": _json(row.score_breakdown, {}), "recommended_position": row.suggested_shares, "stop_loss_reference": row.stop_loss_price if row.stop_loss_price is not None else row.stop_price, "take_profit_reference": {"take_profit_1": row.take_profit_1_price if row.take_profit_1_price is not None else row.take_profit_1, "take_profit_2": row.take_profit_2_price if row.take_profit_2_price is not None else row.take_profit_2}, "reasons": _json(row.reasons, []), "invalidation_conditions": _json(row.invalidation_conditions, []), "strategy_version": row.strategy_version, "parameter_digest": row.parameter_version, "provider": row.market_data_source, "data_checksum": row.market_data_checksum, "generated_at": _iso(row.generated_at), "research_only": True}


def _quote_bar(row: MarketQuoteSnapshotRecord) -> dict[str, Any]:
    close = row.last_price
    return {"trading_date": row.trading_date, "open": row.open_price, "high": row.high_price, "low": row.low_price, "close": close, "volume": row.volume, "amount": row.amount, "ma20": None, "ma60": None, "provider": row.provider, "checksum": row.data_checksum}


def _backtest(row: BacktestRunRecord) -> dict[str, Any]:
    summary = _json(row.result_summary_json, {})
    return {"backtest_id": row.run_id, "strategy_version": row.strategy_version, "symbol": None, "universe": _json(row.config_json, {}).get("symbols", []), "start_date": _json(row.config_json, {}).get("start_date", ""), "end_date": _json(row.config_json, {}).get("end_date", ""), "initial_cash": _json(row.config_json, {}).get("initial_cash", ""), "final_equity": summary.get("final_equity"), "total_return": summary.get("total_return"), "annualized_return": summary.get("annualized_return"), "maximum_drawdown": summary.get("maximum_drawdown"), "win_rate": summary.get("win_rate"), "profit_factor": summary.get("profit_factor"), "turnover": summary.get("turnover"), "trade_count": summary.get("trade_count"), "status": row.status, "data_version": row.data_checksums_json, "created_at": _iso(row.started_at), "research_only": True}


def _backtest_row(backtest_id: str) -> BacktestRunRecord:
    with SessionLocal() as session:
        row = session.scalars(select(BacktestRunRecord).where(BacktestRunRecord.run_id == backtest_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        return row


def _account(row: PaperAccountRecord) -> dict[str, Any]:
    return {"account_id": row.account_id, "name": row.name, "status": row.status, "cash_available": row.cash_available, "cash_frozen": row.cash_frozen, "market_value": row.market_value, "total_equity": row.total_equity, "drawdown": row.drawdown, "paper_trading": True}


def _position(row: PaperPositionRecord) -> dict[str, Any]:
    return {"symbol": row.symbol, "total_quantity": row.total_quantity, "available_quantity": row.available_quantity, "average_cost": row.average_cost, "market_value": row.market_value, "unrealized_pnl": row.unrealized_pnl}


def _order(row: PaperOrderRecord) -> dict[str, Any]:
    return {"paper_order_id": row.paper_order_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "remaining_quantity": row.remaining_quantity, "status": row.status, "created_at": _iso(row.created_at), "paper_trading": True}


def _fill(row: PaperFillRecord) -> dict[str, Any]:
    return {"fill_id": row.fill_id, "paper_order_id": row.paper_order_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "execution_price": row.execution_price, "filled_at": _iso(row.filled_at), "paper_trading": True}


def _job(row: ScheduledTaskRunRecord) -> dict[str, Any]:
    return {"job_id": row.task_run_id or row.task_key, "task_type": row.task_type, "status": row.status, "attempt": row.attempt, "started_at": _iso(row.started_at), "completed_at": _iso(row.completed_at)}


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.isoformat()
