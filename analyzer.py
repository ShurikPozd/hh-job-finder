import asyncio
import json
import logging
import re
import time

import requests
import config

log = logging.getLogger("analyzer")

BANK_TEMPLATE = """# Банк профиля — {name}

Рабочая основа для сопроводительных писем под вакансии hh.ru.
Использование: берём блоки ниже, адаптируем под ключевые слова вакансии, вставляем в форму hh.ru.

## 1. Самопрезентация (варианты)
Вариант A — {title}: {about}

## 2. Матрица hard skills по категориям
{skills_block}

## 3. Банк измеримых фактов
{facts_block}

## 4. Банк фраз-глаголов и формул достижений
Формула: Глагол + задача/действие + измеримый результат (цифры/сроки/деньги)
Глаголы: Разработал / Спроектировал / Реализовал / Внедрил / Автоматизировал / Оптимизировал / Интегрировал / Развернул
"""

COVER_LETTER_TEMPLATE = """Здравствуйте, откликаюсь на вакансию {vacancy_name}.

{intro}

{body}

{skills_match}

{cta}"""


_groq_lock = asyncio.Lock()
_groq_next_call = 0.0
GROQ_CALL_GAP_SEC = 25.0  # ~25с между вызовами: ~2-3 шт/мин, укладываемся в OTPM


