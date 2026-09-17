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
_groq_next_call: dict[str, float] = {}
GROQ_CALL_GAP_SEC = 25.0  # qwen: ~8000 TPM — держим ~2-3 вызова/мин
GROQ_PARSE_CALL_GAP_SEC = 5.0  # compound: 70000 TPM — лимит не мешает

# Резюме бьём на части: ~8000 символов ≈ 2300 токенов. Тело запроса держим
# небольшим — крупные запросы Groq иногда отклоняет (413/429).
RESUME_CHUNK_CHARS = 8000
RESUME_MAX_CHUNKS = 3

# Куда писать расход токенов: async callable(model, prompt_tokens, completion_tokens)
_usage_hook = None


def set_usage_hook(fn):
    global _usage_hook
    _usage_hook = fn


async def _record_usage(model: str, usage: dict | None):
    if _usage_hook is None:
        return
    usage = usage or {}
    try:
        await _usage_hook(model, usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0))
    except Exception as e:  # учёт не должен ломать основную работу
        log.warning("Не записал llm_usage: %s", e)


async def _post_groq(system: str, user: str, temperature: float = 0.3,
                     retries: int = 5, max_tokens: int | None = None,
                     model: str | None = None, json_mode: bool = False) -> str:
    model = model or config.GROQ_MODEL
    gap = GROQ_PARSE_CALL_GAP_SEC if "compound" in model else GROQ_CALL_GAP_SEC
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    proxies = {"https": config.GROQ_PROXY, "http": config.GROQ_PROXY}
    if not config.GROQ_PROXY:
        proxies = None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens or config.GROQ_MAX_TOKENS,
    }
    if "compound" not in model:
        payload["reasoning_effort"] = "none"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with _groq_lock:
        delay = _groq_next_call.get(model, 0.0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(1, retries + 1):
            try:
                resp = await asyncio.to_thread(
                    requests.post, url, json=payload, headers=headers,
                    proxies=proxies, timeout=config.GROQ_TIMEOUT_SEC,
                )
                resp.raise_for_status()
                body = resp.json()
                choice = body["choices"][0]
                content = choice["message"]["content"]
                if json_mode and choice.get("finish_reason") == "length":
                    # JSON обрезан — модели не хватило max_tokens (часто из-за
                    # внутреннего reasoning). Повтор не поможет: уходим на резерв.
                    raise RuntimeError("Groq обрезал JSON (finish_reason=length)")
                if not (content or "").strip():
                    # Groq-модель иногда отдаёт пустой content без ошибки
                    if attempt < retries:
                        wait = min(2 ** attempt, 15)
                        log.warning("Groq пустой ответ (попытка %s/%s), сон %ss",
                                    attempt, retries, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise RuntimeError("Groq вернул пустой ответ")
                await _record_usage(model, body.get("usage"))
                _groq_next_call[model] = time.monotonic() + gap
                return content
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ProxyError,
                    requests.exceptions.SSLError) as e:
                # Прокси/TLS может сброситься разово — сначала мгновенно дублируем
                # через curl (другой TLS-стек), затем пауза и повтор requests.
                body = await asyncio.to_thread(_post_curl_sync, url, headers, payload)
                if body is not None:
                    choice = body["choices"][0]
                    content = choice["message"]["content"]
                    if json_mode and choice.get("finish_reason") == "length":
                        raise RuntimeError("Groq обрезал JSON (curl)")
                    if (content or "").strip():
                        await _record_usage(model, body.get("usage"))
                        _groq_next_call[model] = time.monotonic() + gap
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


def _post_curl_sync(url: str, headers: dict, payload: dict, timeout: int | None = None) -> dict | None:
    """Обходной путь через curl.exe и SOCKS-прокси — переживает сброс TLS/SSLEOF.
    Возвращает разобранный JSON-ответ (вместе с usage)."""
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
            try:
                return json.loads(body.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError) as e:
                log.warning("Groq curl: не разобрал JSON (%s)", e)
                return None
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


def _letter_text(text: str) -> str:
    """Текст сопроводительного. Просим модель писать обычным текстом, но если
    она всё же обернула его в JSON или ```-фенс — аккуратно достаём."""
    out = (text or "").strip()
    if out.startswith("{"):
        try:
            data = _extract_json(out)
            for key in ("letter", "cover_letter", "text", "body"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    out = val.strip()
                    break
        except ValueError:
            pass
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\s*", "", out)
        out = re.sub(r"\s*```$", "", out)
    return out.strip()


class Analyzer:
    async def score_vacancy(self, vacancy: dict, profile: dict) -> dict | None:
        """0-10 скоринг соответствия вакансии профилю.

        None — если оценить не удалось (сеть/квота). Вызывающий НЕ должен
        считать это «не подошло»: вакансию нужно переоценить в следующий раз."""
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
                # Профиль дублируется в каждом вызове — режем, чтобы влезать
                # в суточную free-квоту; описание вакансии не трогаем
                "skills": (profile.get("skills") or [])[:25],
                "experience_years": profile.get("experience_years", 0),
                "about": (profile.get("about") or "")[:600],
                "projects": profile.get("projects", []),
            },
        }, ensure_ascii=False)

        models: list[str] = []
        for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL):
            if m and m not in models:
                models.append(m)
        for model in models:
            try:
                out = await _post_groq(system, user,
                                       max_tokens=config.GROQ_SCORE_MAX_TOKENS,
                                       model=model)
                data = _extract_json(out)
                return {
                    "score": max(0, min(10, int(_num(data.get("score"))))),
                    "summary": data.get("summary", ""),
                    "missing_skills": data.get("missing_skills", []),
                    "matched_skills": data.get("matched_skills", []),
                    "risk": data.get("risk", "low"),
                }
            except Exception as e:
                log.warning("Скоринг моделью %s не удался: %s", model, e)
        log.error("Скоринг вакансии %s не удался всеми моделями",
                  vacancy.get("vacancy_id"))
        return None

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
        models: list[str] = []
        for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL):
            if m and m not in models:
                models.append(m)
        last_err = None
        for model in models:
            try:
                out = await _post_groq(system, user, 0.5,
                                       max_tokens=config.GROQ_LETTER_MAX_TOKENS,
                                       model=model)
                return _letter_text(out)
            except Exception as e:
                last_err = e
                log.warning("Письмо моделью %s не удалось: %s", model, e)
        log.exception("Ошибка генерации письма: %s", last_err)
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
            out = await _post_groq(system, user,
                                   max_tokens=config.GROQ_BANK_MAX_TOKENS)
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
        """Парсинг текста резюме в структурированный профиль.

        Длинный текст бьётся на части (лимит Groq free ~8000 токенов/мин),
        результаты частей объединяются — данные не теряются."""
        system = (
            "Ты — парсер резюме. Из текста резюме извлекаешь данные и "
            "возвращаешь строго JSON: "
            '{"name","title","skills":[],"experience_years","languages":[],'
            '"education","city","salary_expectation","about","projects":[]} '
            "Опыт указывай с округлением, например 1.5."
        )
        compact = re.sub(r"[ \t]+", " ", text or "").strip()
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        max_chars = RESUME_CHUNK_CHARS * RESUME_MAX_CHUNKS
        truncated = len(compact) > max_chars
        compact = compact[:max_chars]
        if not compact:
            return {}

        # Для каждой части: сначала модель парсинга (большой TPM), при сбое —
        # резервная. Так один капризный запрос не теряет часть резюме.
        models: list[tuple[str, int]] = []
        for m, mt in ((config.GROQ_PARSE_MODEL, 2000), (config.GROQ_MODEL, 1500)):
            if m and m not in [x[0] for x in models]:
                models.append((m, mt))

        n_chunks = max(1, -(-len(compact) // RESUME_CHUNK_CHARS))
        size = max(1, -(-len(compact) // n_chunks))
        chunks = [compact[i:i + size] for i in range(0, len(compact), size)]

        parts: list[dict] = []
        failed = 0
        used: list[str] = []
        for idx, chunk in enumerate(chunks, 1):
            for model, max_tokens in models:
                part = await self._parse_resume_chunk(
                    system, chunk, idx, len(chunks), model, max_tokens)
                if part:
                    parts.append(part)
                    used.append(model)
                    break
            else:
                failed += 1
        if not parts:
            log.error("Резюме не распарсилось (частей: %s)", len(chunks))
            return {}

        profile = _merge_profiles(parts)
        # Хвост/части могли не попасть — отдадим в мете для предупреждения
        profile["_meta"] = {
            "chunks": len(chunks),
            "failed_chunks": failed,
            "truncated": truncated,
            "chars": len(compact),
            "model": "+".join(sorted(set(used))),
        }
        return profile

    async def _parse_resume_chunk(self, system: str, chunk: str, idx: int,
                                  total: int, model: str,
                                  max_tokens: int) -> dict | None:
        """Распарсить одну часть резюме. None — если не удалось."""
        user = chunk if total == 1 else f"(часть {idx} из {total})\n{chunk}"
        try:
            out = await _post_groq(system, user, max_tokens=max_tokens,
                                   model=model, json_mode=True)
            data = _extract_json(out)
            skills = data.get("skills", [])
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.split(",") if s.strip()]
            projects = data.get("projects", [])
            if isinstance(projects, str):
                projects = [p.strip() for p in projects.split(";") if p.strip()]
            parsed = {
                "name": data.get("name", ""),
                "title": data.get("title", ""),
                "skills": skills,
                "experience_years": _num(data.get("experience_years")),
                "languages": data.get("languages", []),
                "education": data.get("education", ""),
                "city": data.get("city", ""),
                "salary_expectation": int(_num(data.get("salary_expectation"))),
                "about": data.get("about", ""),
                "projects": projects,
            }
            # Мусорный/пустой ответ (например, вложенный объект) — сбой части
            if not (parsed["skills"] or parsed["about"] or parsed["title"]):
                log.warning("Часть %s/%s резюме: ответ неполон", idx, total)
                return None
            return parsed
        except Exception as e:
            log.warning("Часть %s/%s резюме не распарсилась: %s", idx, total, e)
            return None


def _num(value, cast=float):
    """Число из ответа модели: 1.5, «1 год», «Не указано» — без падений."""
    if isinstance(value, (int, float)):
        return cast(value)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return cast(m.group(0).replace(",", ".")) if m else cast(0)


def _norm_key(value) -> str:
    """Ключ для дедупликации: регистр и варианты дефисов не важны."""
    s = str(value).lower()
    for ch in "\u2010\u2011\u2012\u2013\u2014":
        s = s.replace(ch, "-")
    return re.sub(r"\s+", " ", s).strip()


def _lang_name(raw: str) -> str:
    """«Russian – native»/«английский C1» → каноничное название языка."""
    k = _norm_key(raw)
    if "russian" in k or "русск" in k:
        return "Русский"
    if "english" in k or "англ" in k:
        return "Английский"
    return raw.strip()


def _merge_profiles(parts: list[dict]) -> dict:
    """Объединить профили, распарсенные по частям (без потери данных)."""
    def first(key: str):
        for p in parts:
            if p.get(key):
                return p[key]
        return ""

    def union(key: str) -> list:
        out: list = []
        seen: set = set()
        for p in parts:
            for item in (p.get(key) or []):
                val = item.strip() if isinstance(item, str) else item
                k = _norm_key(val)
                if val and k not in seen:
                    seen.add(k)
                    out.append(val)
        return out

    abouts: list[str] = []
    for p in parts:
        a = (p.get("about") or "").strip()
        if a and a not in abouts:
            abouts.append(a)
    skills = union("skills")
    skill_keys = {_norm_key(s) for s in skills}
    languages: list = []
    seen_lang: set = set()
    for p in parts:
        for lang in (p.get("languages") or []):
            if not isinstance(lang, str) or not lang.strip():
                continue
            k = _norm_key(lang)
            if k in skill_keys:
                continue  # модель сунула язык программирования в языки
            name = _lang_name(lang)
            if _norm_key(name) not in seen_lang:
                seen_lang.add(_norm_key(name))
                languages.append(name)
    return {
        "name": first("name"),
        "title": first("title"),
        "skills": skills,
        "experience_years": max(
            [_num(p.get("experience_years")) for p in parts] or [0]),
        "languages": languages,
        "education": first("education"),
        "city": first("city"),
        "salary_expectation": max(
            [int(_num(p.get("salary_expectation"))) for p in parts] or [0]),
        "about": " ".join(abouts),
        "projects": union("projects"),
    }