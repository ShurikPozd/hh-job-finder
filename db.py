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
                only_accredited INTEGER DEFAULT 1,
                onboarding_done INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                resume_text TEXT,
                resume_filename TEXT,
                resume_at TEXT
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

            CREATE TABLE IF NOT EXISTS sent_messages (
                user_id INTEGER,
                vacancy_id TEXT,
                message_id INTEGER,
                sent_at TEXT,
                PRIMARY KEY (user_id, vacancy_id)
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                day TEXT,               -- UTC-дата YYYY-MM-DD
                model TEXT,
                source TEXT NOT NULL DEFAULT 'search',  -- search | recheck | letter | bank | parse
                calls INTEGER DEFAULT 0,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                PRIMARY KEY (day, model, source)
            );

            CREATE TABLE IF NOT EXISTS recheck_queue (
                user_id INTEGER,
                vacancy_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | sent | failed
                tries INTEGER NOT NULL DEFAULT 0,
                added_at TEXT,
                checked_at TEXT,
                PRIMARY KEY (user_id, vacancy_id)
            );
            """
        )
        await self._conn.commit()
        await self._ensure_column("users", "only_accredited",
                                  "INTEGER NOT NULL DEFAULT 1")
        await self._ensure_column("users", "last_search_at", "TEXT")
        await self._ensure_column("users", "resume_text", "TEXT")
        await self._ensure_column("users", "resume_filename", "TEXT")
        await self._ensure_column("users", "resume_at", "TEXT")
        await self._migrate_llm_usage_source()

    async def _migrate_llm_usage_source(self):
        """llm_usage раньше имел PRIMARY KEY (day, model): один счётчик на модель
        в день, без различия источника. Нужен PK (day, model, source), чтобы
        бюджет поиска и перепроверки считались раздельно."""
        await self._ensure_column("llm_usage", "source",
                                  "TEXT NOT NULL DEFAULT 'search'")
        cur = await self._conn.execute("PRAGMA index_list(llm_usage)")
        rows = await cur.fetchall()
        pk = None
        for r in rows:
            if r["unique"]:
                sub = await self._conn.execute(f"PRAGMA index_info({r['name']})")
                pk = [s["name"] for s in await sub.fetchall()]
                break
        if pk != ["day", "model", "source"]:
            await self._conn.executescript(
                """
                ALTER TABLE llm_usage RENAME TO llm_usage_old;
                CREATE TABLE llm_usage (
                    day TEXT,
                    model TEXT,
                    source TEXT NOT NULL DEFAULT 'search',
                    calls INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    PRIMARY KEY (day, model, source)
                );
                INSERT INTO llm_usage (day, model, source, calls,
                                       prompt_tokens, completion_tokens)
                    SELECT day, model, source, calls,
                           prompt_tokens, completion_tokens FROM llm_usage_old;
                DROP TABLE llm_usage_old;
                """
            )
            await self._conn.commit()

    async def _ensure_column(self, table: str, column: str, ddl: str):
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        cols = {r["name"] for r in rows}
        if column not in cols:
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
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

    # ================= sent_messages (история присланного) =================

    async def save_sent_message(self, user_id: int, vacancy_id: str, message_id: int):
        await self._conn.execute(
            "INSERT INTO sent_messages (user_id, vacancy_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, vacancy_id) DO UPDATE SET "
            "message_id = excluded.message_id, sent_at = excluded.sent_at",
            (user_id, vacancy_id, message_id, utcnow()),
        )
        await self._conn.commit()

    async def get_sent_message(self, user_id: int, vacancy_id: str) -> int | None:
        cur = await self._conn.execute(
            "SELECT message_id FROM sent_messages WHERE user_id = ? AND vacancy_id = ?",
            (user_id, vacancy_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def list_sent_vacancies(self, user_id: int, page: int = 0,
                                  page_size: int = 10, sort: str = "date",
                                  filter_: str = "all") -> dict:
        """Присланные вакансии (кроме скрытых) с пагинацией.
        sort: date | score. filter_: all | responded | pending."""
        where = ["s.user_id = ?"]
        params: list = [user_id]
        if filter_ == "responded":
            where.append("COALESCE(sv.status, '') = 'responded'")
        elif filter_ == "pending":
            where.append("COALESCE(sv.status, 'seen') = 'seen'")
        where.append("COALESCE(sv.status, 'seen') != 'hidden'")
        where_sql = " AND ".join(where)
        order = {
            "date": "s.sent_at DESC",
            "score": "v.llm_score DESC, s.sent_at DESC",
        }.get(sort, "s.sent_at DESC")
        base = (
            "FROM sent_messages s "
            "JOIN vacancies v ON v.vacancy_id = s.vacancy_id "
            "LEFT JOIN seen_vacancies sv "
            "ON sv.user_id = s.user_id AND sv.vacancy_id = s.vacancy_id "
            f"WHERE {where_sql}"
        )
        cur = await self._conn.execute(f"SELECT COUNT(*) {base}", params)
        total = (await cur.fetchone())[0]
        cur = await self._conn.execute(
            f"SELECT v.*, s.sent_at, COALESCE(sv.status, 'seen') AS status "
            f"{base} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [page_size, page * page_size],
        )
        rows = await cur.fetchall()
        return {"items": [dict(r) for r in rows], "total": total}

    async def sent_stats(self, user_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM sent_messages WHERE user_id = ?) AS sent, "
            "  (SELECT COUNT(*) FROM seen_vacancies WHERE user_id = ? "
            "    AND status = 'responded') AS responded, "
            "  (SELECT COUNT(*) FROM seen_vacancies WHERE user_id = ? "
            "    AND status = 'hidden') AS hidden",
            (user_id, user_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else {"sent": 0, "responded": 0, "hidden": 0}

    # ================= Учёт использования LLM =================

    async def add_llm_usage(self, model: str, prompt_tokens: int,
                            completion_tokens: int, source: str = "search"):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self._conn.execute(
            "INSERT INTO llm_usage (day, model, source, calls, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(day, model, source) DO UPDATE SET "
            "  calls = calls + 1, "
            "  prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
            "  completion_tokens = completion_tokens + excluded.completion_tokens",
            (day, model, source, int(prompt_tokens or 0), int(completion_tokens or 0)),
        )
        await self._conn.commit()

    async def llm_usage_today(self) -> dict:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = await self._conn.execute(
            "SELECT model, calls, prompt_tokens, completion_tokens FROM llm_usage "
            "WHERE day = ? ORDER BY model", (day,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        return {
            "day": day,
            "models": rows,
            "calls": sum(r["calls"] for r in rows),
            "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows),
        }

    async def llm_calls_today(self, models: list[str] | None = None,
                              source: str | None = None) -> int:
        """Сколько LLM-вызовов уже сделано сегодня (для бюджета прогона)."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cond = ["day = ?"]
        params: list = [day]
        if models:
            q = ",".join("?" * len(models))
            cond.append(f"model IN ({q})")
            params.extend(models)
        if source:
            cond.append("source = ?")
            params.append(source)
        cur = await self._conn.execute(
            f"SELECT COALESCE(SUM(calls), 0) c FROM llm_usage "
            f"WHERE {' AND '.join(cond)}", params)
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def llm_tokens_today(self, models: list[str] | None = None,
                               source: str | None = None) -> int:
        """Потрачено токенов (вход+выход) сегодня — основа бюджета скоринга.
        source: 'search' | 'recheck' | None (любой источник)."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cond = ["day = ?"]
        params: list = [day]
        if models:
            q = ",".join("?" * len(models))
            cond.append(f"model IN ({q})")
            params.extend(models)
        if source:
            cond.append("source = ?")
            params.append(source)
        cur = await self._conn.execute(
            f"SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) t "
            f"FROM llm_usage WHERE {' AND '.join(cond)}", params)
        row = await cur.fetchone()
        return int(row["t"]) if row else 0

    # ================= Очередь перепроверки пропущенных =================

    async def recheck_seed(self) -> int:
        """Сбросить в очередь все «увиденные, но не сохранённые» вакансии
        (seen без записи в vacancies). Идемпотентно: PK уже в очереди не дублирует.
        Возвращает число добавленных."""
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO recheck_queue (user_id, vacancy_id, added_at) "
            "SELECT s.user_id, s.vacancy_id, ? FROM seen_vacancies s "
            "WHERE s.status = 'seen' "
            "AND NOT EXISTS (SELECT 1 FROM vacancies v WHERE v.vacancy_id = s.vacancy_id)",
            (utcnow(),),
        )
        await self._conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    async def recheck_next(self, batch: int) -> list[dict]:
        """Следующие pending-вакансии, самые свежие по last_seen первыми."""
        cur = await self._conn.execute(
            "SELECT q.user_id, q.vacancy_id, "
            "(SELECT s.last_seen FROM seen_vacancies s "
            " WHERE s.user_id = q.user_id AND s.vacancy_id = q.vacancy_id) AS last_seen "
            "FROM recheck_queue q WHERE q.status = 'pending' "
            "ORDER BY last_seen DESC LIMIT ?",
            (batch,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def recheck_finish(self, user_id: int, vacancy_id: str, status: str):
        await self._conn.execute(
            "UPDATE recheck_queue SET status = ?, checked_at = ?, tries = tries + 1 "
            "WHERE user_id = ? AND vacancy_id = ?",
            (status, utcnow(), user_id, vacancy_id),
        )
        await self._conn.commit()

    async def recheck_fail(self, user_id: int, vacancy_id: str) -> bool:
        """Пометить ошибку. Возвращает True, если попытки исчерпаны (failed)."""
        cur = await self._conn.execute(
            "UPDATE recheck_queue SET tries = tries + 1, checked_at = ? "
            "WHERE user_id = ? AND vacancy_id = ? RETURNING tries",
            (utcnow(), user_id, vacancy_id),
        )
        row = await cur.fetchone()
        await self._conn.commit()
        tries = row[0] if row else 0
        if tries >= 3:
            await self._conn.execute(
                "UPDATE recheck_queue SET status = 'failed' "
                "WHERE user_id = ? AND vacancy_id = ? AND status = 'pending'",
                (user_id, vacancy_id),
            )
            await self._conn.commit()
            return True
        return False

    async def recheck_stats(self) -> dict:
        cur = await self._conn.execute(
            "SELECT status, COUNT(*) c FROM recheck_queue GROUP BY status")
        rows = {r["status"]: r["c"] for r in await cur.fetchall()}
        return {
            "pending": rows.get("pending", 0),
            "sent": rows.get("sent", 0),
            "done": rows.get("done", 0),
            "failed": rows.get("failed", 0),
        }

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

    # ================= Бэкап =================

    TABLES = ("users", "vacancies", "seen_vacancies", "hidden_employers", "registry_cache")

    async def export_json(self) -> dict:
        """Полный дамп всех таблиц для облачного бэкапа."""
        out = {"version": 1, "tables": {}}
        for table in self.TABLES:
            cur = await self._conn.execute(f"SELECT * FROM {table}")
            rows = await cur.fetchall()
            out["tables"][table] = [dict(r) for r in rows]
        return out

    async def import_json(self, data: dict) -> int:
        """Импорт дампа. Восстанавливает только если целевые таблицы пусты."""
        tables = (data or {}).get("tables") or {}
        restored = 0
        for table, rows in tables.items():
            if table not in self.TABLES or not isinstance(rows, list) or not rows:
                continue
            cur = await self._conn.execute(f"SELECT COUNT(*) FROM {table}")
            cnt = (await cur.fetchone())[0]
            if cnt > 0:
                continue
            cols = list(rows[0].keys())
            marks = ", ".join("?" * len(cols))
            for r in rows:
                await self._conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                    tuple(r.get(c) for c in cols),
                )
                restored += 1
        await self._conn.commit()
        return restored