async def _post_groq(system: str, user: str, temperature: float = 0.3,
                     retries: int = 5) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    proxies = {"https": config.GROQ_PROXY, "http": config.GROQ_PROXY}
    if not config.GROQ_PROXY:
        proxies = None
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": config.GROQ_MAX_TOKENS,
        "reasoning_effort": "none",
    }
    async with _groq_lock:
        global _groq_next_call
        delay = _groq_next_call - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(1, retries + 1):
            resp = await asyncio.to_thread(
                requests.post, url, json=payload, headers=headers,
                proxies=proxies, timeout=config.GROQ_TIMEOUT_SEC,
            )
            if resp.status_code == 429 and attempt < retries:
                wait = min(2 ** attempt, 60)
                log.warning("Groq 429 (попытка %s/%s), сон %ss", attempt, retries, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            _groq_next_call = time.monotonic() + GROQ_CALL_GAP_SEC
            return resp.json()["choices"][0]["message"]["content"]
        raise RuntimeError("Groq не ответил после всех попыток")


def _extract_json(text: str) -> dict:
    """Достать JSON-объект из ответа модели (может быть обёрнут в thinking/fence)."""
    text = text or ""
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # qwen-модели любят выдавать "thinking..." перед ответом — берём ПОСЛЕДНИЙ JSON
    # (в thinking-блоке скобки могут попадаться, но финальный ответ — последний)
    candidates = list(re.finditer(r"\{", text))
    for m in reversed(candidates):
        try:
            return json.loads(text[m.start():])
        except Exception:
            continue
    raise ValueError("JSON не найден в ответе модели")


class Analyzer:
    async def score_vacancy(self, vacancy: dict, profile: dict) -> dict:
        """0-10 скоринг соответствия вакансии профилю."""
        system = (
            "Ты — рекрутер, оцениваешь соответствие вакансии профилю кандидата "
            "для junior Python/QA разработчика."
            " Ответь одним JSON-объектом без markdown-разметки: "
            '{"score": 0-10, "summary": "...", "missing_skills": [...], '
            '"matched_skills": [...], "risk": "low|medium|high"}'
        )
        user = json.dumps({
            "vacancy": {
                "title": vacancy.get("name"),
                "description": vacancy.get("description", "")[:6000],
                "skills": vacancy.get("key_skills", []),
            },
            "candidate_profile": {
                "title": profile.get("title"),
                "skills": profile.get("skills", []),
                "experience_years": profile.get("experience_years", 0),
                "about": profile.get("about", ""),
                "projects": profile.get("projects", []),
            },
        }, ensure_ascii=False)
        try:
            out = await _post_groq(system, user)
            data = _extract_json(out)
            return {
                "score": max(0, min(10, int(data.get("score", 0)))),
                "summary": data.get("summary", ""),
                "missing_skills": data.get("missing_skills", []),
                "matched_skills": data.get("matched_skills", []),
                "risk": data.get("risk", "low"),
            }
        except Exception as e:
            log.exception("Ошибка скоринга")
            return {"score": 0, "summary": "", "missing_skills": [],
                    "matched_skills": [], "risk": "unknown"}

    async def generate_cover_letter(self, vacancy: dict, profile: dict, bank: str | None) -> str:
        """Сопроводительное из банка или шаблона."""
        if not bank:
            system = (
                "Ты — колаборатор молодёжного junior-разработчика Черно. "
                "Составляешь сопроводительное письмо на русском по правилам ATS: "
                "монотекст, без таблиц, без местоимений в начале фраз, "
                "глагол+действие+результат. Письмо на hh.ru, до 3000 символов."
            )
            bank_ref = ""
        else:
            system = (
                "Ты — помощник юзера. У него есть банк профиля для сопроводительных. "
                "Составь персональное сопроводительное письмо под вакансию, "
                "максимально используя факты и формулировки из банка. "
                "Правила ATS: монотекст, без таблиц, без лишних местоимений, "
                "глагол+действие+результат, до 3000 символов."
            )
            bank_ref = bank[:6000]
        user = json.dumps({
            "vacancy": {
                "title": vacancy.get("name"),
                "company": vacancy.get("employer_name"),
                "description": vacancy.get("description", "")[:5000],
            },
            "candidate_profile": profile,
            "profile_bank": bank_ref,
        }, ensure_ascii=False)
        try:
            out = await _post_groq(system, user, 0.5)
            data = _extract_json(out)
            return data.get("letter", data.get("cover_letter", out))
        except Exception as e:
            log.exception("Ошибка генерации письма")
            return "Не удалось сгенерировать письмо. Попробуйте позже."

    async def generate_bank(self, profile: dict) -> str:
        """Генерация банка профиля по данным пользователя."""
        system = (
            "Ты — карьерный консультант. По профилю кандидата генерируешь "
            "банк профиля в Markdown для генерации сопроводительных писем hh.ru. "
            "Структура: самопрезентация, матрица навыков, измеримые факты, "
            "фразы-глаголы. Никаких выдуманных фактов — только данные профиля. "
            "Отвечай строго JSON с полем 'bank'."
        )
        user = json.dumps(profile, ensure_ascii=False)
        try:
            out = await _post_groq(system, user)
            data = _extract_json(out)
            return data.get("bank", BANK_TEMPLATE.format(
                name=profile.get("name", "Кандидат"),
                title=profile.get("title", ""),
                about=profile.get("about", ""),
                skills_block=", ".join(profile.get("skills", [])),
                facts_block=profile.get("about", ""),
            ))
        except Exception as e:
            log.exception("Ошибка генерации банка")
            return BANK_TEMPLATE.format(
                name=profile.get("name", "Кандидат"),
                title=profile.get("title", ""),
                about=profile.get("about", ""),
                skills_block=", ".join(profile.get("skills", [])),
                facts_block=profile.get("about", ""),
            )

    async def parse_resume(self, text: str) -> dict:
        """Парсинг текста резюме в структурированный профиль."""
        system = (
            "Ты — парсер резюме. Из текста резюме извлекаешь данные и "
            "возвращаешь строго JSON: "
            '{"name","title","skills":[],"experience_years","languages":[],'
            '"education","city","salary_expectation","about","projects":[]} '
            "Опыт указывай с округлением, например 1.5."
        )
        try:
            out = await _post_groq(system, text[:12000])
            data = _extract_json(out)
            skills = data.get("skills", [])
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.split(",") if s.strip()]
            projects = data.get("projects", [])
            if isinstance(projects, str):
                projects = [p.strip() for p in projects.split(";") if p.strip()]
            return {
                "name": data.get("name", ""),
                "title": data.get("title", ""),
                "skills": skills,
                "experience_years": float(data.get("experience_years", 0) or 0),
                "languages": data.get("languages", []),
                "education": data.get("education", ""),
                "city": data.get("city", ""),
                "salary_expectation": int(data.get("salary_expectation", 0) or 0),
                "about": data.get("about", ""),
                "projects": projects,
            }
        except Exception as e:
            log.exception("Ошибка парсинга резюме")
            return {}