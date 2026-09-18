import json
import logging
import re

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

import config
from analyzer import set_usage_source, reset_usage_source

log = logging.getLogger("handlers.letter")
router = Router()

LINK_RE = re.compile(r"(?:https?://)?(?:[a-z0-9.-]+\.)?hh\.ru/vacancy/(\d+)")


def extract_vacancy_id(text: str) -> str | None:
    """Достать id вакансии из ссылки hh.ru (разные форматы с query-параметрами)."""
    m = LINK_RE.search(text or "")
    return m.group(1) if m else None


@router.message(StateFilter(None), F.text)
async def auto_letter(message: Message, state: FSMContext):
    """Любое сообщение со ссылкой на вакансию hh.ru → генерируем сопроводительное."""
    vid = extract_vacancy_id(message.text)
    if not vid:
        return
    await _generate_letter(message, vid)


@router.message(Command("letter"))
async def cmd_letter(message: Message, state: FSMContext):
    text = message.text or ""
    parts = text.split()
    vid = None
    if len(parts) >= 2:
        vid = extract_vacancy_id(parts[1])
    if not vid:
        await message.answer(
            "📄 Сопроводительное по ссылке.\n"
            "Пришли ссылку на вакансию hh.ru (например /letter https://hh.ru/vacancy/123456) "
            "или просто скинь ссылку на вакансию."
        )
        return
    await _generate_letter(message, vid)


async def _generate_letter(message: Message, vid: str):
    user_id = message.from_user.id
    if config.OWNER_ID and user_id != config.OWNER_ID:
        await message.answer("⛔️ Бот доступен только владельцу.")
        return
    user = await router.obj.db.get_user(user_id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    try:
        detail = await router.obj.service.hh.get_vacancy(vid)
    except Exception as e:
        log.warning("Не загружена вакансия %s: %s", vid, e)
        await message.answer("❌ Не удалось загрузить вакансию — она могла быть удалена или закрыта.")
        return
    rec = await router.obj.service._collect_record({}, detail)

    profile = json.loads(user.get("profile") or "{}")
    bank = user.get("profile_bank") or None

    status = await message.answer("✍️ Генерирую сопроводительное… (~20 сек)")
    token = set_usage_source("letter")
    try:
        letter = await router.obj.analyzer.generate_cover_letter(rec, profile, bank)
    finally:
        reset_usage_source(token)

    if await router.obj.db.get_vacancy(vid):
        await router.obj.db.update_vacancy_letter(vid, letter)

    url = rec.get("url") or f"https://hh.ru/vacancy/{vid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть на hh.ru", url=url)],
    ])
    await status.edit_text(f"{letter}", reply_markup=kb, disable_web_page_preview=True)