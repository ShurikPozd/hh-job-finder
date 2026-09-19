import json
import logging
import re

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, BufferedInputFile)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
from db import DEFAULT_PROFILE, utcnow
from keyboards import (settings_keyboard, bank_keyboard, cancel_keyboard,
                       profile_keyboard, resume_sections_keyboard)
from resume_parser import parse_resume_document, pop_profile_note

log = logging.getLogger("handlers.settings")
router = Router()

EXPERIENCE_LABELS = [
    ("noExperience", "Нет опыта"),
    ("between1And3", "1–3 года"),
    ("between3And6", "3–6 лет"),
    ("moreThan6", "более 6 лет"),
]


def _exp_set(value: str) -> set[str]:
    return {v for v in (value or "").split(",") if v}


def _exp_joined(cur: set[str]) -> str:
    return ",".join(v for v, _ in EXPERIENCE_LABELS if v in cur)


def _exp_summary(cur: set[str]) -> str:
    if not cur:
        return "Нет ограничений (любой опыт)"
    return ", ".join(lbl for v, lbl in EXPERIENCE_LABELS if v in cur)


def _experience_kb(cur: set[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if v in cur else "▫️ ") + lbl,
            callback_data=f"exp:toggle:{v}")]
        for v, lbl in EXPERIENCE_LABELS
    ]
    rows.append([InlineKeyboardButton(text="❌ Нет ограничений", callback_data="exp:any")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        cur = _exp_set(user.get("experience"))
        await call.message.edit_text(
            "🎯 Требуемый опыт (отметь нужные):\n\n"
            "Выбрано: " + _exp_summary(cur),
            reply_markup=_experience_kb(cur))
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
            "📝 Профиль и резюме:\n"
            f"👤 {cur.get('name') or '—'} · {cur.get('title') or '—'}\n"
            f"🛠 Навыки: {', '.join(cur.get('skills') or [])}\n"
            f"📊 Опыт: {cur.get('experience_years')} лет\n"
            f"💰 Зарплата: {cur.get('salary_expectation')}\n\n"
            "Тут можно посмотреть, что понял бот, и сам текст резюме — "
            "или обновить профиль файлом / вручную.",
            reply_markup=profile_keyboard(),
        )
    elif action == "profile_full":
        cur = json.loads(user.get("profile") or "{}")
        lines = [
            "🧾 Профиль (что понял бот):\n",
            f"👤 {cur.get('name') or '—'}",
            f"💼 {cur.get('title') or '—'}",
            f"📍 Город: {cur.get('city') or '—'}",
            f"📊 Опыт: {cur.get('experience_years')} лет",
            f"💳 Ожидание по ЗП: {cur.get('salary_expectation') or '—'}",
            f"🌐 Языки: {', '.join(cur.get('languages') or []) or '—'}",
            f"🎓 Образование:\n{cur.get('education') or '—'}",
        ]
        if cur.get("projects"):
            lines.append("👷 Проекты:\n• " + "\n• ".join(cur.get("projects")))
        if cur.get("about"):
            lines.append(f"🧠 Обо мне:\n{cur.get('about')[:1200]}")
        if cur.get("skills"):
            lines.append("🛠 Навыки: " + ", ".join(cur.get("skills")))
        await _chunk_reply(call, "\n".join(lines), profile_keyboard())
    elif action == "resume_view":
        await _show_resume(call, user, as_file=False)
    elif action == "resume_download":
        await _show_resume(call, user, as_file=True)
    elif action == "reparse":
        resume = user.get("resume_text")
        if not resume:
            await call.message.edit_text(
                "Нет сохранённого текста резюме — загрузи файл.",
                reply_markup=profile_keyboard())
        else:
            await call.message.edit_text("🤖 Переразбираю резюме… (~20 сек)")
            profile = await router.obj.analyzer.parse_resume(resume)
            note = None
            if not profile:
                profile = json.loads(user.get("profile") or "{}")
                note = "⚠️ Не удалось распарсить заново — профиль остался прежним."
            else:
                note = pop_profile_note(profile)
                if profile.get("experience_years", 0) == 0:
                    profile["experience_years"] = 1
            await router.obj.db.upsert_user(
                user_id, profile=json.dumps(profile, ensure_ascii=False))
            text = "✅ Профиль пересобран из сохранённого текста резюме."
            if note:
                text += "\n\n" + note
            await call.message.answer(text, reply_markup=profile_keyboard())
    elif action == "profile_reupload":
        await state.set_state(SettingsFSM.waiting_resume_file)
        await call.message.edit_text(
            "📎 Прикрепи файл резюме (.txt, .docx, .pdf, .doc, .rtf):",
            reply_markup=cancel_keyboard(),
        )
    elif action == "profile_edit":
        await state.set_state(SettingsFSM.enter_profile_text)
        await call.message.edit_text(
            "Опиши профиль текстом (как при /start) — можно кратко: "
            "«Начинающий Python-разработчик, Python, Django, SQL, опыт 1 год, Москва, 80к» "
            "— или вставь текст резюме (до 4096 символов). Для полного резюме лучше файл.",
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


@router.callback_query(F.data.regexp(r"^exp:(any|noExperience|between1And3|between3And6|moreThan6)$"))
async def cb_exp(call: CallbackQuery):
    val = call.data.split(":")[1]
    new = "" if val == "any" else val
    await router.obj.db.upsert_user(call.from_user.id, experience=new)
    cur = _exp_set(new)
    await call.message.edit_text(
        "🎯 Требуемый опыт (отметь нужные):\n\n"
        "Выбрано: " + _exp_summary(cur),
        reply_markup=_experience_kb(cur))
    await call.answer()


@router.callback_query(F.data.startswith("exp:toggle:"))
async def cb_exp_toggle(call: CallbackQuery):
    val = call.data.split(":")[2]
    user = await router.obj.db.get_user(call.from_user.id)
    cur = _exp_set(user.get("experience"))
    if val in cur:
        cur.discard(val)
    else:
        cur.add(val)
    await router.obj.db.upsert_user(call.from_user.id, experience=_exp_joined(cur))
    await call.message.edit_text(
        "🎯 Требуемый опыт (отметь нужные):\n\n"
        "Выбрано: " + _exp_summary(cur),
        reply_markup=_experience_kb(cur))
    await call.answer()


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


@router.message(SettingsFSM.enter_keywords, F.text)
async def fsm_keywords(message: Message, state: FSMContext):
    await router.obj.db.upsert_user(message.from_user.id, keywords=message.text)
    await state.clear()
    await message.answer("✅ Ключевые слова обновлены.", reply_markup=settings_keyboard())


@router.message(SettingsFSM.enter_keywords)
async def fsm_keywords_other(message: Message, state: FSMContext):
    await message.answer("Введи ключевые слова текстом — или нажми «↩️ Отмена».",
                         reply_markup=cancel_keyboard())


@router.message(SettingsFSM.enter_salary, F.text)
async def fsm_salary(message: Message, state: FSMContext):
    await _save_int(message, state, "min_salary")


@router.message(SettingsFSM.enter_threshold, F.text)
async def fsm_threshold(message: Message, state: FSMContext):
    await _save_int(message, state, "match_threshold", 0, 10)


@router.message(SettingsFSM.enter_interval, F.text)
async def fsm_interval(message: Message, state: FSMContext):
    await _save_float(message, state, "search_interval_hours",
                      config.SEARCH_INTERVAL_MIN, config.SEARCH_INTERVAL_MAX)


@router.message(SettingsFSM.enter_salary)
@router.message(SettingsFSM.enter_threshold)
@router.message(SettingsFSM.enter_interval)
async def fsm_number_other(message: Message, state: FSMContext):
    await message.answer("Нужно число — или нажми «↩️ Отмена».",
                         reply_markup=cancel_keyboard())


@router.message(SettingsFSM.enter_profile_text, F.text)
async def fsm_profile(message: Message, state: FSMContext):
    profile = await router.obj.analyzer.parse_resume(message.text)
    note = None
    if not profile:
        profile = DEFAULT_PROFILE.copy()
        profile["about"] = message.text
        note = ("⚠️ Не удалось распознать структуру — сохранил текст как есть. "
                "Лучше загрузи файл резюме (📄 Загрузить файл).")
    else:
        note = pop_profile_note(profile)
        if profile.get("experience_years", 0) == 0:
            profile["experience_years"] = 1
    await router.obj.db.upsert_user(message.from_user.id,
                                    profile=json.dumps(profile, ensure_ascii=False),
                                    resume_text=message.text[:50000],
                                    resume_filename=None,
                                    resume_at=utcnow())
    await state.clear()
    text = "✅ Профиль обновлён."
    if note:
        text += "\n\n" + note
    await message.answer(text, reply_markup=settings_keyboard())


@router.message(SettingsFSM.enter_profile_text)
async def fsm_profile_other(message: Message, state: FSMContext):
    await message.answer(
        "Напиши профиль текстом (до 4096 символов) — или нажми «↩️ Отмена».",
        reply_markup=cancel_keyboard(),
    )


@router.message(SettingsFSM.waiting_resume_file, F.document)
async def fsm_resume_file(message: Message, state: FSMContext):
    await message.answer("🤖 Обрабатываю резюме… (может занять 30–60 сек)")
    profile, err, text = await parse_resume_document(message, router.obj.analyzer)
    if err:
        err_text = {
            "not_doc": "Отправь файл резюме (.txt, .docx, .pdf, .doc).",
            "ext": "Поддерживаются: .txt, .docx, .pdf, .doc, .rtf. Попробуй ещё раз.",
            "read": "Не удалось прочитать файл. Попробуй другой формат.",
            "download": "Не удалось скачать файл из Telegram (сеть/прокси). "
                        "Попробуй ещё раз или пришли .txt.",
            "too_big": "Файл слишком большой. Ужми его или сохрани как .txt.",
            "short": "Файл пуст или не распознан. Попробуй другой формат (.txt лучше).",
            "binary": "Не удалось прочитать файл (похоже на бинарный формат). "
                      "Сохрани как .txt или .docx и попробуй ещё раз.",
            "parse": "Не удалось извлечь данные. Введи текст вручную.",
        }
        await message.answer(err_text.get(err, "Ошибка. Попробуй ещё раз."))
        return
    note = pop_profile_note(profile)
    await router.obj.db.upsert_user(message.from_user.id,
                                    profile=json.dumps(profile, ensure_ascii=False),
                                    resume_text=text[:50000],
                                    resume_filename=message.document.file_name,
                                    resume_at=utcnow())
    await state.clear()
    text = "✅ Резюме обновлено, профиль пересобран."
    if note:
        text += "\n\n" + note
    await message.answer(text, reply_markup=settings_keyboard())


@router.message(SettingsFSM.waiting_resume_file)
async def fsm_resume_file_other(message: Message, state: FSMContext):
    await message.answer(
        "Пришли файл резюме (.txt, .docx, .pdf, .doc, .rtf) — или нажми «↩️ Отмена».",
        reply_markup=cancel_keyboard(),
    )


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


RESUME_TOP_HEADERS = {
    "Желаемая должность и зарплата": "Желаемая должность и зарплата",
    "Образование": "Образование",
    "Электронные сертификаты": "Электронные сертификаты",
    "Навыки": "Навыки",
    "Дополнительная информация": "Дополнительная информация",
}


def _resume_header(line: str):
    """Заголовок топ-секции резюме hh.ru, если строка им является, иначе None."""
    low = line.rstrip("|").strip()
    if low in RESUME_TOP_HEADERS:
        return RESUME_TOP_HEADERS[low]
    if low.startswith("Опыт работы"):
        return "Опыт работы"
    return None


def _clean_section_body(body: str) -> str:
    """Убрать RTF-артефакты экспорта hh.ru: строки-пайпы и пайпы в конце строк."""
    body = re.sub(r"(?m)^\s*\|\s*$", "", body)
    body = re.sub(r"(?m)\|\s*$", "", body)
    body = re.sub(r"^\s*\|", "", body)
    return body.strip()


def split_resume_sections(text: str) -> list[tuple[str, str]]:
    """Резюме (экспорт hh.ru) → список (заголовок, тело).

    Шапка до первого заголовка + топ-секции; внутри «Опыт работы» каждый блок
    (строка начинается с '|') становится отдельным пунктом «Опыт: …», причём
    служебные строки до первого блока (заголовок опыта, дата, длительность)
    присоединяются к первому «Опыт:». Если ничего не распознано — один пункт
    «Резюме» целиком."""
    sections: list[tuple[str, str]] = []
    preamble: list[str] = []
    cur: list[str] = []
    cur_title = None
    in_exp = False
    exp_pending = False
    exp_pre: list[str] = []

    def flush():
        if cur_title is not None:
            sections.append((cur_title, _clean_section_body("\n".join(cur))))

    for raw in (text or "").splitlines():
        s = raw.strip()
        header = _resume_header(s)
        if header:
            flush()
            cur_title = None
            cur = []
            if header == "Опыт работы":
                in_exp = True
                exp_pending = True
                exp_pre = [s]
            else:
                in_exp = False
                exp_pending = False
                exp_pre = []
                cur_title = header
            continue
        if in_exp and s.startswith("|") and s != "|":
            name = re.sub(r"^Организация:\s*", "", s.lstrip("|").strip())
            if name:
                flush()
                cur_title = f"Опыт: {name[:40]}"
                cur = list(exp_pre)
                exp_pre = []
                exp_pending = False
                continue
        if exp_pending:
            exp_pre.append(raw)
        elif cur_title is None:
            preamble.append(raw)
        else:
            cur.append(raw)
    flush()

    if preamble:
        sections.insert(0, ("Шапка и контакты", _clean_section_body("\n".join(preamble))))
    if not sections:
        return [("Резюме", (text or "").strip())]
    return sections


def _resume_section_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К оглавлению", callback_data="resume:toc")],
    ])


