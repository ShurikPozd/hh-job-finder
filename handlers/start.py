import json
import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import DEFAULT_PROFILE
from resume_parser import extract_text
import config

log = logging.getLogger("handlers.start")
router = Router()


class Onboarding(StatesGroup):
    waiting_resume = State()
    waiting_manual = State()
    waiting_bank_file = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        await message.answer("⛔️ Бот доступен только владельцу.")
        return

    user = await router.obj.db.get_user(message.from_user.id)
    if user and user.get("onboarding_done"):
        await message.answer(
            "Привет! Ты уже настроен. Команды:\n"
            "/search — ручной поиск сейчас\n"
            "/settings — настройки\n"
            "/status — статус\n"
            "/help — помощь"
        )
        return

    await message.answer(
        "Привет! Это бот поиска вакансий hh.ru с проверкой IT-аккредитации.\n\n"
        "Давай настроим твой профиль за пару минут.\n\n"
        "Вариант 1: прикрепи файл резюме (.txt, .docx, .pdf, .doc).\n"
        "Вариант 2: заполни вручную.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 Загрузить резюме", callback_data="onb:resume"),
                InlineKeyboardButton(text="⌨️ Вручную", callback_data="onb:manual"),
            ],
            [InlineKeyboardButton(text="⏭ Пропустить (шаблон)", callback_data="onb:skip")],
        ]),
    )


@router.callback_query(F.data.startswith("onb:"))
async def onb_choice(call: CallbackQuery, state: FSMContext):
    await call.answer()
    choice = call.data.split(":")[1]
    if choice == "resume":
        await state.set_state(Onboarding.waiting_resume)
        await call.message.answer("Отправь файл резюме одним сообщением (.txt, .docx, .pdf, .doc):")
    elif choice == "manual":
        await state.set_state(Onboarding.waiting_manual)
        await call.message.answer(
            "Расскажи о себе кратко в одном сообщении:\n"
            "пример: «Начинающий Python-разработчик, знаю Python, Django, SQL, "
            "опыт 1 год, город Москва, хочу от 80к»"
        )
    elif choice == "skip":
        await _finish(call.message, call.from_user.id, DEFAULT_PROFILE, state)


async def _finish(msg, user_id, profile: dict, state: FSMContext):
    await router.obj.db.upsert_user(
        user_id,
        profile=json.dumps(profile, ensure_ascii=False),
        onboarding_done=1,
        notifications_enabled=1,
    )
    await state.clear()
    await msg.answer(
        "✅ Профиль готов!\n\n"
        "Бот каждые N часов ищет вакансии и присылает подходящие с кнопками.\n"
        "Настройки — /settings. Ручной поиск — /search.\n\n"
        "💡 Совет: в /settings загрузи свой банк профиля или сгенерируй новый "
        "— сопроводительные станут точнее."
    )
    await router.obj.maybe_try_search(user_id)


@router.message(Onboarding.waiting_resume)
async def onb_resume(message: Message, state: FSMContext):
    if not message.document:
        return
    doc = message.document
    ext = doc.file_name.split(".")[-1].lower()
    if ext not in ("txt", "docx", "pdf", "doc"):
        await message.answer("Поддерживаются: .txt, .docx, .pdf, .doc. Попробуй ещё раз.")
        return
    try:
        data = await message.bot.download(doc, destination=None)
        raw = data.getvalue() if hasattr(data, "getvalue") else data
        text = extract_text(doc.file_name, raw)
    except Exception as e:
        log.exception("Ошибка загрузки резюме")
        await message.answer("Не удалось прочитать файл. Попробуй другой или заполни вручную.")
        return
    if len(text) < 50:
        await message.answer("Файл пуст или не распознан. Попробуй другой формат (.txt лучше).")
        return
    printable = sum(1 for c in text if c.isprintable())
    if printable / max(len(text), 1) < 0.6:
        await message.answer("Не удалось прочитать файл (похоже на бинарный формат). "
                             "Сохрани как .txt или .docx и попробуй ещё раз.")
        return

    await message.answer("🤖 Обрабатываю резюме… (может занять ~20 сек)")
    profile = await router.obj.analyzer.parse_resume(text)
    if not profile:
        await message.answer("Не удалось извлечь данные. Введи вручную (/start → Вручную).")
        return
    if profile.get("experience_years", 0) == 0:
        profile["experience_years"] = 1  # пет-проекты засчитываются как ~1 год

    await _finish(message, message.from_user.id, profile, state)


@router.message(Onboarding.waiting_manual)
async def onb_manual(message: Message, state: FSMContext):
    text = message.text or ""
    profile = DEFAULT_PROFILE.copy()
    profile["about"] = text
    await _finish(message, message.from_user.id, profile, state)