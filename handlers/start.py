import json
import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import DEFAULT_PROFILE
from resume_parser import parse_resume_document
from keyboards import main_keyboard
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
            "Привет! Ты уже настроен. Что делаем?\n\n"
            "Внизу появилась клавиатура — главные действия на ней.\n"
            "/search — ручной поиск сейчас\n"
            "/vacancies — уже присланные вакансии\n"
            "/settings — настройки\n"
            "/status — статус\n"
            "/help — помощь",
            reply_markup=main_keyboard(),
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
        "— сопроводительные станут точнее.",
        reply_markup=main_keyboard(),
    )
    await router.obj.maybe_try_search(user_id)
    router.obj.backup_now()


@router.message(Onboarding.waiting_resume)
async def onb_resume(message: Message, state: FSMContext):
    await message.answer("🤖 Обрабатываю резюме… (может занять ~20 сек)")
    profile, err = await parse_resume_document(message, router.obj.analyzer)
    if err:
        err_text = {
            "not_doc": "Отправь файл резюме (.txt, .docx, .pdf, .doc).",
            "ext": "Поддерживаются: .txt, .docx, .pdf, .doc, .rtf. Попробуй ещё раз.",
            "read": "Не удалось прочитать файл. Попробуй другой или заполни вручную.",
            "short": "Файл пуст или не распознан. Попробуй другой формат (.txt лучше).",
            "binary": "Не удалось прочитать файл (похоже на бинарный формат). "
                      "Сохрани как .txt или .docx и попробуй ещё раз.",
            "parse": "Не удалось извлечь данные. Введи вручную (/start → Вручную).",
        }
        await message.answer(err_text.get(err, "Ошибка. Попробуй ещё раз."))
        return

    await _finish(message, message.from_user.id, profile, state)


@router.message(Onboarding.waiting_manual)
async def onb_manual(message: Message, state: FSMContext):
    text = message.text or ""
    profile = DEFAULT_PROFILE.copy()
    profile["about"] = text
    await _finish(message, message.from_user.id, profile, state)