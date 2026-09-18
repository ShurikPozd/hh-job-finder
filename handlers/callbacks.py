import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from formatters import format_detail
from keyboards import (settings_keyboard, bank_keyboard, bank_sections_keyboard,
                       cancel_keyboard, vacancy_keyboard, hide_confirm_keyboard)
from analyzer import set_usage_source, reset_usage_source

log = logging.getLogger("handlers.callbacks")
router = Router()


class BankFSM(StatesGroup):
    waiting_bank_file = State()


# ================= Подробнее =================

@router.callback_query(F.data.startswith("detail:"))
async def cb_detail(call: CallbackQuery):
    vid = call.data.split(":")[1]
    rec = await router.obj.db.get_vacancy(vid)
    if not rec:
        await call.answer("Вакансия не найдена.", show_alert=True)
        return
    await call.message.edit_text(
        format_detail(rec),
        reply_markup=vacancy_keyboard(vid),
        disable_web_page_preview=True,
    )
    await call.answer()


# ================= Сопроводительное =================

@router.callback_query(F.data.startswith("letter:"))
async def cb_letter(call: CallbackQuery):
    vid = call.data.split(":")[1]
    rec = await router.obj.db.get_vacancy(vid)
    if not rec:
        await call.answer("Вакансия не найдена.", show_alert=True)
        return
    user = await router.obj.db.get_user(call.from_user.id)
    profile = user.get("profile") or "{}"
    import json as _json
    profile = _json.loads(profile)
    bank = user.get("profile_bank") or None
    await call.message.edit_text("✍️ Генерирую сопроводительное… (~20 сек)")

    # расход писать в source='letter' (раньше уходил в 'search')
    token = set_usage_source("letter")
    try:
        letter = await router.obj.analyzer.generate_cover_letter(rec, profile, bank)
    finally:
        reset_usage_source(token)
    await router.obj.db.update_vacancy_letter(vid, letter)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ К вакансии", callback_data=f"detail:{vid}")],
    ])
    await call.message.edit_text(f"✍️ Сопроводительное ({rec.get('name')}):\n\n{letter}", reply_markup=kb)
    await call.answer()


# ================= Откликнулся =================

@router.callback_query(F.data.startswith("respond:"))
async def cb_respond(call: CallbackQuery):
    vid = call.data.split(":")[1]
    await router.obj.db.mark_seen(call.from_user.id, vid, "responded")
    await call.answer("✅ Отмечено: откликнулся.", show_alert=True)
    await call.message.edit_text(
        "✅ Пометил «откликнулся». Вакансия не будет дублироваться.\n"
        "Вернуть в поиск можно в /settings → Откликнулся.",
        reply_markup=settings_keyboard(),
    )


# ================= Не интересно =================

@router.callback_query(F.data.startswith("hide:"))
async def cb_hide(call: CallbackQuery):
    vid = call.data.split(":")[1]
    rec = await router.obj.db.get_vacancy(vid)
    emp = rec.get("employer_name") if rec else "Компания"
    await call.message.edit_text(
        f"🚫 «{rec.get('name') if rec else vid}» ({emp})\n\nСкрыть:",
        reply_markup=hide_confirm_keyboard(vid, emp),
    )
    await call.answer()


@router.callback_query(F.data.startswith("hide_one:"))
async def cb_hide_one(call: CallbackQuery):
    vid = call.data.split(":")[1]
    await router.obj.db.mark_seen(call.from_user.id, vid, "hidden")
    await call.answer("Скрыл вакансию.", show_alert=True)
    await call.message.edit_text("🚫 Скрыл. Вернуть можно в /settings → Скрытые.", reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("hide_emp:"))
async def cb_hide_emp(call: CallbackQuery):
    vid = call.data.split(":")[1]
    rec = await router.obj.db.get_vacancy(vid)
    emp_id = rec.get("employer_id") if rec else ""
    await router.obj.db.add_hidden_employer(call.from_user.id, emp_id or "none")
    if rec:
        await router.obj.db.mark_seen(call.from_user.id, vid, "hidden")
    await call.answer(f"Скрыл все вакансии работодателя.", show_alert=True)
    await call.message.edit_text("🏢 Скрыл все вакансии работодателя. /settings → Скрытые работодатели.",
                                 reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("hide_cancel:"))
async def cb_hide_cancel(call: CallbackQuery):
    vid = call.data.split(":")[1]
    rec = await router.obj.db.get_vacancy(vid)
    if rec:
        await call.message.edit_text(
            router.obj.service.format_vacancy(rec),
            reply_markup=vacancy_keyboard(vid),
            disable_web_page_preview=True,
        )
    await call.answer("Отменено.")


# ================= restore =================

@router.callback_query(F.data.startswith("restore_v:"))
async def cb_restore_v(call: CallbackQuery):
    vid = call.data.split(":")[1]
    await router.obj.db.restore_hidden_vacancy(call.from_user.id, vid)
    await call.answer("Вернул в поиск.")
    await call.message.edit_text("✅ Вакансия возвращена в поиск.", reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("restore_e:"))
async def cb_restore_e(call: CallbackQuery):
    emp_id = call.data.split(":")[1]
    await router.obj.db.remove_hidden_employer(call.from_user.id, emp_id)
    await call.answer("Работодатель возвращён.")
    await call.message.edit_text("✅ Работодатель возвращён в поиск.", reply_markup=settings_keyboard())


# ================= Банк профиля =================

