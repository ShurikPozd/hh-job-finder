import json
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
from db import DEFAULT_PROFILE
from keyboards import settings_keyboard, bank_keyboard, cancel_keyboard
from resume_parser import parse_resume_document

log = logging.getLogger("handlers.settings")
router = Router()


class SettingsFSM(StatesGroup):
    enter_keywords = State()
    enter_experience = State()
    enter_schedule = State()
    enter_salary = State()
    enter_threshold = State()
    enter_interval = State()
    enter_bank_file = State()
    enter_profile_text = State()
    waiting_resume_file = State()


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await message.answer("⚙️ Настройки:", reply_markup=settings_keyboard())


@router.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message):
    await cmd_settings(message)


@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("⚙️ Настройки:", reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("set:"))
async def cb_set(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":")[1]
    user_id = call.from_user.id
    user = await router.obj.db.get_user(user_id)

    if action == "keywords":
        await state.set_state(SettingsFSM.enter_keywords)
        cur = user.get("keywords") or ", ".join(config.HH_DEFAULT_KEYWORDS)
        await call.message.edit_text(
            f"Введи ключевые слова через запятую.\nТекущие: {cur}\n\n"
            "Например: python developer, junior python, qa automation",
            reply_markup=cancel_keyboard(),
        )
    elif action == "experience":
        opts = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="0 лет (noExperience)", callback_data="exp:noExperience")],
            [InlineKeyboardButton(text="1–3 года", callback_data="exp:between1And3")],
            [InlineKeyboardButton(text="3–6 лет", callback_data="exp:between3And6")],
            [InlineKeyboardButton(text="Нет ограничений", callback_data="exp:any")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="settings")],
        ])
        await call.message.edit_text("🎯 Требуемый опыт:", reply_markup=opts)
    elif action == "schedule":
        opts = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Только удалёнка", callback_data="sch:remote")],
            [InlineKeyboardButton(text="Гибрид/удалёнка", callback_data="sch:mixed")],
            [InlineKeyboardButton(text="Полный день", callback_data="sch:fullDay")],
            [InlineKeyboardButton(text="Любой", callback_data="sch:any")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="settings")],
        ])
        await call.message.edit_text("📍 График работы:", reply_markup=opts)
    elif action == "salary":
        await state.set_state(SettingsFSM.enter_salary)
        await call.message.edit_text(
            "💰 Минимальная зарплата (в рублях, только число):",
            reply_markup=cancel_keyboard(),
        )
    elif action == "threshold":
        await state.set_state(SettingsFSM.enter_threshold)
        await call.message.edit_text(
            "📊 Порог соответствия 0–10 (по умолчанию 6).\n"
            "Чем выше — тем строже фильтр.\nВведи число:",
            reply_markup=cancel_keyboard(),
        )
    elif action == "interval":
        await state.set_state(SettingsFSM.enter_interval)
        cur = user.get("search_interval_hours") or config.DEFAULT_SEARCH_INTERVAL_HOURS
        await call.message.edit_text(
            f"⏱ Интервал поиска (часы). Сейчас: {cur} ч.\n"
            f"Допустимо от {config.SEARCH_INTERVAL_MIN} до {config.SEARCH_INTERVAL_MAX}.\nВведи число:",
            reply_markup=cancel_keyboard(),
        )
    elif action == "notifications":
        new_val = 0 if user.get("notifications_enabled", 1) else 1
        await router.obj.db.upsert_user(user_id, notifications_enabled=new_val)
        status = "🔔 Вкл" if new_val else "🔕 Выкл"
        await call.message.edit_text(f"Уведомления: {status}", reply_markup=settings_keyboard())
    elif action == "accreditation":
        new_val = 0 if user.get("only_accredited", 1) else 1
        await router.obj.db.upsert_user(user_id, only_accredited=new_val)
        status = "✅ Только с аккредитацией" if new_val else "❌ Любые (без фильтра)"
        await call.message.edit_text(f"Фильтр аккредитации: {status}", reply_markup=settings_keyboard())
    elif action == "bank":
        await call.message.edit_text("📁 Банк профиля:", reply_markup=bank_keyboard())
    elif action == "vacancies":
        from handlers.misc import show_vacancy_list
        await show_vacancy_list(call, 0, "date", "all", edit=True)
        await call.answer()
        return
    elif action == "hidden":
        await _list_hidden(call)
    elif action == "hiddenemp":
        await _list_hidden_emp(call)
    elif action == "responded":
        await _list_responded(call)
    elif action == "profile":
        cur = json.loads(user.get("profile") or "{}")
        await call.message.edit_text(
            "Текущий профиль:\n"
            f"👤 {cur.get('name') or '—'}\n"
            f"💼 {cur.get('title') or '—'}\n"
            f"🛠 Навыки: {', '.join(cur.get('skills') or [])}\n"
            f"📊 Опыт: {cur.get('experience_years')} лет\n"
            f"💰 Зарплата: {cur.get('salary_expectation')}\n\n"
            "Можно обновить резюме файлом или ввести текст вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Загрузить файл",
                                      callback_data="set:profile_reupload")],
                [InlineKeyboardButton(text="✏️ Ввести вручную",
                                      callback_data="set:profile_edit")],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="settings")],
            ]),
        )
    elif action == "profile_reupload":
        await state.set_state(SettingsFSM.waiting_resume_file)
        await call.message.edit_text(
            "📎 Прикрепи файл резюме (.txt, .docx, .pdf, .doc):",
            reply_markup=cancel_keyboard(),
        )
    elif action == "profile_edit":
        await state.set_state(SettingsFSM.enter_profile_text)
        await call.message.edit_text(
            "Опиши профиль текстом (как при /start). Например: "
            "«Начинающий Python-разработчик, Python, Django, SQL, опыт 1 год, Москва, 80к»",
            reply_markup=cancel_keyboard(),
        )
    await call.answer()


