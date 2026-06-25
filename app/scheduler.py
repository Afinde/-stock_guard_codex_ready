from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select

from .config import get_settings
from .data_provider import LocalTradingCalendar, MarketDataError
from .db import ScheduledTaskRunRecord, SessionLocal
from .notifier import send_webhook
from .risk import stable_id
from .service import scan_watchlist


def run_scan_job() -> None:
    settings = get_settings()
    results = scan_watchlist()
    candidates = [x for x in results if x.get("action") == "BUY_WATCH"]
    if candidates:
        lines = [
            f"{x['symbol']} 分数{x['score']} 参考价{x['price']} 止损{x['stop_price']} "
            f"目标{x['take_profit_1']}/{x['take_profit_2']} 建议{x['suggested_shares']}股"
            for x in candidates
        ]
        asyncio.run(send_webhook(settings.webhook_url, "股票监控候选提醒", "\n".join(lines)))


def enqueue_recommendation_job(phase: str) -> None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    if not _is_trading_day(settings, now):
        return
    task_type = "PRE_MARKET_RECOMMENDATION" if phase == "pre_market" else "POST_MARKET_RECOMMENDATION"
    task_key = stable_id("recommendation", task_type, now.date().isoformat())
    with SessionLocal() as session:
        existing = session.scalars(select(ScheduledTaskRunRecord).where(ScheduledTaskRunRecord.task_key == task_key)).first()
        if existing is not None:
            return
        row = ScheduledTaskRunRecord(
            task_run_id=task_key,
            task_key=task_key,
            task_type=task_type,
            session_date=now.date().isoformat(),
            trading_date=now.date().isoformat(),
            scheduled_at=now,
            status="QUEUED",
            attempt=1,
            idempotency_key=task_key,
            started_at=now,
        )
        session.add(row)
        session.commit()


def main() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    for hour, minute in [(9, 35), (10, 30), (14, 30)]:
        scheduler.add_job(run_scan_job, "cron", day_of_week="mon-fri", hour=hour, minute=minute)
    scheduler.add_job(enqueue_recommendation_job, "cron", hour=8, minute=45, args=["pre_market"])
    scheduler.add_job(enqueue_recommendation_job, "cron", hour=15, minute=30, args=["post_market"])
    scheduler.start()


def _is_trading_day(settings, now: datetime) -> bool:
    try:
        calendar = LocalTradingCalendar.from_file(
            settings.market_calendar_resolved_path,
            close_time=settings.market_close_time_value,
            timezone=settings.timezone,
        )
        return calendar.is_trading_day(now.date())
    except MarketDataError:
        return False


if __name__ == "__main__":
    main()
