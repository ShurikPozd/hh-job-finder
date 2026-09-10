import json
import os
from datetime import datetime, timezone

import aiosqlite

DEFAULT_PROFILE = {
    "name": "",
    "title": "",
    "skills": [],
    "experience_years": 0,
    "languages": [],
    "education": "",
    "city": "",
    "salary_expectation": 0,
    "about": "",
    "projects": [],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._migrate()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _migrate(self):
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                profile TEXT NOT NULL DEFAULT '{}',
                profile_bank TEXT,
                keywords TEXT,
                experience TEXT,
                schedule TEXT,
                area INTEGER,
                min_salary INTEGER DEFAULT 0,
                match_threshold INTEGER DEFAULT 6,
                search_interval_hours REAL DEFAULT 3,
                notifications_enabled INTEGER DEFAULT 1,
                onboarding_done INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS vacancies (
                vacancy_id TEXT PRIMARY KEY,
                name TEXT,
                employer_name TEXT,
                employer_id TEXT,
                salary_from INTEGER,
                salary_to INTEGER,
                salary_currency TEXT,
                salary_text TEXT,
                experience_id TEXT,
                experience_name TEXT,
                description TEXT,
                key_skills TEXT,
                area_name TEXT,
                url TEXT,
                published_at TEXT,
                accredited_it INTEGER DEFAULT 0,
                accreditation_source TEXT,
                llm_score INTEGER,
                llm_summary TEXT,
                cover_letter TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_vacancies (
                user_id INTEGER,
                vacancy_id TEXT,
                status TEXT DEFAULT 'seen',  -- seen | responded | hidden
                first_seen TEXT,
                last_seen TEXT,
                PRIMARY KEY (user_id, vacancy_id)
            );

            CREATE TABLE IF NOT EXISTS hidden_employers (
                user_id INTEGER,
                employer_id TEXT,
                created_at TEXT,
                PRIMARY KEY (user_id, employer_id)
            );

            CREATE TABLE IF NOT EXISTS registry_cache (
                inn TEXT PRIMARY KEY,
                company_name TEXT,
                accredited INTEGER DEFAULT 0,
                checked_at TEXT
            );
            """
        )
        await self._conn.commit()

    # ================= users =================

    async def get_user(self, telegram_id: int) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_user(self, telegram_id: int, **fields):
        now = utcnow()
        existing = await self.get_user(telegram_id)
        if not existing:
            defaults = {
                "telegram_id": telegram_id,
                "profile": json.dumps(DEFAULT_PROFILE, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            }
            defaults.update(fields)
            cols = ", ".join(defaults.keys())
            marks = ", ".join("?" * len(defaults))
            await self._conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({marks})",
                tuple(defaults.values()),
            )
        else:
            sets = ", ".join(f"{k} = ?" for k in fields)
            await self._conn.execute(
                f"UPDATE users SET {sets}, updated_at = ? WHERE telegram_id = ?",
                (*fields.values(), now, telegram_id),
            )
        await self._conn.commit()

    async def all_users(self) -> list[dict]:
        cur = await self._conn.execute("SELECT * FROM users")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ================= vacancies =================

    async def upsert_vacancy(self, v: dict):
        keys = [
            "vacancy_id", "name", "employer_name", "employer_id",
            "salary_from", "salary_to", "salary_currency", "salary_text",
            "experience_id", "experience_name", "description", "key_skills",
            "area_name", "url", "published_at", "accredited_it",
            "accreditation_source", "llm_score", "llm_summary", "created_at",
        ]
        data = {k: v.get(k) for k in keys}
        if data["key_skills"] is not None and not isinstance(data["key_skills"], str):
            data["key_skills"] = json.dumps(data["key_skills"], ensure_ascii=False)
        if data["accreditation_source"] and not isinstance(data["accreditation_source"], str):
            data["accreditation_source"] = json.dumps(data["accreditation_source"], ensure_ascii=False)
        if not data["created_at"]:
            data["created_at"] = utcnow()
        cols = ", ".join(data.keys())
        marks = ", ".join("?" * len(data))
        update = ", ".join(f"{k} = excluded.{k}" for k in data)
        await self._conn.execute(
            f"INSERT INTO vacancies ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(vacancy_id) DO UPDATE SET {update}",
            tuple(data.values()),
        )
        await self._conn.commit()

    async def get_vacancy(self, vacancy_id: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM vacancies WHERE vacancy_id = ?", (vacancy_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_vacancy_score(self, vacancy_id: str, score: int, summary: str):
        await self._conn.execute(
            "UPDATE vacancies SET llm_score = ?, llm_summary = ? WHERE vacancy_id = ?",
            (score, summary, vacancy_id),
        )
        await self._conn.commit()

    async def update_vacancy_letter(self, vacancy_id: str, letter: str):
        await self._conn.execute(
            "UPDATE vacancies SET cover_letter = ? WHERE vacancy_id = ?",
            (letter, vacancy_id),
        )
        await self._conn.commit()

    # ================= seen / responded / hidden =================

    async def is_seen(self, user_id: int, vacancy_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM seen_vacancies WHERE user_id = ? AND vacancy_id = ?",
            (user_id, vacancy_id),
        )
        return await cur.fetchone() is not None

    async def mark_seen(self, user_id: int, vacancy_id: str, status: str = "seen"):
        cur = await self._conn.execute(
            "SELECT status FROM seen_vacancies WHERE user_id = ? AND vacancy_id = ?",
            (user_id, vacancy_id),
        )
        row = await cur.fetchone()
        now = utcnow()
        if row:
            new_status = row[0] if status == "seen" else status
            await self._conn.execute(
                "UPDATE seen_vacancies SET status = ?, last_seen = ? "
                "WHERE user_id = ? AND vacancy_id = ?",
                (new_status, now, user_id, vacancy_id),
            )
        else:
            await self._conn.execute(
                "INSERT INTO seen_vacancies (user_id, vacancy_id, status, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, vacancy_id, status, now, now),
            )
        await self._conn.commit()

    async def vacancy_status(self, user_id: int, vacancy_id: str) -> str | None:
        cur = await self._conn.execute(
            "SELECT status FROM seen_vacancies WHERE user_id = ? AND vacancy_id = ?",
            (user_id, vacancy_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def has_hidden_employer(self, user_id: int, employer_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM hidden_employers WHERE user_id = ? AND employer_id = ?",
            (user_id, employer_id),
        )
        return await cur.fetchone() is not None

    async def add_hidden_employer(self, user_id: int, employer_id: str):
        await self._conn.execute(
            "INSERT OR IGNORE INTO hidden_employers (user_id, employer_id, created_at) "
            "VALUES (?, ?, ?)",
            (user_id, employer_id, utcnow()),
        )
        await self._conn.commit()

    async def remove_hidden_employer(self, user_id: int, employer_id: str):
        await self._conn.execute(
            "DELETE FROM hidden_employers WHERE user_id = ? AND employer_id = ?",
            (user_id, employer_id),
        )
        await self._conn.commit()
        # вернуть вакансии этого работодателя в оборот
        await self._conn.execute(
            "UPDATE seen_vacancies SET status = 'seen' WHERE user_id = ? "
            "AND vacancy_id IN (SELECT v.vacancy_id FROM vacancies v "
            "WHERE v.employer_id = ?)",
            (user_id, employer_id),
        )
        await self._conn.commit()

    async def list_responded(self, user_id: int) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT v.*, s.status, s.last_seen FROM vacancies v "
            "JOIN seen_vacancies s ON s.vacancy_id = v.vacancy_id "
            "WHERE s.user_id = ? AND s.status = 'responded' "
            "ORDER BY s.last_seen DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_hidden_vacancies(self, user_id: int) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT v.*, s.status, s.last_seen FROM vacancies v "
            "JOIN seen_vacancies s ON s.vacancy_id = v.vacancy_id "
            "WHERE s.user_id = ? AND s.status = 'hidden' "
            "ORDER BY s.last_seen DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_hidden_employers(self, user_id: int) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT he.employer_id, v.employer_name, he.created_at "
            "FROM hidden_employers he "
            "LEFT JOIN (SELECT employer_id, employer_name FROM vacancies "
            "           GROUP BY employer_id) v ON v.employer_id = he.employer_id "
            "WHERE he.user_id = ? ORDER BY he.created_at DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def restore_hidden_vacancy(self, user_id: int, vacancy_id: str):
        await self._conn.execute(
            "UPDATE seen_vacancies SET status = 'seen' WHERE user_id = ? AND vacancy_id = ?",
            (user_id, vacancy_id),
        )
        await self._conn.commit()

    # ================= registry cache =================

    async def registry_lookup(self, inn: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM registry_cache WHERE inn = ?", (inn,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def registry_save(self, inn: str, company_name: str, accredited: bool):
        await self._conn.execute(
            "INSERT OR REPLACE INTO registry_cache (inn, company_name, accredited, checked_at) "
            "VALUES (?, ?, ?, ?)",
            (inn, company_name, 1 if accredited else 0, utcnow()),
        )
        await self._conn.commit()