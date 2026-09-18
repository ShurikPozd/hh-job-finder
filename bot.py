import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiohttp import web

import config
import cloud_backup
from db import Database
from service import VacancyService

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_handlers: list[logging.Handler] = [
    logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
]
if sys.stderr:  # под pythonw (скрытый запуск) консоли нет
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("bot")

ctx_router = Router()


class App:
    """Держит сервисы в доступе для handler'ов через router.obj."""

    def __init__(self):
        self.db = Database(config.DB_PATH)
        self.bot: Bot | None = None
        self.dispatcher = Dispatcher()
        self.service: VacancyService | None = None
        self._search_locks: dict[int, asyncio.Lock] = {}
        self.analyzer = None

    def backup_now(self):
        """Пуш бэкапа сразу (например после онбординга), отложенно."""
        async def go():
            try:
                await cloud_backup.push_backup(self.db)
            except Exception as e:
                log.warning("backup_now: %s", e)
        asyncio.create_task(go())

    async def maybe_try_search(self, user_id: int):
        """Первый поиск сразу после онбординга (отложенно)."""
        async def go():
            await asyncio.sleep(3)
            try:
                await self.service.run_search(user_id, silent=False)
            except Exception as e:
                log.warning("Первый поиск user=%s: %s", user_id, e)
        asyncio.create_task(go())

    def is_searching(self, user_id: int) -> bool:
        lock = self._search_locks.get(user_id)
        return lock is not None and lock.locked()

    async def search_lock(self, user_id: int):
        lock = self._search_locks.setdefault(user_id, asyncio.Lock())
        return lock

    async def startup(self):
        await self.db.connect()
        # Таймаут поднят: скачивание файлов резюме из Telegram через SOCKS
        # идёт медленно, дефолта 60 сек не хватает (ловили TimeoutError)
        session = AiohttpSession(proxy=config.TG_PROXY, timeout=config.TG_API_TIMEOUT)
        self.bot = Bot(config.BOT_TOKEN, session=session)
        self.dispatcher["db"] = self.db
        self.dispatcher["bot"] = self.bot
        self.service = VacancyService(self.db, self.bot)
        self.analyzer = self.service.analyzer
        ctx_router.obj = self

        from handlers import common as h_common
        from handlers import start as h_start
        from handlers import search as h_search
        from handlers import letter as h_letter
        from handlers import settings as h_settings
        from handlers import callbacks as h_callbacks
        from handlers import misc as h_misc
        for r in (h_common.router, h_start.router, h_search.router, h_letter.router,
                  h_settings.router, h_callbacks.router, h_misc.router):
            r.obj = self
            self.dispatcher.include_router(r)

        who = await self.bot.get_me()
        log.info("Бот запущен: @%s", who.username)

        from aiogram.types import BotCommand
        await self.bot.set_my_commands([
            BotCommand(command="start", description="Приветствие и настройка"),
            BotCommand(command="search", description="Ручной поиск сейчас"),
            BotCommand(command="vacancies", description="Присланные вакансии"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="status", description="Статус и статистика"),
            BotCommand(command="letter", description="Сопроводительное по ссылке"),
            BotCommand(command="recheck", description="Перепроверка пропущенных"),
            BotCommand(command="help", description="Справка"),
        ])

        from scheduler import start_scheduler
        await start_scheduler(self.db, self.service)

        await self._load_bank_file()

        await self._restore_or_backup()

    async def _load_bank_file(self):
        """Автозагрузка банка профиля из файла (env BANK_FILE) в users.profile_bank."""
        if not config.BANK_FILE:
            return
        path = Path(config.BANK_FILE)
        if not path.is_file():
            log.warning("BANK_FILE не найден: %s", config.BANK_FILE)
            return
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Не прочитан BANK_FILE %s: %s", config.BANK_FILE, e)
            return
        loaded = text.strip()
        if not loaded:
            return
        owner = config.OWNER_ID
        if owner is not None:
            await self.db.upsert_user(owner, profile_bank=loaded)
            log.info("Банк профиля загружен из файла (%d символов)", len(loaded))

    async def _restore_or_backup(self):
        """Эфемерный диск Render: если БД пуста — восстановить из GitHub из бэкапа.
        Если есть данные, но бэкапа ещё нет — сделать первый push."""
        import cloud_backup as cb
        users = await self.db.all_users()
        if not users:
            res = await cb.restore_from_github(self.db)
            if res.get("users"):
                log.info("БД восстановлена из бэкапа: %s", res)
        else:
            asyncio.create_task(cb.push_backup(self.db))

    async def shutdown(self):
        if self.bot:
            await self.bot.session.close()
        await self.db.close()


app = App()
router_holder = ctx_router


# ===== Health check (Render: PORT) =====

async def handle_root(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "hh-job-finder"})


async def handle_healthz(request: web.Request) -> web.Response:
    if app.bot is None:
        return web.json_response({"ok": False}, status=503)
    connected = await app.bot.get_me()
    if connected:
        return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=503)


async def http_main():
    port = config.HTTP_PORT
    if not port:
        log.warning("HTTP_PORT=0, health check не запущен")
        return
    runner = web.AppRunner(_http_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP server на :%s", port)
    while True:
        await asyncio.sleep(3600)


def _http_app():
    webapp = web.Application()
    webapp.add_routes([web.get("/", handle_root), web.get("/healthz", handle_healthz)])
    return webapp


async def main():
    await app.startup()
    backup_task = asyncio.create_task(cloud_backup.backup_loop(app.db))
    recheck_task = None
    if config.RECHECK_ENABLED:
        from recheck import recheck_loop
        recheck_task = asyncio.create_task(recheck_loop(app.service))
    try:
        await asyncio.gather(
            app.dispatcher.start_polling(app.bot),
            http_main(),
        )
    finally:
        backup_task.cancel()
        if recheck_task:
            recheck_task.cancel()
        try:
            await backup_task
            if recheck_task:
                await recheck_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        log.exception("Критическая ошибка: %s", e)
        sys.exit(1)