async def _chunk_reply(call: CallbackQuery, text: str, kb):
    """Длинный текст: первый кусок — edit_text, остальные — новые сообщения."""
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
    for i, chunk in enumerate(chunks):
        if i == 0:
            await call.message.edit_text(chunk, reply_markup=kb)
        else:
            await call.message.answer(chunk, reply_markup=kb if i == len(chunks) - 1 else None)


async def _show_resume(call: CallbackQuery, user: dict, as_file: bool):
    resume = user.get("resume_text")
    if not resume:
        await call.message.edit_text(
            "Резюме не сохранено — загрузи файл или введи текст.",
            reply_markup=profile_keyboard())
        return
    if as_file:
        await call.message.answer_document(
            BufferedInputFile(resume.encode("utf-8"), filename="resume.txt"),
            caption="📄 Текст загруженного резюме")
        return
    header = f"📄 Резюме ({str(user.get('resume_at') or '')[:10]}):\n\n"
    sections = split_resume_sections(resume)
    if len(sections) > 1:
        await call.message.edit_text(
            f"📄 Резюме ({str(user.get('resume_at') or '')[:10]}) — выбери раздел:",
            reply_markup=resume_sections_keyboard(sections))
        return
    body = resume
    chunks = [header + body[i:i + 3800] for i in range(0, len(body), 3800)]
    for i, chunk in enumerate(chunks):
        if i == 0:
            await call.message.edit_text(chunk, reply_markup=profile_keyboard())
        else:
            await call.message.answer(
                chunk, reply_markup=profile_keyboard() if i == len(chunks) - 1 else None)


