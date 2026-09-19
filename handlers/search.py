import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
from handlers.misc import show_vacancy_list
from keyboards import vacancy_keyboard, cancel_keyboard, score_list_keyboard

log = logging.getLogger("handlers.search")
router = Router()


class ScoreFSM(StatesGroup):
    enter = State()


def _result_text(result: dict) -> str:
    text = (
        f"🔍 Найдено: {result['found']} · новых для тебя: {result['new']} "
        f"({result['already_seen']} уже видел)\n"
        f"🤖 Оценено сейчас: {result['scored']} из {result['new']}\n"
        f"✅ Прошли порог {result['threshold']}/10: {result['sent']} "
        f"→ прислано {result['sent']}\n"
    )
    if result.get("deferred"):
        text += (f"⏳ В очереди: {result['deferred']} — "
                 f"дооценю при следующем поиске.\n")
    free = result.get("budget_day_free")
    if free is not None:
        text += (f"💾 Квота LLM на сегодня: свободно "
                 f"~{int(free):,} из {config.SCORE_TOKEN_BUDGET_PER_DAY:,} токенов\n")
    text += "Всё, что когда-либо присылал бот, — в /vacancies."
    return text


def _result_markup(result: dict) -> InlineKeyboardMarkup | None:
    if not result.get("deferred"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Дооценить очередь",
                              callback_data="search:more")],
    ])


@router.message(Command("search"))
async def cmd_search(message: Message):
    user_id = message.from_user.id
    user = await router.obj.db.get_user(user_id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    lock = await router.obj.service.search_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Поиск уже выполняется, подожди чуть-чуть.")
        return
    async with lock:
        await message.answer("🔍 Ищу вакансии… может занять 30–120 сек.")
        result = await router.obj.service.run_search(user_id, silent=False, top_n=5)
    await message.answer(_result_text(result), reply_markup=_result_markup(result))


@router.message(F.text == "🔍 Поиск")
async def btn_search(message: Message):
    await cmd_search(message)


@router.callback_query(F.data == "search:more")
async def cb_search_more(call: CallbackQuery):
    user_id = call.from_user.id
    lock = await router.obj.service.search_lock(user_id)
    if lock.locked():
        await call.answer("⏳ Поиск уже выполняется, подожди чуть-чуть.", show_alert=True)
        return
    await call.message.edit_text("⏳ Дооцениваю очередь…")
    try:
        async with lock:
            result = await router.obj.service.run_search(user_id, silent=False, top_n=5)
        await call.message.edit_text(_result_text(result),
                                     reply_markup=_result_markup(result))
    except Exception as e:
        log.exception("Дооценка user=%s: %s", user_id, e)
        await call.message.edit_text("❌ Ошибка при дооценке. Попробуй ещё раз: /search")
    await call.answer()


# ================= Просмотр вакансий с определённой оценкой =================

async def _score_prompt(who, state: FSMContext, threshold: int):
    text = ("🔎 Покажу уже оценённые вакансии с указанной оценкой (0–10).\n"
            f"Сейчас порог: {threshold}/10 — так можно глянуть «недобор».\n"
            "Введи оценку:")
    if hasattr(who, "message"):
        await who.message.edit_text(text, reply_markup=cancel_keyboard())
    else:
        await who.answer(text, reply_markup=cancel_keyboard())


@router.message(Command("score"))
async def cmd_score(message: Message, state: FSMContext):
    user = await router.obj.db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    threshold = user.get("match_threshold") or config.DEFAULT_MATCH_THRESHOLD
    await state.set_state(ScoreFSM.enter)
    await _score_prompt(message, state, threshold)


@router.callback_query(F.data == "vl_score")
async def cb_vl_score(call: CallbackQuery, state: FSMContext):
    user = await router.obj.db.get_user(call.from_user.id)
    threshold = (user.get("match_threshold") or config.DEFAULT_MATCH_THRESHOLD) if user else config.DEFAULT_MATCH_THRESHOLD
    await state.set_state(ScoreFSM.enter)
    await _score_prompt(call, state, threshold)


@router.message(ScoreFSM.enter, F.text)
async def fsm_score(message: Message, state: FSMContext):
    txt = message.text.strip().replace(",", ".")
    try:
        score = int(float(txt))
    except ValueError:
        await message.answer("Нужно целое число от 0 до 10.",
                             reply_markup=cancel_keyboard())
        return
    if score < 0 or score > 10:
        await message.answer("Оценка должна быть от 0 до 10.",
                             reply_markup=cancel_keyboard())
        return
    await state.clear()
    items = await router.obj.db.list_by_score(message.from_user.id, score, limit=15)
    if not items:
        await message.answer(
            f"🔎 Вакансий с оценкой {score} нет.\n"
            "Оценки сохраняются начиная с этого обновления — раньше "
            "ниже-пороговые не запоминались.\nПопробуй другое значение: /score")
        return
    await message.answer(
        f"🔎 Вакансии с оценкой {score}/10 ({len(items)} шт.):",
        reply_markup=score_list_keyboard(items))


@router.message(ScoreFSM.enter)
async def fsm_score_other(message: Message, state: FSMContext):
    await message.answer("Введи оценку числом (0–10) — или нажми «↩️ Отмена».",
                         reply_markup=cancel_keyboard())


@router.callback_query(F.data == "vl_back")
async def cb_vl_back(call: CallbackQuery):
    await show_vacancy_list(call, 0, "date", "all", edit=True)
    await call.answer()


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