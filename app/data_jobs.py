from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .db import (
    DataIngestionRunRecord,
    IndustrySnapshotRecord,
    MarketQuoteSnapshotRecord,
    ProviderHealthStatusRecord,
    SessionLocal,
    StockNewsRecord,
)
from .public_market_data import PublicMarketProvider, provider_by_name
from .schema import assert_schema_ready_for_writes
from .db import engine

JOB_TYPES = {"MARKET_SPOT_SYNC", "DAILY_BAR_SYNC", "STOCK_NEWS_SYNC", "INDUSTRY_SYNC", "FINANCIAL_SYNC", "INSTRUMENT_SYNC"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run public market data ingestion jobs.")
    parser.add_argument("--provider", default="fixture")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["market-spot", "daily-bars", "stock-news", "industry", "financial", "instruments"]:
        p = sub.add_parser(command)
        p.add_argument("--once", action="store_true", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "status":
        return print_status()
    provider = provider_by_name(args.provider)
    mapping: dict[str, Callable[[PublicMarketProvider], int]] = {
        "market-spot": run_market_spot,
        "daily-bars": run_noop("DAILY_BAR_SYNC"),
        "stock-news": run_stock_news,
        "industry": run_industry,
        "financial": run_noop("FINANCIAL_SYNC"),
        "instruments": run_noop("INSTRUMENT_SYNC"),
    }
    return mapping[args.command](provider)


def run_market_spot(provider: PublicMarketProvider) -> int:
    return _run("MARKET_SPOT_SYNC", provider.name, lambda run: _save_spot(provider, run))


def run_stock_news(provider: PublicMarketProvider) -> int:
    symbols = get_settings().watchlist[:20]
    return _run("STOCK_NEWS_SYNC", provider.name, lambda run: _save_news(provider, symbols, run))


def run_industry(provider: PublicMarketProvider) -> int:
    return _run("INDUSTRY_SYNC", provider.name, lambda run: _save_industries(provider, run))


def run_noop(job_type: str) -> Callable[[PublicMarketProvider], int]:
    def runner(provider: PublicMarketProvider) -> int:
        return _run(job_type, provider.name, lambda run: None)

    return runner


def print_status() -> int:
    with SessionLocal() as session:
        rows = session.scalars(select(DataIngestionRunRecord).order_by(DataIngestionRunRecord.started_at.desc()).limit(20)).all()
    for row in rows:
        print(f"{row.started_at.isoformat()} {row.job_type} {row.provider} {row.status} success={row.success_count} duplicate={row.duplicate_count} invalid={row.invalid_count} errors={row.error_count}")
    return 0


def _run(job_type: str, provider: str, fn: Callable[[DataIngestionRunRecord], None]) -> int:
    assert_schema_ready_for_writes(engine)
    now = datetime.now(timezone.utc)
    run_id = f"{job_type.lower()}-{uuid.uuid4().hex[:16]}"
    with SessionLocal() as session:
        active = session.scalars(select(DataIngestionRunRecord).where(DataIngestionRunRecord.job_type == job_type, DataIngestionRunRecord.status == "RUNNING")).first()
        if active is not None:
            return 2
        row = DataIngestionRunRecord(run_id=run_id, job_type=job_type, provider=provider, status="RUNNING", started_at=now)
        session.add(row)
        session.commit()
        session.refresh(row)
        run_pk = row.id
    try:
        with SessionLocal() as session:
            row = session.get(DataIngestionRunRecord, run_pk)
            if row is None:
                return 1
            fn(row)
            row.status = "SUCCEEDED" if row.error_count == 0 else "PARTIAL"
            row.completed_at = datetime.now(timezone.utc)
            _provider_health(session, provider, "HEALTHY", None)
            session.commit()
        return 0
    except Exception as exc:
        with SessionLocal() as session:
            row = session.get(DataIngestionRunRecord, run_pk)
            if row is not None:
                row.status = "FAILED"
                row.error_count += 1
                row.error_summary_json = json.dumps([{"type": type(exc).__name__, "message": str(exc)[:500]}], ensure_ascii=False)
                row.completed_at = datetime.now(timezone.utc)
            _provider_health(session, provider, "UNAVAILABLE", exc)
            session.commit()
        return 1


def _save_spot(provider: PublicMarketProvider, run: DataIngestionRunRecord) -> None:
    quotes = provider.fetch_spot_quotes()
    with SessionLocal() as session:
        run = session.merge(run)
        for quote in quotes:
            existing = session.scalars(select(MarketQuoteSnapshotRecord).where(MarketQuoteSnapshotRecord.quote_id == f"{quote.provider}:{quote.symbol}:{quote.market_time.isoformat()}:{quote.checksum}")).first()
            if existing is not None:
                run.duplicate_count += 1
                continue
            row = MarketQuoteSnapshotRecord(
                quote_id=f"{quote.provider}:{quote.symbol}:{quote.market_time.isoformat()}:{quote.checksum}",
                provider=quote.provider,
                symbol=quote.symbol,
                exchange=quote.exchange,
                trading_date=quote.market_time.date().isoformat(),
                market_time=quote.market_time,
                received_at=datetime.now(timezone.utc),
                validated_at=datetime.now(timezone.utc),
                open_price=str(quote.open_price),
                high_price=str(quote.high_price),
                low_price=str(quote.low_price),
                last_price=str(quote.last_price),
                previous_close=None if quote.previous_close is None else str(quote.previous_close),
                volume=quote.volume,
                amount=None if quote.amount is None else str(quote.amount),
                suspension_status="NORMAL",
                data_checksum=quote.checksum,
                calendar_version="public-market",
                raw_schema_version="6b-v1",
                quality_status="VALID",
            )
            session.add(row)
            try:
                session.flush()
                run.success_count += 1
            except IntegrityError:
                session.rollback()
                run = session.merge(run)
                run.duplicate_count += 1
        session.commit()


def _save_news(provider: PublicMarketProvider, symbols: list[str], run: DataIngestionRunRecord) -> None:
    items = provider.fetch_stock_news(symbols)
    with SessionLocal() as session:
        run = session.merge(run)
        for item in items:
            existing = session.scalars(select(StockNewsRecord).where(StockNewsRecord.checksum == item.checksum)).first()
            if existing is not None:
                run.duplicate_count += 1
                continue
            session.add(StockNewsRecord(provider=item.provider, symbol=item.symbol, title=item.title, summary=item.summary, source_url=item.source_url, source_url_hash=item.source_url_hash, published_at=item.published_at, checksum=item.checksum, received_at=datetime.now(timezone.utc)))
            try:
                session.flush()
                run.success_count += 1
            except IntegrityError:
                session.rollback()
                run = session.merge(run)
                run.duplicate_count += 1
        session.commit()


def _save_industries(provider: PublicMarketProvider, run: DataIngestionRunRecord) -> None:
    items = provider.fetch_industries()
    with SessionLocal() as session:
        run = session.merge(run)
        for item in items:
            existing = session.scalars(select(IndustrySnapshotRecord).where(IndustrySnapshotRecord.provider == item.provider, IndustrySnapshotRecord.industry_name == item.industry_name, IndustrySnapshotRecord.market_time == item.market_time, IndustrySnapshotRecord.checksum == item.checksum)).first()
            if existing is not None:
                run.duplicate_count += 1
                continue
            session.add(IndustrySnapshotRecord(provider=item.provider, industry_name=item.industry_name, market_time=item.market_time, change_pct=item.change_pct, turnover=item.turnover, leading_stock=item.leading_stock, checksum=item.checksum, received_at=datetime.now(timezone.utc)))
            try:
                session.flush()
                run.success_count += 1
            except IntegrityError:
                session.rollback()
                run = session.merge(run)
                run.duplicate_count += 1
        session.commit()


def _provider_health(session, provider: str, status: str, exc: Exception | None) -> None:
    now = datetime.now(timezone.utc)
    row = session.scalars(select(ProviderHealthStatusRecord).where(ProviderHealthStatusRecord.provider == provider)).first()
    if row is None:
        row = ProviderHealthStatusRecord(provider=provider, status=status, updated_at=now)
        session.add(row)
    row.status = status
    row.updated_at = now
    if exc is None:
        row.last_success_at = now
        row.last_error_type = ""
        row.last_error_message = ""
    else:
        row.last_failure_at = now
        row.last_error_type = type(exc).__name__
        row.last_error_message = str(exc)[:500]


if __name__ == "__main__":
    raise SystemExit(main())
