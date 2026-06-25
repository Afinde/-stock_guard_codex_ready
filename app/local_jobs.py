from __future__ import annotations

import argparse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import get_settings
from .db import ScheduledTaskRunRecord, SessionLocal, engine
from .db_backup import backup_sqlite_database, sqlite_database_path
from .data_jobs import run_market_spot, run_stock_news, run_industry
from .public_market_data import provider_by_name
from .schema import assert_schema_ready_for_writes
from .service import scan_watchlist

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
RECOMMENDATION_TASKS = {"PRE_MARKET_RECOMMENDATION", "POST_MARKET_RECOMMENDATION"}
ALLOWED_TASKS = {"WATCHLIST_SCAN", "DATA_MAINTENANCE", "SQLITE_BACKUP", "MARKET_SPOT_SYNC", "DAILY_BAR_SYNC", "STOCK_NEWS_SYNC", "INDUSTRY_SYNC", "FINANCIAL_SYNC", "INSTRUMENT_SYNC", *RECOMMENDATION_TASKS}


def run_pending_once() -> int:
    with SessionLocal() as session:
        job = session.scalars(
            select(ScheduledTaskRunRecord)
            .where(ScheduledTaskRunRecord.status == "QUEUED", ScheduledTaskRunRecord.task_type.in_(ALLOWED_TASKS))
            .order_by(ScheduledTaskRunRecord.started_at.asc())
            .limit(1)
        ).first()
        if job is None:
            return 0
        return run_one(job.task_run_id or job.task_key)


def run_one(job_id: str | None = None) -> int:
    settings = get_settings()
    assert_schema_ready_for_writes(engine)
    with SessionLocal() as session:
        query = select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.status.in_(["QUEUED", "RUNNING"]))
        if job_id:
            query = query.where((ScheduledTaskRunRecord.task_run_id == job_id) | (ScheduledTaskRunRecord.task_key == job_id))
        job = session.scalars(query.order_by(ScheduledTaskRunRecord.started_at.asc()).limit(1)).first()
        if job is None:
            return 0
        if job.task_type not in ALLOWED_TASKS:
            job.status = "FAILED"
            job.error_type = "UNSUPPORTED_TASK"
            job.error_message = f"unsupported task type: {job.task_type}"
            job.completed_at = datetime.now(TZ)
            session.commit()
            return 2
        job.status = "RUNNING"
        job.lease_owner = "local-job-runner"
        job.started_at = datetime.now(TZ)
        job_pk = job.id
        task_type = job.task_type
        session.commit()

    try:
        if task_type == "WATCHLIST_SCAN":
            if not settings.enable_light_scan:
                raise RuntimeError("light scan capability disabled")
            scan_watchlist()
        elif task_type == "SQLITE_BACKUP":
            source = sqlite_database_path(settings.database_url)
            backup_sqlite_database(source, source.parent.parent / "backups" / "sqlite")
        elif task_type == "MARKET_SPOT_SYNC":
            run_market_spot(provider_by_name("fixture"))
        elif task_type == "STOCK_NEWS_SYNC":
            run_stock_news(provider_by_name("fixture"))
        elif task_type == "INDUSTRY_SYNC":
            run_industry(provider_by_name("fixture"))
        elif task_type in RECOMMENDATION_TASKS:
            provider = provider_by_name("fixture")
            run_market_spot(provider)
            run_industry(provider)
            if settings.enable_light_scan:
                scan_watchlist()
        else:
            logger.info("data maintenance task completed without state changes", extra={"job_id": job_id})
        status, error_type, message, code = "SUCCEEDED", "", "", 0
    except Exception as exc:
        logger.exception("local job failed", extra={"job_id": job_id, "task_type": task_type})
        status, error_type, message, code = "FAILED", type(exc).__name__, str(exc), 1

    with SessionLocal() as session:
        row = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.id == job_pk)).first()
        if row is not None:
            row.status = status
            row.error_type = error_type
            row.error_message = message[:2000]
            row.completed_at = datetime.now(TZ)
            session.commit()
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one local lightweight Stock Guard job.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-one", action="store_true")
    group.add_argument("--run-pending-once", action="store_true")
    parser.add_argument("--job-id", default=None)
    args = parser.parse_args(argv)
    if args.run_pending_once:
        return run_pending_once()
    return run_one(args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