async def _list_hidden(call: CallbackQuery):
    items = await router.obj.db.list_hidden_vacancies(call.from_user.id)
    if not items:
        await call.message.edit_text("Нет скрытых вакансий.", reply_markup=settings_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↩️ {i.get('name') or i['vacancy_id']}",
                              callback_data=f"restore_v:{i['vacancy_id']}")]
        for i in items[:10]
    ] + [[InlineKeyboardButton(text="↩️ Назад", callback_data="settings")]])
    await call.message.edit_text("🚫 Скрытые вакансии:\n(нажми, чтобы вернуть в поиск)", reply_markup=kb)


async def _list_hidden_emp(call: CallbackQuery):
    items = await router.obj.db.list_hidden_employers(call.from_user.id)
    if not items:
        await call.message.edit_text("Нет скрытых работодателей.", reply_markup=settings_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↩️ {i.get('employer_name') or i['employer_id']}",
                              callback_data=f"restore_e:{i['employer_id']}")]
        for i in items[:10]
    ] + [[InlineKeyboardButton(text="↩️ Назад", callback_data="settings")]])
    await call.message.edit_text("🏢 Скрытые работодатели:", reply_markup=kb)


async def _list_responded(call: CallbackQuery):
    items = await router.obj.db.list_responded(call.from_user.id)
    if not items:
        await call.message.edit_text("Пока нет вакансий, где ты откликнулся.", reply_markup=settings_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔄 {i.get('name') or i['vacancy_id']}",
                              callback_data=f"restore_v:{i['vacancy_id']}")]
        for i in items[:10]
    ] + [[InlineKeyboardButton(text="↩️ Назад", callback_data="settings")]])
    await call.message.edit_text("✅ Вакансии, где откликнулся:\n(нажми, чтобы вернуть в поиск)", reply_markup=kb)


@router.callback_query(F.data.startswith("exp:"))
async def cb_exp(call: CallbackQuery):
    val = call.data.split(":")[1]
    if val == "any":
        new = ""
    else:
        new = val
    await router.obj.db.upsert_user(call.from_user.id, experience=new)
    await call.answer(f"Опыт: {val}", show_alert=False)
    await call.message.edit_text("✅ Сохранено.", reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("sch:"))