@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Отменено.", reply_markup=settings_keyboard())
    await call.answer()


@router.callback_query(F.data.regexp(r"^bank:(upload|generate|view|delete)$"))
async def cb_bank(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":")[1]
    user_id = call.from_user.id
    if action == "upload":
        await state.set_state(BankFSM.waiting_bank_file)
        await call.message.edit_text(
            "📎 Прикрепи файл .md банка профиля (или .txt).",
            reply_markup=cancel_keyboard(),
        )
    elif action == "generate":
        user = await router.obj.db.get_user(user_id)
        import json as _json
        profile = _json.loads(user.get("profile") or "{}")
        await call.message.edit_text("🤖 Генерирую банк… (~20 сек)")
        bank = await router.obj.analyzer.generate_bank(profile)
        await router.obj.db.upsert_user(user_id, profile_bank=bank)
        await call.message.edit_text(
            "✅ Банк сгенерирован и сохранён. Теперь сопроводительные будут персональными.",
            reply_markup=bank_keyboard(),
        )
    elif action == "view":
        user = await router.obj.db.get_user(user_id)
        bank = user.get("profile_bank")
        if not bank:
            await call.message.edit_text("Банк не загружен. Сгенерируй или загрузи.", reply_markup=bank_keyboard())
            return
        sections = split_bank_sections(bank)
        if len(sections) > 1:
            await call.message.edit_text(
                "📁 Банк профиля — выбери раздел:",
                reply_markup=bank_sections_keyboard(sections),
            )
        else:
            await _view_bank_plain(call, bank)
    elif action == "delete":
        await router.obj.db.upsert_user(user_id, profile_bank=None)
        await call.message.edit_text("🗑 Банк удалён. Использую универсальный шаблон.", reply_markup=bank_keyboard())
    await call.answer()


def split_bank_sections(bank: str) -> list[tuple[str, str]]:
    """Банк → список (заголовок, текст) по разделам '## '. Текст до первого
    '## ' показываем как раздел «Введение»."""
    sections: list[tuple[str, str]] = []
    cur_title: str | None = None
    cur: list[str] = []
    for line in (bank or "").splitlines():
        if line.startswith("## "):
            if cur_title is not None:
                sections.append((cur_title, "\n".join(cur).strip()))
            cur_title = line[3:].strip()
            cur = []
        else:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, "\n".join(cur).strip()))
    elif cur:
        sections.append(("Введение", "\n".join(cur).strip()))
    return sections


async def _view_bank_plain(call: CallbackQuery, bank: str):
    """Фолбэк: банк без разделов '## ' — показываем кусками по 3800."""
    header = "📁 Ваш банк профиля:\n\n"
    chunks = [header + bank[i:i + 3800] for i in range(0, len(bank), 3800)]
    chunks[0] = header + bank[:3800]
    for i, chunk in enumerate(chunks):
        kb = bank_keyboard() if i == len(chunks) - 1 else None
        if i == 0:
            await call.message.edit_text(chunk, reply_markup=kb)
        else:
            await call.message.answer(chunk, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^bank:sec:\d+$"))
async def cb_bank_section(call: CallbackQuery):
    idx = int(call.data.split(":")[2])
    user = await router.obj.db.get_user(call.from_user.id)
    sections = split_bank_sections(user.get("profile_bank") or "")
    if idx >= len(sections):
        await call.answer("Раздел не найден.", show_alert=True)
        return
    title, body = sections[idx]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К оглавлению", callback_data="bank:toc")],
    ])
    text = f"📁 {title}\n\n{body}"
    if len(text) <= 4096:
        await call.message.edit_text(text, reply_markup=kb)
    else:
        await call.message.edit_text(text[:4096])
        await call.message.answer(f"📁 {title} (продолжение)\n\n{text[4096:]}", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "bank:toc")
async def cb_bank_toc(call: CallbackQuery):
    user = await router.obj.db.get_user(call.from_user.id)
    sections = split_bank_sections(user.get("profile_bank") or "")
    if len(sections) > 1:
        await call.message.edit_text(
            "📁 Банк профиля — выбери раздел:",
            reply_markup=bank_sections_keyboard(sections),
        )
    else:
        await call.message.edit_text("📁 Банк профиля:", reply_markup=bank_keyboard())
    await call.answer()


@router.callback_query(F.data == "bank:toc_back")
async def cb_bank_toc_back(call: CallbackQuery):
    await call.message.edit_text("📁 Банк профиля:", reply_markup=bank_keyboard())
    await call.answer()


@router.message(BankFSM.waiting_bank_file, F.document)
async def fsm_bank_file(message, state: FSMContext):
    if not message.document:
        return
    doc = message.document
    if not (doc.file_name.endswith(".md") or doc.file_name.endswith(".txt")):
        await message.answer("Нужен файл .md или .txt.")
        return
    data = await message.bot.download(doc, destination=None)
    raw = data.getvalue() if hasattr(data, "getvalue") else data
    text = raw.decode("utf-8", errors="replace")
    await router.obj.db.upsert_user(message.from_user.id, profile_bank=text)
    await state.clear()
    await message.answer("✅ Банк загружен и сохранён.", reply_markup=settings_keyboard())


@router.message(BankFSM.waiting_bank_file)
async def fsm_bank_file_other(message, state: FSMContext):
    await message.answer("Пришли файл .md или .txt — или нажми «↩️ Отмена».",
                         reply_markup=cancel_keyboard())