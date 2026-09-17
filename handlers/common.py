import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from keyboards import MAIN_MENU_BUTTONS, main_keyboard

log = logging.getLogger("handlers.common")
router = Router()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Что дальше?", reply_markup=main_keyboard())


@router.message(StateFilter("*"), F.text.in_(MAIN_MENU_BUTTONS))
async def interrupt_menu(message: Message, state: FSMContext):
    """Кнопка меню в любой момент сбрасывает текущий ввод и выполняет действие."""
    await state.clear()
    text = message.text
    if text == "🔍 Поиск":
        from handlers.search import btn_search
        await btn_search(message)
    elif text == "💼 Вакансии":
        from handlers.misc import cmd_vacancies
        await cmd_vacancies(message)
    elif text == "⚙️ Настройки":
        from handlers.settings import cmd_settings
        await cmd_settings(message)
    elif text == "📊 Статус":
        from handlers.misc import cmd_status
        await cmd_status(message)
    else:
        from handlers.misc import cmd_help
        await cmd_help(message)


@router.message(StateFilter("*"), CommandStart())
async def interrupt_start(message: Message, state: FSMContext):
    await state.clear()
    from handlers.start import cmd_start
    await cmd_start(message, state)


@router.message(StateFilter("*"), Command("search"))
async def interrupt_search(message: Message, state: FSMContext):
    await state.clear()
    from handlers.search import cmd_search
    await cmd_search(message)


@router.message(StateFilter("*"), Command("vacancies"))
async def interrupt_vacancies(message: Message, state: FSMContext):
    await state.clear()
    from handlers.misc import cmd_vacancies
    await cmd_vacancies(message)


@router.message(StateFilter("*"), Command("settings"))
async def interrupt_settings(message: Message, state: FSMContext):
    await state.clear()
    from handlers.settings import cmd_settings
    await cmd_settings(message)


@router.message(StateFilter("*"), Command("status"))
async def interrupt_status(message: Message, state: FSMContext):
    await state.clear()
    from handlers.misc import cmd_status
    await cmd_status(message)


@router.message(StateFilter("*"), Command("help"))
async def interrupt_help(message: Message, state: FSMContext):
    await state.clear()
    from handlers.misc import cmd_help
    await cmd_help(message)