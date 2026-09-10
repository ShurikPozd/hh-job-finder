from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo


def vacancy_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Подробнее", callback_data=f"detail:{vacancy_id}"),
            InlineKeyboardButton(text="✍️ Сопроводительное", callback_data=f"letter:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Откликнулся", callback_data=f"respond:{vacancy_id}"),
            InlineKeyboardButton(text="🚫 Не интересно", callback_data=f"hide:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="🌐 Открыть на hh.ru", url=f"https://hh.ru/vacancy/{vacancy_id}"),
        ],
    ])
    return kb


def hide_confirm_keyboard(vacancy_id: str, employer_name: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🙅 Только эту вакансию", callback_data=f"hide_one:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text=f"🏢 Все от «{employer_name[:20]}»", callback_data=f"hide_emp:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"hide_cancel:{vacancy_id}"),
        ],
    ])
    return kb


def settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔎 Ключевые слова", callback_data="set:keywords"),
            InlineKeyboardButton(text="🎯 Опыт", callback_data="set:experience"),
        ],
        [
            InlineKeyboardButton(text="📍 Город/удалёнка", callback_data="set:schedule"),
            InlineKeyboardButton(text="💰 Зарплата", callback_data="set:salary"),
        ],
        [
            InlineKeyboardButton(text="📊 Порог соответствия", callback_data="set:threshold"),
            InlineKeyboardButton(text="⏱ Интервал поиска", callback_data="set:interval"),
        ],
        [
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="set:notifications"),
            InlineKeyboardButton(text="🎫 Только с аккредитацией", callback_data="set:accreditation"),
        ],
        [
            InlineKeyboardButton(text="📁 Банк профиля", callback_data="set:bank"),
        ],
        [
            InlineKeyboardButton(text="🚫 Скрытые вакансии", callback_data="set:hidden"),
            InlineKeyboardButton(text="🏢 Скрытые работодатели", callback_data="set:hiddenemp"),
        ],
        [
            InlineKeyboardButton(text="✅ Откликнулся", callback_data="set:responded"),
            InlineKeyboardButton(text="🔄 Обновить профиль", callback_data="set:profile"),
        ],
    ])
    return kb


def bank_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬆️ Загрузить файл", callback_data="bank:upload"),
            InlineKeyboardButton(text="🤖 Сгенерировать", callback_data="bank:generate"),
        ],
        [
            InlineKeyboardButton(text="👀 Просмотреть", callback_data="bank:view"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="bank:delete"),
        ],
        [
            InlineKeyboardButton(text="↩️ Назад", callback_data="settings"),
        ],
    ])
    return kb


def cancel_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel")],
    ])
    return kb