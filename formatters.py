import json
import re

import config


def parse_skills(raw) -> list[str]:
    """key_skills из БД: JSON-строка от db.upsert_vacancy, список, строка или None."""
    if not raw:
        return []
    if isinstance(raw, list):
        skills = raw
    else:
        try:
            skills = json.loads(raw)
        except (ValueError, TypeError):
            skills = [raw]
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    return [str(s).strip() for s in skills if str(s).strip()]


def skills_line(raw, limit: int = 12) -> str:
    skills = parse_skills(raw)
    if not skills:
        return "—"
    shown = skills[:limit]
    text = ", ".join(shown)
    if len(skills) > limit:
        text += f" +ещё {len(skills) - limit}"
    return text


def clean_html(html: str) -> str:
    """HTML hh.ru → текст с переносами по абзацам/спискам."""
    if not html:
        return ""
    html = re.sub(
        r"<\s*(/?)\s*(p|div|li|ul|ol|br)\s*>",
        lambda m: "\n" if m.group(2) == "br" else "\n",
        html,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("\xa0", " ")
    text = text.replace("\r", "")
    untangled = re.sub(r"[ \t]+", " ", text)
    untangled = re.sub(r" ?\n ?", "\n", untangled)
    lines = [ln.strip() for ln in untangled.split("\n")]
    out = []
    for ln in lines:
        if not ln:
            if out and out[-1]:
                out.append("")
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def truncate_words(text: str, limit: int = 1500) -> str:
    """Обрезка по границе слова, без разрыва посреди слова."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.5:
        cut = cut[:last_space]
    return cut.rstrip(" .,;:!?-") + "…"


def clean_description(desc, limit: int = 1500) -> str:
    return truncate_words(clean_html(desc or ""), limit)


def salary_text(rec: dict) -> str:
    raw = str(rec.get("salary_text") or "").replace("\xa0", " ").strip()
    if raw and raw.lower() not in ("зп", "зарплата", "не указана", "0"):
        return raw
    if rec.get("salary_from") or rec.get("salary_to"):
        frm, to = rec.get("salary_from"), rec.get("salary_to")
        cur = rec.get("salary_currency") or "RUR"
        sign = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get(cur, cur)
        if frm and to:
            return f"{frm}–{to} {sign}"
        if frm:
            return f"от {frm} {sign}"
        if to:
            return f"до {to} {sign}"
    return "не указана"


def accreditation_line(rec: dict) -> str:
    if rec.get("accredited_it"):
        line = "✅ IT-аккредитация"
    else:
        line = "❓ Аккредитация не найдена"
    if rec.get("accreditation_source"):
        line += f"\n   источники: {rec.get('accreditation_source')}"
    return line


def score_bar(rec: dict) -> str:
    score = int(rec.get("llm_score", 0) or 0)
    score = max(0, min(10, score))
    bar = "█" * score + "░" * (10 - score)
    return f"Соответствие: {bar} {score}/10"


def format_card(rec: dict) -> str:
    """Карточка вакансии, как её присылает бот уведомлением."""
    lines = [
        f"💼 {rec.get('name')}",
        f"🏢 {rec.get('employer_name') or '—'}",
        f"📍 {rec.get('area_name') or '—'} · {rec.get('experience_name') or '—'}",
        f"💰 {salary_text(rec)}",
        f"🛠 {skills_line(rec.get('key_skills'), limit=10)}",
        f"   {accreditation_line(rec)}",
        "",
        score_bar(rec),
    ]
    sd = rec.get("score_data") or {}
    if sd.get("summary"):
        lines.append(f"   {sd['summary']}")
    if sd.get("missing_skills"):
        lines.append(f"⚠️ Не хватает: {', '.join(sd['missing_skills'][:5])}")
    if sd.get("risk"):
        lines.append(f"🎯 Риск: {sd['risk']}")
    lines.append("")
    lines.append(f"🗓 Прогнозируемый {config.GROQ_MODEL.split('/')[-1]}")
    return "\n".join(lines)


def format_detail(rec: dict) -> str:
    """Подробное описание вакансии: карточка + описание + навыки + ссылка."""
    lines = [
        f"💼 {rec.get('name')}",
        f"🏢 {rec.get('employer_name') or '—'}",
        f"📍 {rec.get('area_name') or '—'} · {rec.get('experience_name') or '—'}",
        f"💰 {salary_text(rec)}",
        f"   {accreditation_line(rec)}",
        "",
    ]
    desc = clean_description(rec.get("description"), 1500)
    if desc:
        lines.append(f"📄 Описание:\n{desc}")
        lines.append("")
    lines.append(f"🛠 Навыки: {skills_line(rec.get('key_skills'), limit=15)}")
    url = rec.get("url") or (
        f"https://hh.ru/vacancy/{rec.get('vacancy_id')}" if rec.get("vacancy_id") else ""
    )
    if url:
        lines += ["", f"🌐 Ссылка: {url}"]
    return "\n".join(lines)