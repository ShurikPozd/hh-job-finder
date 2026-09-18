from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)

MAIN_MENU_BUTTONS = ("🔍 Поиск", "💼 Вакансии", "⚙️ Настройки", "📊 Статус", "❓ Помощь")


def main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="💼 Вакансии")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или введи команду…",
    )
    return kb


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
            InlineKeyboardButton(text="💼 Мои вакансии", callback_data="set:vacancies"),
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


def bank_sections_keyboard(sections: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Оглавление банка: кнопка на каждый раздел (заголовок '## ') + «назад»."""
    rows = [
        [InlineKeyboardButton(text=title[:36], callback_data=f"bank:sec:{i}")]
        for i, (title, _body) in enumerate(sections)
    ]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="bank:toc_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧾 Что понял бот", callback_data="set:profile_full"),
        ],
        [
            InlineKeyboardButton(text="👁 Резюме", callback_data="set:resume_view"),
            InlineKeyboardButton(text="📥 Резюме файлом", callback_data="set:resume_download"),
        ],
        [
            InlineKeyboardButton(text="♻️ Распарсить заново", callback_data="set:reparse"),
        ],
        [
            InlineKeyboardButton(text="📄 Загрузить файл", callback_data="set:profile_reupload"),
            InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="set:profile_edit"),
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


def vacancy_list_keyboard(items: list[dict], page: int, total_pages: int,
                          sort: str, filter_: str) -> InlineKeyboardMarkup:
    """Список присланных вакансий: строки-кнопки + пагинация + сортировка/фильтр."""
    rows = []
    for it in items:
        prefix = "✅ " if it.get("status") == "responded" else "💼 "
        label = prefix + (it.get("name") or it["vacancy_id"])
        if it.get("employer_name"):
            label += f" — {it['employer_name']}"
        rows.append([InlineKeyboardButton(text=label[:60],
                                          callback_data=f"vl_open:{it['vacancy_id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️",
                                        callback_data=f"vl:{page - 1}:{sort}:{filter_}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}",
                                    callback_data="vl_none"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️",
                                        callback_data=f"vl:{page + 1}:{sort}:{filter_}"))
    rows.append(nav)

    def mark(active: bool, label: str) -> str:
        return ("▫️" if active else "") + label

    rows.append([
        InlineKeyboardButton(
            text=mark(sort == "date", "🔽 Дата"),
            callback_data=f"vl:0:date:{filter_}"),
        InlineKeyboardButton(
            text=mark(sort == "score", "🎯 Рейтинг"),
            callback_data=f"vl:0:score:{filter_}"),
    ])
    rows.append([
        InlineKeyboardButton(
            text=mark(filter_ == "all", "Все"),
            callback_data=f"vl:0:{sort}:all"),
        InlineKeyboardButton(
            text=mark(filter_ == "responded", "✅ Откликнулся"),
            callback_data=f"vl:0:{sort}:responded"),
        InlineKeyboardButton(
            text=mark(filter_ == "pending", "📄 Не откликался"),
            callback_data=f"vl:0:{sort}:pending"),
    ])
    rows.append([InlineKeyboardButton(text="↩️ В настройки", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)