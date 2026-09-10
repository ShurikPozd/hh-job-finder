import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import Bot, types

import config
from analyzer import Analyzer
from db import Database
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
        self.registry = RegistryChecker(db)

    # ============ Пайплайн поиска ============

    async def run_search(self, user_id: int, silent: bool = False) -> dict:
        """Полный проход: поиск → детали → аккредитация → скоринг → отправка."""
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

        sent = 0
        for item in fetched:
            vid = str(item["id"])
            if await self.db.is_seen(user_id, vid):
                continue
            try:
                detail = await self.hh.get_vacancy(vid)
            except Exception as e:
                log.warning("Ошибка деталей %s: %s", vid, e)
                continue

            employer_id = self._emp_id(detail.get("employer_href"))
            if employer_id and await self.db.has_hidden_employer(user_id, employer_id):
                await self.db.mark_seen(user_id, vid, "hidden")
                continue

            profile = json.loads(user["profile"] or "{}")
            rec = await self._build_record(item, detail, profile)
            rec["employer_id"] = employer_id

            if rec.get("score", 0) >= threshold:
                await self.db.upsert_vacancy(rec)
                await self.db.mark_seen(user_id, vid, "seen")
                if not silent and user.get("notifications_enabled", 1):
                    await self._send_vacancy(user_id, rec)
                    sent += 1
                if silent:
                    sent += 1
            else:
                await self.db.mark_seen(user_id, vid, "seen")

        return {"found": len(fetched), "sent": sent}

    # ============ Сборка записи о вакансии ============

    async def _build_record(self, item: dict, detail: dict, profile: dict) -> dict:
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

        score_data = await self.analyzer.score_vacancy(record, profile)
        record["score"] = score_data["score"]
        record["llm_score"] = score_data["score"]
        record["llm_summary"] = score_data.get("summary", "")
        record["score_data"] = score_data
        return record

    @staticmethod
    def _emp_id(href: str | None) -> str | None:
        if not href:
            return None
        m = re.search(r"/employer/(\d+)", href)
        return m.group(1) if m else None

    # ============ Отправка в Telegram ============

    async def _send_vacancy(self, user_id: int, rec: dict) -> None:
        text = self.format_vacancy(rec)
        try:
            await self.bot.send_message(
                user_id, text, reply_markup=vacancy_keyboard(rec["vacancy_id"]),
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("Не удалось отправить %s: %s", rec["vacancy_id"], e)

    def format_vacancy(self, rec: dict) -> str:
        score = int(rec.get("llm_score", 0) or 0)
        score = max(0, min(10, score))
        bar = "█" * score + "░" * (10 - score)
        accr = "✅ IT-аккредитация" if rec.get("accredited_it") else "❓ Аккредитация не найдена"
        if rec.get("accreditation_source"):
            accr += f"\n   источники: {rec.get('accreditation_source')}"
        salary = rec.get("salary_text") or "не указана"
        skills = rec.get("key_skills") or []
        if isinstance(skills, str):
            skills = json.loads(skills) if skills else []
        skills_str = ", ".join(skills[:10]) if skills else "—"
        sd = rec.get("score_data") or {}

        lines = [
            f"💼 {rec.get('name')}",
            f"🏢 {rec.get('employer_name') or '—'}",
            f"📍 {rec.get('area_name') or '—'} · {rec.get('experience_name') or '—'}",
            f"💰 {salary}",
            f"🛠 {skills_str}",
            f"   {accr}",
            f"",
            f"Соответствие: {bar} {score}/10",
        ]
        if sd.get("summary"):
            lines.append(f"   {sd['summary']}")
        if sd.get("missing_skills"):
            lines.append(f"⚠️ Не хватает: {', '.join(sd['missing_skills'][:5])}")
        if sd.get("risk"):
            lines.append(f"🎯 Риск: {sd['risk']}")
        lines.append("")
        lines.append(f"🗓 Прогнозируемый {config.GROQ_MODEL.split('/')[-1]}")
        return "\n".join(lines)

    # ============ Вспомогательные ============

    def _user_keywords(self, user: dict) -> list[str]:
        if user.get("keywords"):
            return [k.strip() for k in user["keywords"].split(",") if k.strip()]
        return config.HH_DEFAULT_KEYWORDS