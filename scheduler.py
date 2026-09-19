import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from db import Database

log = logging.getLogger("scheduler")
scheduler = AsyncIOScheduler()


async def _periodic_search(db: Database, service):
    """Проходит по юзерам, у кого интервал уже истёк, и ищет."""
    users = await db.all_users()
    for user in users:
        if not user.get("onboarding_done") or not user.get("notifications_enabled", 1):
            continue
        uid = user["telegram_id"]
        interval_h = user.get("search_interval_hours") or config.DEFAULT_SEARCH_INTERVAL_HOURS
        last = user.get("last_search_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if datetime.now(timezone.utc) - last_dt < timedelta(hours=interval_h):
                    continue
            except ValueError:
                pass
        if service.is_searching(uid):
            log.info("Фон пропустил user=%s (поиск уже идёт)", uid)
            continue
        try:
            async with await service.search_lock(uid):
                res = await service.run_search(uid, silent=False)
            log.info("Фон user=%s -> %s", uid, res)
        except Exception as e:
            log.exception("Фоновая ошибка user=%s: %s", uid, e)
        await asyncio.sleep(5)


async def start_scheduler(db: Database, service):
    trigger = IntervalTrigger(minutes=config.SCHEDULER_TICK_MIN, jitter=120)
    scheduler.add_job(
        _periodic_search, trigger, args=[db, service],
        id="periodic_search", replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=3600,
    )
    scheduler.start()
    log.info("Scheduler запущен, тик %s мин (интервалы юзеров per-user)",
             config.SCHEDULER_TICK_MIN)