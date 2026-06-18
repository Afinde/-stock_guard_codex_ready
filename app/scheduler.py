from __future__ import annotations

import asyncio

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .notifier import send_webhook
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


def main() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    # A股交易日判断应接入交易日历；MVP仅按工作日调度。
    for hour, minute in [(9, 35), (10, 30), (14, 30)]:
        scheduler.add_job(run_scan_job, "cron", day_of_week="mon-fri", hour=hour, minute=minute)
    scheduler.start()


if __name__ == "__main__":
    main()
