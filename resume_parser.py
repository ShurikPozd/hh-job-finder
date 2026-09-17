import io
import logging
import re
from pathlib import Path

log = logging.getLogger("resume_parser")


def extract_text(filename: str, data: bytes) -> str:
    """Извлечь текст из файла .txt/.docx/.pdf/.doc (fallback)."""
    name = (filename or "").lower()
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

    # Дальше — попытки выжать хоть что-то
    for enc in ("utf-8", "cp1251", "latin1"):
        try:
            text = data.decode(enc)
            if len(text) > 50:
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


def extract_phone(text: str) -> str | None:
    m = re.search(r"(\+?7\s?\(?\d{3}\)?\s?\d{3}[-\s]?\d{2}[-\s]?\d{2})", text)
    return m.group(1) if m else None


def extract_email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return m.group(0) if m else None


async def parse_resume_document(message, analyzer):
    """Скачивает и парсит файл резюме (.txt/.docx/.pdf/.doc).

    Возвращает (profile | None, error_tag | None). error_tag один из:
    not_doc | ext | read | short | binary | parse
    """
    if not message.document:
        return None, "not_doc"
    doc = message.document
    ext = (doc.file_name or "").split(".")[-1].lower()
    if ext not in ("txt", "docx", "pdf", "doc"):
        return None, "ext"
    try:
        data = await message.bot.download(doc, destination=None)
        raw = data.getvalue() if hasattr(data, "getvalue") else data
        text = extract_text(doc.file_name, raw)
    except Exception:
        log.exception("Ошибка загрузки резюме")
        return None, "read"
    if len(text) < 50:
        return None, "short"
    printable = sum(1 for c in text if c.isprintable())
    if printable / max(len(text), 1) < 0.6:
        return None, "binary"
    profile = await analyzer.parse_resume(text)
    if not profile:
        return None, "parse"
    if profile.get("experience_years", 0) == 0:
        profile["experience_years"] = 1  # пет-проекты засчитываются как ~1 год
    return profile, None