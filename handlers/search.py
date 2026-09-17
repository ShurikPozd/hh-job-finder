import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from keyboards import vacancy_keyboard

log = logging.getLogger("handlers.search")
router = Router()


@router.message(Command("search"))
async def cmd_search(message: Message):
    user_id = message.from_user.id
    user = await router.obj.db.get_user(user_id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    if not router.obj.is_searching(user_id):
        await message.answer("🔍 Ищу вакансии… может занять 30–120 сек.")
        result = await router.obj.service.run_search(user_id, silent=False, top_n=5)
        text = (
            f"🔍 Найдено вакансий: {result['found']}\n"
            f"🤖 Оценено: {result['scored']}\n"
            f"✅ Показано лучших: {result['sent']}\n"
        )
        if result.get("deferred"):
            text += (f"⏳ Отложено из-за лимита LLM: {result['deferred']} "
                     f"— оценю в следующий поиск.\n")
        if result["scored"] > result["sent"]:
            text += "Остальные — ниже порога соответствия.\n"
        text += "Всё, что когда-либо присылал бот, — в /vacancies."
        await message.answer(text)
    else:
        await message.answer("⏳ Поиск уже выполняется, подожди чуть-чуть.")


@router.message(F.text == "🔍 Поиск")
async def btn_search(message: Message):
    await cmd_search(message)


@router.message(F.text.startswith("/v "))
async def cmd_vacancy(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        return
    vid = parts[1].strip()
    rec = await router.obj.db.get_vacancy(vid)
    if not rec:
        await message.answer("Вакансия не найдена в базе. Используй /search.")
        return
    await message.answer(
        router.obj.service.format_vacancy(rec),
        reply_markup=vacancy_keyboard(vid),
        disable_web_page_preview=True,
    )