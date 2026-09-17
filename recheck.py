import asyncio
import logging

import config

log = logging.getLogger("recheck")


async def recheck_loop(service):
    """Фоновый воркер: очередь пропущенных вакансий (seen-без-в-базе).

    Каждые RECHECK_INTERVAL_MIN минут берёт пачку, фильтрует по аккредитации
    и скорингу в рамках RECHECK_TOKEN_BUDGET_PER_DAY (учёт через source='recheck'),
    находки присылает с пометкой «↩ найдена при перепроверке»."""
    if not config.RECHECK_ENABLED:
        log.info("Перепроверка выключена (RECHECK_ENABLED=0)")
        return
    await asyncio.sleep(30)  # дать боту полностью подняться
    await _seed_if_empty(service)
    log.info("Воркер перепроверки запущен (интервал %s мин)",
             config.RECHECK_INTERVAL_MIN)
    while True:
        try:
            await _run_batch(service)
        except Exception as e:
            log.error("Перепроверка (батч): %s", e)
        await asyncio.sleep(config.RECHECK_INTERVAL_MIN * 60)


async def _seed_if_empty(service):
    """Автозаполнение очереди при первом старте (если она совсем пуста)."""
    try:
        stats = await service.db.recheck_stats()
        if not any(stats.values()):
            added = await service.db.recheck_seed()
            log.info("Очередь перепроверки заполнена: %s вакансий", added)
    except Exception as e:
        log.warning("Перепроверка: не удалось заполнить очередь: %s", e)


async def _run_batch(service):
    db = service.db
    models = [m for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL) if m]
    used = await db.llm_tokens_today(models, source="recheck")
    if used >= config.RECHECK_TOKEN_BUDGET_PER_DAY:
        log.info("Перепроверка: бюджет дня исчерпан (%s/%s)",
                 used, config.RECHECK_TOKEN_BUDGET_PER_DAY)
        return
    pending = await db.recheck_next(config.RECHECK_BATCH)
    if not pending:
        log.info("Перепроверка: очередь пуста")
        return
    log.info("Перепроверка: беру %s вакансий (перепроверено сегодня %s токенов)",
             len(pending), used)
    for item in pending:
        used = await db.llm_tokens_today(models, source="recheck")
        if used >= config.RECHECK_TOKEN_BUDGET_PER_DAY:
            log.info("Перепроверка: бюджет исчерпан, остальные ждут")
            break
        user_id = item["user_id"]
        vid = item["vacancy_id"]
        try:
            res = await service.process_recheck_vacancy(user_id, vid)
        except Exception as e:
            log.error("Перепроверка %s: %s", vid, e)
            res = "failed"
        if res == "sent":
            await db.recheck_finish(user_id, vid, "sent")
            log.info("Перепроверка %s: найдена по профилю и отправлена", vid)
        elif res == "done":
            await db.recheck_finish(user_id, vid, "done")
            log.info("Перепроверка %s: мимо (фильтр/порог) — закрыта", vid)
        elif res == "no_budget":
            break
        else:
            if await db.recheck_fail(user_id, vid):
                log.warning("Перепроверка %s: 3 ошибки подряд — в failed", vid)
            else:
                log.warning("Перепроверка %s: ошибка, попробуем позже", vid)