@router.callback_query(F.data.regexp(r"^resume:sec:\d+$"))
async def cb_resume_section(call: CallbackQuery):
    user = await router.obj.db.get_user(call.from_user.id)
    sections = split_resume_sections(user.get("resume_text") or "")
    idx = int(call.data.split(":")[2])
    if idx >= len(sections):
        await call.answer("Раздел не найден.", show_alert=True)
        return
    title, body = sections[idx]
    text = f"📄 {title}\n\n{body}"
    kb = _resume_section_kb()
    if len(text) <= 4096:
        await call.message.edit_text(text, reply_markup=kb)
    else:
        await call.message.edit_text(text[:4096])
        await call.message.answer(f"📄 {title} (продолжение)\n\n{text[4096:]}",
                                  reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "resume:toc")
async def cb_resume_toc(call: CallbackQuery):
    user = await router.obj.db.get_user(call.from_user.id)
    sections = split_resume_sections(user.get("resume_text") or "")
    if len(sections) > 1:
        await call.message.edit_text(
            f"📄 Резюме ({str(user.get('resume_at') or '')[:10]}) — выбери раздел:",
            reply_markup=resume_sections_keyboard(sections))
    else:
        await call.message.edit_text("📄 Резюме:", reply_markup=profile_keyboard())
    await call.answer()


@router.callback_query(F.data == "resume:toc_back")
async def cb_resume_toc_back(call: CallbackQuery):
    await call.message.edit_text(
        "📝 Профиль и резюме:", reply_markup=profile_keyboard())
    await call.answer()