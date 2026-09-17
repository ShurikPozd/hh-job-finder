import asyncio
import io
import logging
import re
from pathlib import Path

log = logging.getLogger("resume_parser")

# Telegram Bot API: документы до 50 МБ; больше — бессмысленно тянуть по прокси
MAX_RESUME_SIZE = 50 * 1024 * 1024


def extract_text(filename: str, data: bytes) -> str:
    """Извлечь текст из файла .txt/.docx/.pdf/.doc/.rtf (fallback).

    RTF распознаётся по магии `{\\rtf` — покрывает .rtf и .doc,
    который на деле является RTF (старые экспорты Word)."""
    name = (filename or "").lower()
    if data[:8].lstrip().startswith(b"{\\rtf") or name.endswith(".rtf"):
        return _from_rtf(data)
    try:
        if name.endswith(".txt"):
            return data.decode("utf-8", errors="replace")
        if name.endswith(".docx"):
            return _from_docx(data)
        if name.endswith(".pdf"):
            return _from_pdf(data)
        if name.endswith(".doc"):
            return _from_docx(data)
    except Exception as e:
        log.warning("Не удалось распарсить %s: %s", filename, e)

    # Дальше — попытки выжать хоть что-то (только если похоже на резюме)
    for enc in ("utf-8", "cp1251", "latin1"):
        try:
            text = data.decode(enc)
            if len(text) > 50 and _cyrillic_ok(text):
                return text
        except UnicodeDecodeError:
            continue
    return ""


def _from_docx(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text for c in row.cells]
            parts.append(" | ".join(cells))
    return "\n".join(parts)


def _from_pdf(data: bytes) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def _from_rtf(data: bytes) -> str:
    """RTF → текст (кодировка из заголовка ansicpg, у парсера striprtf)."""
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception:
        rtf_to_text = None
    head = data[:400].decode("latin1", errors="ignore")
    cpg = re.search(r"\\ansicpg(\d+)", head)
    encoding = f"cp{cpg.group(1)}" if cpg else "cp1251"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        text = data.decode("latin1", errors="replace")
    if rtf_to_text:
        try:
            cleaned = rtf_to_text(text)
            if len(cleaned) > 50:
                return cleaned
        except Exception as e:
            log.warning("striprtf не справился: %s", e)
    return text


def _cyrillic_ok(text: str) -> bool:
    """Есть ли в тексте заметная доля кириллицы (резюме на русском)."""
    total = sum(1 for c in text if c.isprintable())
    cyr = sum(1 for c in text if "а" <= c.lower() <= "я" or c in "ёЁ")
    return total > 0 and cyr / total >= 0.05


def extract_phone(text: str) -> str | None:
    m = re.search(r"(\+?7\s?\(?\d{3}\)?\s?\d{3}[-\s]?\d{2}[-\s]?\d{2})", text)
    return m.group(1) if m else None


def extract_email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return m.group(0) if m else None


async def _download_with_retry(message, doc, attempts: int = 3) -> bytes:
    """Скачать файл из Telegram с ретраями. Скачивание по SOCKS-прокси из РФ
    нестабильно (ловили asyncio.TimeoutError на 31-й секунде)."""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            data = await message.bot.download(doc, destination=None)
            raw = data.getvalue() if hasattr(data, "getvalue") else data
            return bytes(raw)
        except Exception as e:
            last_exc = e
            log.warning("Скачивание %s, попытка %s/%s: %s",
                        doc.file_name, i, attempts, e)
            if i < attempts:
                await asyncio.sleep(2 * i)
    assert last_exc is not None
    raise last_exc


async def parse_resume_document(message, analyzer):
    """Скачивает и парсит файл резюме (.txt/.docx/.pdf/.doc).

    Возвращает (profile | None, error_tag | None, text | None), где text —
    распознанный текст файла (для сохранения и просмотра).
    error_tag один из: not_doc | ext | read | short | binary | parse
    | download | too_big
    """
    if not message.document:
        return None, "not_doc", None
    doc = message.document
    ext = (doc.file_name or "").split(".")[-1].lower()
    if ext not in ("txt", "docx", "pdf", "doc", "rtf"):
        return None, "ext", None
    if doc.file_size and doc.file_size > MAX_RESUME_SIZE:
        return None, "too_big", None
    try:
        raw = await _download_with_retry(message, doc)
    except Exception:
        log.exception("Не удалось скачать резюме %s", doc.file_name)
        return None, "download", None
    try:
        text = extract_text(doc.file_name, raw)
    except Exception:
        log.exception("Ошибка извлечения текста %s", doc.file_name)
        return None, "read", None
    if len(text) < 50:
        return None, "short", None
    printable = sum(1 for c in text if c.isprintable())
    if printable / max(len(text), 1) < 0.6:
        return None, "binary", None
    profile = await analyzer.parse_resume(text)
    if not profile:
        return None, "parse", None
    if profile.get("experience_years", 0) == 0:
        profile["experience_years"] = 1  # пет-проекты засчитываются как ~1 год
    return profile, None, text


def pop_profile_note(profile: dict) -> str | None:
    """Достать служебную мету парсинга (кол-во частей/обрезка) и вернуть
    предупреждение для пользователя. Мету удаляет, чтобы не ушла в БД."""
    meta = profile.pop("_meta", None) or {}
    notes = []
    if meta.get("truncated"):
        notes.append("⚠️ Резюме оказалось очень длинным — хвост не обработан. "
                     "Проверь профиль и дополни нужное вручную.")
    failed = meta.get("failed_chunks") or 0
    if failed:
        notes.append(f"⚠️ {failed} часть(и) резюме не удалось разобрать — "
                     "профиль может быть неполным.")
    elif (meta.get("chunks") or 1) > 1:
        notes.append("ℹ️ Резюме длинное — разобрал по частям, данные собраны целиком.")
    elif (meta.get("chars") or 0) > 8000:
        notes.append(f"ℹ️ Резюме длинное (~{meta['chars']} симв.) — "
                     "прочитал целиком, данные собраны полностью.")
    return "\n".join(notes) if notes else None