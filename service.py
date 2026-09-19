import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import Bot, types

import config
from analyzer import (Analyzer, set_usage_hook, set_usage_source,
                      reset_usage_source)
from db import Database, utcnow
from formatters import format_card
from hh_client import HHClient, extract_inn, text_has_accreditation
from keyboards import vacancy_keyboard
from registry import RegistryChecker

log = logging.getLogger("service")


class VacancyService:
    def __init__(self, db: Database, bot: Bot):
        self.db = db
        self.bot = bot
        self.hh = HHClient()
        self.analyzer = Analyzer()
        set_usage_hook(db.add_llm_usage)
        self.registry = RegistryChecker(db)
        self._locks: dict[int, asyncio.Lock] = {}

    def is_searching(self, user_id: int) -> bool:
        lock = self._locks.get(user_id)
        return lock is not None and lock.locked()

    async def search_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        return lock

    # ============ Пайплайн поиска ============

    async def run_search(self, user_id: int, silent: bool = False,
                         top_n: int | None = None) -> dict:
        """Полный проход: поиск → детали → аккредитация → скоринг → отправка.
        top_n: при ручном поиске отправить только N лучших по соответствию.
        silent: искать и подсчитать, но ничего не отправлять в чат."""
        user = await self.db.get_user(user_id)
        if not user:
            return {"found": 0, "sent": 0}

        keywords = self._user_keywords(user)
        experience = user["experience"] or config.HH_EXPERIENCE
        schedule = user["schedule"] or config.HH_SCHEDULE
        area = user["area"] or config.HH_AREA
        min_salary = user["min_salary"] or config.HH_MIN_SALARY
        threshold = user["match_threshold"] or config.DEFAULT_MATCH_THRESHOLD

        fetched: list[dict] = []
        seen_ids: set[str] = set()
        for kw in keywords:
            try:
                data = await self.hh.search(
                    kw, area=area, experience=experience,
                    schedule=schedule, period=config.HH_PERIOD,
                    min_salary=min_salary, per_page=config.HH_PER_PAGE,
                )
                if not data:
                    continue
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning("Ошибка поиска «%s»: %s", kw, e)
                continue

            for item in data.get("items", []):
                vid = str(item.get("id"))
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    fetched.append(item)

        log.info("Найдено %d вакансий для user %s", len(fetched), user_id)

        # Сколько из выдачи ещё не видел юзер (новые/отложенные прошлых прогонов)
        new_cnt = 0
        for it in fetched:
            if not await self.db.is_seen(user_id, str(it["id"])):
                new_cnt += 1

        # Бюджет скоринга в токенах: не жжём free-квоту (её делят tg-saver и
        # расширение пользователя) и не теряем вакансии — что не успели оценить,
        # останется необработанным и попадёт в следующий прогон
        score_models = [m for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL) if m]
        day_used = await self.db.llm_tokens_today(score_models)
        est = config.SCORE_EST_TOKENS
        budget = min(
            config.SCORE_TOKEN_BUDGET_PER_RUN,
            max(0, config.SCORE_TOKEN_BUDGET_PER_DAY - day_used),
        )

        sent = 0
        scored = 0
        deferred = 0
        spent_total = 0
        collected: list[dict] = []
        for item in fetched:
            vid = str(item["id"])
            if await self.db.is_seen(user_id, vid):
                continue
            if budget < est:
                deferred += 1
                continue
            try:
                detail = await self.hh.get_vacancy(vid)
            except Exception as e:
                log.warning("Ошибка деталей %s: %s", vid, e)
                deferred += 1
                continue

            employer_id = self._emp_id(detail.get("employer_href"))
            if employer_id and await self.db.has_hidden_employer(user_id, employer_id):
                await self.db.mark_seen(user_id, vid, "hidden")
                continue

            profile = json.loads(user["profile"] or "{}")
            rec = await self._collect_record(item, detail)
            rec["employer_id"] = employer_id

            # Жёсткий фильтр ДО LLM: не платим за то, что всё равно выбросим
            accredited_only = user.get("only_accredited", 1)
            if accredited_only and not rec.get("accredited_it"):
                await self.db.mark_seen(user_id, vid, "seen")
                log.info("Фильтр: %s отброшена (нет аккредитации)", vid)
                continue

            spent_before = await self.db.llm_tokens_today(score_models)
            score_data = await self.analyzer.score_vacancy(rec, profile)
            spent = max(0, (await self.db.llm_tokens_today(score_models)) - spent_before)
            spent_total += spent
            budget -= spent
            if score_data is None:
                # Квота/сеть: НЕ помечаем seen, переоценим в следующий прогон
                deferred += 1
                continue
            scored += 1
            rec["score"] = score_data["score"]
            rec["llm_score"] = score_data["score"]
            rec["llm_summary"] = score_data.get("summary", "")
            rec["score_data"] = score_data

            if rec.get("score", 0) >= threshold:
                await self.db.upsert_vacancy(rec)
                await self.db.mark_seen(user_id, vid, "seen")
                if top_n is not None:
                    collected.append(rec)
                elif not silent and user.get("notifications_enabled", 1):
                    await self._send_vacancy(user_id, rec)
                    sent += 1
                elif silent:
                    sent += 1
            else:
                rec["below_threshold"] = 1
                await self.db.upsert_vacancy(rec)
                await self.db.mark_seen(user_id, vid, "seen")

        if top_n is not None and collected:
            collected.sort(key=lambda r: r.get("llm_score", 0) or 0, reverse=True)
            for rec in collected[: top_n]:
                await self._send_vacancy(user_id, rec)
                sent += 1

        await self.db.upsert_user(user_id, last_search_at=utcnow())
        if deferred:
            log.warning("Отложено (квота/сбой): %d вакансий; токенов за прогон %d, "
                        "лимит дня %d (осталось ~%d)",
                        deferred, spent_total, config.SCORE_TOKEN_BUDGET_PER_DAY,
                        max(0, int(budget)))
        return {"found": len(fetched), "new": new_cnt, "scored": scored, "sent": sent,
                "deferred": deferred, "tokens": spent_total,
                "threshold": threshold,
                "already_seen": len(fetched) - new_cnt,
                "budget_day_free": max(0, config.SCORE_TOKEN_BUDGET_PER_DAY - day_used)}

    # ============ Сборка записи о вакансии ============

    async def _collect_record(self, item: dict, detail: dict) -> dict:
        """Данные о вакансии + аккредитация БЕЗ обращения к LLM."""
        vid = str(detail["id"])
        sources = {}

        # источник 1: hh.ru (бейдж на странице вакансии)
        accr_hh = bool(detail.get("accredited_by_hh"))
        sources["hh.ru"] = accr_hh

        # источник 2: реестр Минцифры по ИНН работодателя
        inn = None
        emp_id = self._emp_id(detail.get("employer_href"))
        if emp_id:
            try:
                emp = await self.hh.get_employer(emp_id)
                inn = emp.get("inn") or extract_inn(emp.get("name") or "")
            except Exception:
                pass
        reg_ok = False
        if inn:
            try:
                reg_ok, reg_name = await self.registry.check_inn(inn)
            except Exception as e:
                log.warning("Реестр для ИНН %s: %s", inn, e)
                reg_ok = False
            sources["реестр"] = reg_ok
        else:
            sources["реестр"] = None

        # источник 3: текст вакансии (маркеры)
        desc = detail.get("description") or ""
        text_ok = text_has_accreditation(desc)
        sources["описание"] = text_ok

        accredited = bool(accr_hh or reg_ok or text_ok)
        source_str = " | ".join(
            f"{k}: {'✓' if v else ('—' if v is None else '✗')}"
            for k, v in sources.items()
        )

        skills = detail.get("skills") or []
        record = {
            "vacancy_id": vid,
            "name": detail.get("title") or item.get("title"),
            "employer_name": detail.get("employer") or item.get("employer"),
            "employer_id": emp_id,
            "salary_text": detail.get("salary_text") or item.get("salary_text"),
            "experience_name": detail.get("experience") or item.get("experience"),
            "description": desc,
            "key_skills": skills,
            "area_name": (detail.get("address") or item.get("address") or "").split(",")[0].strip(),
            "url": detail.get("url") or item.get("url") or f"https://hh.ru/vacancy/{vid}",
            "accredited_it": 1 if accredited else 0,
            "accreditation_source": source_str,
        }
        return record

    @staticmethod
    def _emp_id(href: str | None) -> str | None:
        if not href:
            return None
        m = re.search(r"/employer/(\d+)", href)
        return m.group(1) if m else None

    # ============ Отправка в Telegram ============

    async def _send_vacancy(self, user_id: int, rec: dict, note: str | None = None) -> None:
        text = self.format_vacancy(rec)
        if note:
            text = note + "\n\n" + text
        try:
            msg = await self.bot.send_message(
                user_id, text, reply_markup=vacancy_keyboard(rec["vacancy_id"]),
                disable_web_page_preview=True,
            )
            await self.db.save_sent_message(user_id, rec["vacancy_id"], msg.message_id)
        except Exception as e:
            log.warning("Не удалось отправить %s: %s", rec["vacancy_id"], e)

    def format_vacancy(self, rec: dict) -> str:
        return format_card(rec)

    # ============ Перепроверка пропущенных вакансий ============

    async def process_recheck_vacancy(self, user_id: int, vacancy_id: str) -> str:
        """Одна вакансия из очереди перепроверки (seen-без-в-базе).

        → 'sent' | 'done' | 'failed' | 'no_budget'.
        Расход токенов учитывается под source='recheck'."""
        user = await self.db.get_user(user_id)
        if not user:
            return "done"
        threshold = user.get("match_threshold") or config.DEFAULT_MATCH_THRESHOLD
        profile = json.loads(user.get("profile") or "{}")
        try:
            detail = await self.hh.get_vacancy(vacancy_id)
        except Exception as e:
            log.warning("Перепроверка %s: детали: %s", vacancy_id, e)
            return "failed"

        rec = await self._collect_record({}, detail)
        employer_id = self._emp_id(detail.get("employer_href"))
        rec["employer_id"] = employer_id
        if employer_id and await self.db.has_hidden_employer(user_id, employer_id):
            await self.db.mark_seen(user_id, vacancy_id, "hidden")
            return "done"
        # Фильтр ДО LLM — за отсев ниже порога не платим
        if user.get("only_accredited", 1) and not rec.get("accredited_it"):
            await self.db.mark_seen(user_id, vacancy_id, "seen")
            return "done"

        score_models = [m for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL) if m]
        if await self.db.llm_tokens_today(score_models) >= config.SCORE_TOKEN_BUDGET_PER_DAY:
            return "no_budget"

        token = set_usage_source("recheck")
        try:
            score_data = await self.analyzer.score_vacancy(rec, profile)
        finally:
            reset_usage_source(token)
        if score_data is None:
            return "failed"
        rec["score"] = score_data["score"]
        rec["llm_score"] = score_data["score"]
        rec["llm_summary"] = score_data.get("summary", "")
        rec["score_data"] = score_data

        if score_data["score"] >= threshold:
            await self.db.upsert_vacancy(rec)
            await self.db.mark_seen(user_id, vacancy_id, "seen")
            if user.get("notifications_enabled", 1):
                await self._send_vacancy(user_id, rec, note="↩ найдена при перепроверке")
            return "sent"
        rec["below_threshold"] = 1
        await self.db.upsert_vacancy(rec)
        await self.db.mark_seen(user_id, vacancy_id, "seen")
        return "done"

    # ============ Вспомогательные ============

    def _user_keywords(self, user: dict) -> list[str]:
        if user.get("keywords"):
            return [k.strip() for k in user["keywords"].split(",") if k.strip()]
        return config.HH_DEFAULT_KEYWORDS