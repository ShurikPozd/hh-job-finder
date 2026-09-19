import asyncio
import logging
import re
import random
import time

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("hh_client")

BASE = "https://hh.ru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def format_salary(node_or_text) -> str:
    """Нормализация зарплаты из HTML-карточки в текст вида 'от 80 000 ₽'."""
    if not node_or_text:
        return "не указана"
    text = node_or_text
    if hasattr(node_or_text, "get_text"):
        text = node_or_text.get_text(" ", strip=True)
    text = (text or "").replace("\xa0", " ").strip()
    if not text:
        return "не указана"
    return text


def extract_inn(s: str) -> str | None:
    """Найти ИНН (10 или 12 цифр) в тексте."""
    m = re.search(r"(?<!\d)(\d{10}|\d{12})(?!\d)", s or "")
    return m.group(1) if m else None


def text_has_accreditation(text: str, markers: list[str] | None = None) -> bool:
    """Проверка упоминания аккредитации/отсрочки в тексте."""
    markers = markers or config.ACCREDITATION_MARKERS
    low = (text or "").lower()
    return any(mkr.lower() in low for mkr in markers)


def _text(el) -> str | None:
    if el is None:
        return None
    return el.get_text(" ", strip=True) or None


class HHClient:
    """HTML-клиент hh.ru (публичный API закрыт с апреля 2026 г.)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._lock = asyncio.Lock()
        self._last_req = 0.0

    async def _get(self, url: str, params: dict | None = None,
                   retries: int = 4) -> str:
        for attempt in range(1, retries + 1):
            wait = config.HH_RATE_LIMIT_DELAY - (time.monotonic() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            resp = await asyncio.to_thread(
                self.session.get, url, params=params, timeout=30
            )
            self._last_req = time.monotonic()

            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After", "5") or 5)
                log.warning("hh.ru 429, сон %ss (попытка %s/%s)", delay, attempt, retries)
                await asyncio.sleep(delay + random.uniform(1, 3))
                continue
            if resp.status_code in (403, 302):
                # антибот/редирект на проверку
                if attempt < retries:
                    log.warning("hh.ru %s, возможный антибот; попытка %s/%s",
                                resp.status_code, attempt, retries)
                    await asyncio.sleep(2 * attempt)
                    continue
            resp.raise_for_status()
            return resp.text
        raise requests.HTTPError(f"hh.ru недоступен после {retries} попыток")

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    # ---- Поиск по вакансиям (SERP) ----

    def _parse_serp(self, html: str) -> dict:
        soup = self._soup(html)
        # Пустой результат: hh.ru вместо 0 показывает блок рекомендаций на той
        # же странице — их нельзя считать результатами поиска («fullstack python»).
        h1 = soup.find("h1")
        if h1:
            title = (h1.get_text(" ", strip=True) or "").replace("\xa0", " ")
            if "ничего не найдено" in title or "не найдено" in title:
                return {"items": [], "found": 0}
        items = []
        for it in soup.select('[data-qa="vacancy-serp__vacancy"]'):
            a = it.select_one('a[data-qa="serp-item__title"]')
            url = a.get("href") if a else None
            m = re.search(r"/vacancy/(\d+)", url or "") if url else None
            vid = m.group(1) if m else None
            if not vid:
                continue
            emp_a = it.select_one('a[data-qa="vacancy-serp__vacancy-employer"]')
            emp = _text(emp_a) or _text(
                it.select_one('[data-qa="vacancy-serp__vacancy-employer"]'))
            sal_el = it.select_one('[data-qa="vacancy-serp__vacancy-compensation"]')
            addr = _text(it.select_one('[data-qa="vacancy-serp__vacancy-address"]'))
            exp_el = it.select_one(
                '[data-qa^="vacancy-serp__vacancy-work-experience"]')
            items.append({
                "id": vid,
                "url": url or f"{BASE}/vacancy/{vid}",
                "title": _text(a) or _text(it.select_one("span[data-qa='serp-item__title-text']")),
                "employer": emp,
                "employer_href": emp_a.get("href") if emp_a else None,
                "salary_text": format_salary(sal_el),
                "salary_el": sal_el,
                "address": addr,
                "experience": _text(exp_el),
            })
        return {"items": items, "found": len(items)}

    async def search(self, text: str, page: int = 0, per_page: int = 20,
                     area: int | None = None, experience: str | None = None,
                     schedule: str | None = None, period: int | None = None,
                     min_salary: int | None = None,
                     label: str | None = None) -> dict:
        params = {
            "text": text,
            "page": page,
            "per_page": per_page,
            "order_by": "publication_time",
            "employment": "full",
        }
        if area is not None:
            params["area"] = area
        if experience:
            params["experience"] = experience
        if schedule:
            params["schedule"] = schedule
        if period:
            params["search_period"] = period
        if min_salary:
            params["salary"] = min_salary
            params["only_with_salary"] = "true"
        if label:
            params["label"] = label
        html = await self._get(f"{BASE}/search/vacancy", params)
        return self._parse_serp(html)

    # ---- Страница вакансии ----

    def _parse_vacancy(self, html: str, vacancy_id: str) -> dict:
        soup = self._soup(html)
        title = _text(soup.select_one('[data-qa="vacancy-title"]'))
        desc_el = soup.select_one('[data-qa="vacancy-description"]')
        desc = None
        if desc_el:
            # сохраняем переносы абзацев/списков, а не схлопываем всё в одну строку
            for tag in desc_el.find_all(["p", "div", "li", "ul", "ol", "br"]):
                tag.insert_after("\n")
            desc = desc_el.get_text(" ", strip=True)
            desc = re.sub(r"[ \t]+", " ", desc).replace(" \n", "\n").replace("\n ", "\n")
            desc = re.sub(r"\n{3,}", "\n\n", desc).strip() or None
        emp_el = soup.select_one('a[data-qa="vacancy-company-name"]')
        salary = None
        for sel in ('[data-qa="vacancy-salary-compensation-type-net"]',
                    '[data-qa="vacancy-salary-compensation-type-gross"]',
                    '[data-qa="vacancy-salary-compensation-type"]'):
            el = soup.select_one(sel)
            if el:
                salary = format_salary(el)
                break
        acc_badge = soup.select_one(
            '[data-qa="employer-card-employer-it-accreditation"]')
        skills = [_text(s) for s in soup.select('[data-qa="skills-element"]')]
        skills = [s for s in skills if s]
        exp = _text(soup.select_one('[data-qa="vacancy-experience"]'))
        addr = _text(soup.select_one('[data-qa="vacancy-view-raw-address"]'))
        # работа на удалёнке/смешанно
        remote = None
        for sel in ('[data-qa="vacancy-view-employment-mode"]',
                    '[data-qa="job-format"]',
                    '[data-qa="vacancy-work-format"]'):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                remote = el.get_text(" ", strip=True)
                break
        return {
            "id": str(vacancy_id),
            "title": title,
            "description": desc,
            "employer": _text(emp_el) if emp_el else None,
            "employer_href": emp_el.get("href") if emp_el else None,
            "salary_text": salary,
            "address": addr,
            "experience": exp,
            "skills": skills,
            "remote": remote,
            "accredited_by_hh": bool(acc_badge),
            "accredited_text": _text(acc_badge),
        }

    async def get_vacancy(self, vacancy_id: str) -> dict:
        html = await self._get(f"{BASE}/vacancy/{vacancy_id}")
        return self._parse_vacancy(html, vacancy_id)

    async def get_employer(self, employer_id: str) -> dict:
        """Компания: ищем ИНН (только рядом с меткой «ИНН») и подтв. аккредитации."""
        html = await self._get(f"{BASE}/employer/{employer_id}")
        soup = self._soup(html)
        name = _text(soup.select_one('h1, [data-qa="employer-page-title"], '
                                     '[data-qa="company-header_title"]'))
        acc = soup.select_one('[data-qa="employer-card-employer-it-accreditation"]')
        inn = None
        m = re.search(r"ИНН[^\d]{0,12}(\d{10}|\d{12})", html)
        if m:
            inn = m.group(1)
        return {"id": employer_id, "name": name, "inn": inn,
                "accredited_by_hh": bool(acc)}