async def cb_sch(call: CallbackQuery):
    val = call.data.split(":")[1]
    mapping = {
        "remote": "remote",
        "mixed": "fullDay,remote",
        "fullDay": "fullDay",
        "any": "",
    }
    await router.obj.db.upsert_user(call.from_user.id, schedule=mapping[val])
    await call.answer("График сохранён.")
    await call.message.edit_text("✅ Сохранено.", reply_markup=settings_keyboard())


@router.message(SettingsFSM.enter_keywords)
async def fsm_keywords(message: Message, state: FSMContext):
    await router.obj.db.upsert_user(message.from_user.id, keywords=message.text)
    await state.clear()
    await message.answer("✅ Ключевые слова обновлены.", reply_markup=settings_keyboard())


@router.message(SettingsFSM.enter_salary)
async def fsm_salary(message: Message, state: FSMContext):
    await _save_int(message, state, "min_salary")


@router.message(SettingsFSM.enter_threshold)
async def fsm_threshold(message: Message, state: FSMContext):
    await _save_int(message, state, "match_threshold", 0, 10)


@router.message(SettingsFSM.enter_interval)
async def fsm_interval(message: Message, state: FSMContext):
    await _save_float(message, state, "search_interval_hours",
                      config.SEARCH_INTERVAL_MIN, config.SEARCH_INTERVAL_MAX)


@router.message(SettingsFSM.enter_profile_text)
async def fsm_profile(message: Message, state: FSMContext):
    profile = await router.obj.analyzer.parse_resume(message.text)
    if not profile:
        profile = DEFAULT_PROFILE.copy()
        profile["about"] = message.text
        await message.answer(
            "⚠️ Не удалось распознать структуру — сохранил как текст. "
            "Лучше загрузи файл резюме (📄 Загрузить файл)."
        )
    else:
        if profile.get("experience_years", 0) == 0:
            profile["experience_years"] = 1
    await router.obj.db.upsert_user(message.from_user.id,
                                    profile=json.dumps(profile, ensure_ascii=False))
    await state.clear()
    await message.answer("✅ Профиль обновлён.", reply_markup=settings_keyboard())


@router.message(SettingsFSM.waiting_resume_file)
async def fsm_resume_file(message: Message, state: FSMContext):
    await message.answer("🤖 Обрабатываю резюме… (может занять ~20 сек)")
    profile, err = await parse_resume_document(message, router.obj.analyzer)
    if err:
        err_text = {
            "not_doc": "Отправь файл резюме (.txt, .docx, .pdf, .doc).",
            "ext": "Поддерживаются: .txt, .docx, .pdf, .doc. Попробуй ещё раз.",
            "read": "Не удалось прочитать файл. Попробуй другой формат.",
            "short": "Файл пуст или не распознан. Попробуй другой формат (.txt лучше).",
            "binary": "Не удалось прочитать файл (похоже на бинарный формат). "
                      "Сохрани как .txt или .docx и попробуй ещё раз.",
            "parse": "Не удалось извлечь данные. Введи текст вручную.",
        }
        await message.answer(err_text.get(err, "Ошибка. Попробуй ещё раз."))
        return
    await router.obj.db.upsert_user(message.from_user.id,
                                    profile=json.dumps(profile, ensure_ascii=False))
    await state.clear()
    await message.answer("✅ Резюме обновлено, профиль пересобран.",
                         reply_markup=settings_keyboard())


async def _save_int(message: Message, state: FSMContext, field: str, lo=None, hi=None):
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("Нужно число. Попробуй ещё раз:")
        return
    if lo is not None and hi is not None and not (lo <= val <= hi):
        await message.answer(f"Число должно быть от {lo} до {hi}. Попробуй ещё раз:")
        return
    await router.obj.db.upsert_user(message.from_user.id, **{field: val})
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=settings_keyboard())


async def _save_float(message: Message, state: FSMContext, field: str, lo, hi):
    try:
        val = float(message.text.replace(",", ".").strip())
    except ValueError:
        await message.answer("Нужно число. Попробуй ещё раз:")
        return
    if not (lo <= val <= hi):
        await message.answer(f"Число должно быть от {lo} до {hi}. Попробуй ещё раз:")
        return
    await router.obj.db.upsert_user(message.from_user.id, **{field: val})
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=settings_keyboard())