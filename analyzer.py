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
                     retries: int = 5, max_tokens: int | None = None) -> str:
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
        "max_tokens": max_tokens or config.GROQ_MAX_TOKENS,
        "reasoning_effort": "none",
    }
    async with _groq_lock:
        global _groq_next_call
        delay = _groq_next_call - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(1, retries + 1):
            try:
                resp = await asyncio.to_thread(
                    requests.post, url, json=payload, headers=headers,
                    proxies=proxies, timeout=config.GROQ_TIMEOUT_SEC,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                if not (content or "").strip():
                    # Groq-модель иногда отдаёт пустой content без ошибки
                    if attempt < retries:
                        wait = min(2 ** attempt, 15)
                        log.warning("Groq пустой ответ (попытка %s/%s), сон %ss",
                                    attempt, retries, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise RuntimeError("Groq вернул пустой ответ")
                _groq_next_call = time.monotonic() + GROQ_CALL_GAP_SEC
                return content
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ProxyError,
                    requests.exceptions.SSLError) as e:
                # Прокси/TLS может сброситься разово — сначала мгновенно дублируем
                # через curl (другой TLS-стек), затем пауза и повтор requests.
                content = await asyncio.to_thread(_post_curl_sync, url, headers, payload)
                if content is not None:
                    _groq_next_call = time.monotonic() + GROQ_CALL_GAP_SEC
                    return content
                if attempt < retries:
                    wait = min(2 ** attempt, 30)
                    log.warning("Groq сеть (попытка %s/%s): %s, сон %ss",
                                attempt, retries, type(e).__name__, wait)
                    await asyncio.sleep(wait)
                    continue
                raise
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429 and attempt < retries:
                    # Уважаем Retry-After: при лимите токенов/мин пауза может быть 15-60с
                    ra = str(e.response.headers.get("Retry-After")
                             or e.response.headers.get("retry-after") or "")
                    wait = int(ra) + 2 if ra.isdigit() else min(2 ** attempt, 60)
                    wait = min(wait, 90)
                    log.warning("Groq 429 (попытка %s/%s), сон %ss", attempt, retries, wait)
                    await asyncio.sleep(wait)
                    continue
                raise
        raise RuntimeError("Groq не ответил после всех попыток")


def _post_curl_sync(url: str, headers: dict, payload: dict, timeout: int | None = None) -> str | None:
    """Обходной путь через curl.exe и SOCKS-прокси — переживает сброс TLS/SSLEOF."""
    import subprocess, tempfile, os as _os

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = timeout or min(config.GROQ_TIMEOUT_SEC, 45)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
        proxy = config.GROQ_PROXY or "socks5h://127.0.0.1:10808"
        cmd = [
            "curl.exe", "-s", "--proxy", proxy,
            "-X", "POST", url,
            "-H", f"Authorization: Bearer {config.GROQ_API_KEY}",
            "-H", "Content-Type: application/json",
            "--data-binary", "@" + path,
            "--max-time", str(min(timeout, 60)),
            "-o", "-",
            "-w", "\n%{http_code}",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        out = (r.stdout or b"").rstrip()
        body, _, status_s = out.rpartition(b"\n")
        status = int(status_s) if status_s.isdigit() else 0
        if status == 200 and body:
            return body.decode("utf-8", errors="replace")
        if status == 429:
            log.warning("Groq curl 429")
        elif status:
            log.warning("Groq curl HTTP %s, rc=%s", status, r.returncode)
        return None
    except Exception as e:
        log.warning("Groq curl fallback сбой: %s", e)
        return None
    finally:
        try:
            _os.unlink(path)
        except OSError:
            pass


def _extract_json(text: str) -> dict:
    """Достать JSON-объект из ответа модели (обёрнут в thinking/fence).

    Среди всех `{...}` берём объект с наибольшим числом ключей: у qwen
    projects часто массив объектов, и «последний» `{` — это вложенный проект,
    а не итоговый профиль/скоринг."""
    text = text or ""
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    best: dict | None = None
    for m in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[m.start():])
        except Exception:
            continue
        if isinstance(data, dict) and (best is None or len(data) > len(best)):
            best = data
    if best is None:
        raise ValueError("JSON не найден в ответе модели")
    return best


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
        # Лимит Groq free ~8000 токенов/мин: сжимаем пробелы и режем текст,
        # иначе резюме не влезает и приходит 429
        compact = re.sub(r"[ \t]+", " ", text or "").strip()
        compact = re.sub(r"\n{3,}", "\n\n", compact)[:7000]
        for attempt in (1, 2):
            try:
                out = await _post_groq(system, compact, max_tokens=1200)
                data = _extract_json(out)
                skills = data.get("skills", [])
                if isinstance(skills, str):
                    skills = [s.strip() for s in skills.split(",") if s.strip()]
                projects = data.get("projects", [])
                if isinstance(projects, str):
                    projects = [p.strip() for p in projects.split(";") if p.strip()]
                profile = {
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
                # Мусорный/пустой ответ (например, вложенный объект проекта) —
                # уходим на повтор
                if not (profile["skills"] or profile["about"]):
                    raise ValueError("ответ модели пуст или неполон")
                return profile
            except Exception as e:
                if attempt == 1:
                    log.warning("Парсинг резюме не удался, повтор: %s", e)
                    await asyncio.sleep(3)
                    continue
                log.exception("Ошибка парсинга резюме")
                return {}