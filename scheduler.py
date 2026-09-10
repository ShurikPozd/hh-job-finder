import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from db import Database

log = logging.getLogger("scheduler")
scheduler = AsyncIOScheduler()


async def _periodic_search(db: Database, service):
    """Проходит по всем юзерам с включёнными уведомлениями и ищет."""
    users = await db.all_users()
    for user in users:
        if not user.get("onboarding_done") or not user.get("notifications_enabled", 1):
            continue
        try:
            res = await service.run_search(user["telegram_id"], silent=False)
            log.info("Фон user=%s -> %s", user["telegram_id"], res)
        except Exception as e:
            log.exception("Фоновая ошибка user=%s: %s", user["telegram_id"], e)
        await asyncio.sleep(5)


async def start_scheduler(db: Database, service):
    trigger = IntervalTrigger(hours=config.DEFAULT_SEARCH_INTERVAL_HOURS, jitter=300)
    scheduler.add_job(
        _periodic_search, trigger, args=[db, service],
        id="periodic_search", replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=3600,
    )
    scheduler.start()
    log.info("Scheduler запущен, интервал %s ч", config.DEFAULT_SEARCH_INTERVAL_HOURS)