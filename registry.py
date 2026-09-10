"""Реестр аккредитованных IT-компаний Минцифры.

Первичный источник — hh.ru (employer.accredited_it_employer).
Здесь — вторичная проверка по ИНН через открытое API DataCrafter
(api.crftr.net) либо через CSV-выгрузку с data.gov.ru.
"""
import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta

import requests

import config
from db import Database

log = logging.getLogger("registry")

DATA_GOV_URL = (
    "https://data.gov.ru/opendata/7710474375-registergosaccred/"
    "data-20200115T1030-structure-20200113T1445.csv"
)

# Известные названия колонок реестра (разные редакции)
INN_COLUMNS = {"inn", "иин", "инн"}
NAME_COLUMNS = {"name", "orgname", "organizationname", "название",
                "наименование", "org", "fullname", "полное наименование"}


class RegistryChecker:
    def __init__(self, db: Database):
        self.db = db
        self._inn_cache: dict[str, bool] = {}

    async def check_inn(self, inn: str) -> tuple[bool, str]:
        """Вернуть (аккредитована, название компании), с TTL-обновлением."""
        if not inn:
            return False, ""

        cached = await self.db.registry_lookup(inn)
        if cached:
            return bool(cached["accredited"]), cached.get("company_name", "")

        accredited, company = False, ""
        if config.REGISTRY_URL:
            try:
                accredited, company = await self._query_apicrafter(inn)
            except Exception as e:
                log.warning("api.crftr.net не ответил: %s", e)
        if not accredited:
            accredited, company = await self._query_csv(inn)
        await self.db.registry_save(inn, company, accredited)
        self._inn_cache[inn] = accredited
        return accredited, company

    async def _query_apicrafter(self, inn: str) -> tuple[bool, str]:
        try:
            url = config.REGISTRY_URL
            resp = await asyncio.to_thread(_crftr_request, url, inn)
            total = resp.get("_meta", {}).get("total", 0)
            if total > 0:
                items = resp.get("_items", [])
                item = items[0] if items else {}
                name = item.get("name") or item.get("orgname") or ""
                return True, name
            return False, ""
        except Exception as e:
            log.warning("api.crftr.net не ответил (%s), пробую CSV", e)
            return await self._query_csv(inn)

    async def _query_csv(self, inn: str) -> tuple[bool, str]:
        try:
            resp = requests.get(DATA_GOV_URL, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.warning("CSV реестра недоступен: %s", e)
            return False, ""

        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            row_low = {k.lower(): v for k, v in row.items() if k}
            inn_val = next((v for k, v in row_low.items() if k in INN_COLUMNS), "")
            if inn_val and str(inn_val).strip() == inn:
                name = next((v for k, v in row_low.items() if k in NAME_COLUMNS), "")
                return True, name or ""
        return False, ""

    async def clear(self, inn: str):
        """Сбросить кеш по ИНН (например, при обновлении реестра)."""
        self._inn_cache.pop(inn, None)


def _crftr_request(url: str, inn: str) -> dict:
    params = {"where": json.dumps({"inn": inn})}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()