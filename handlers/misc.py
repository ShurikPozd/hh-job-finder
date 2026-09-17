import json
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
from formatters import skills_line, format_card
from keyboards import main_keyboard, vacancy_list_keyboard, vacancy_keyboard

log = logging.getLogger("handlers.misc")
router = Router()

PAGE_SIZE = 10

HELP_TEXT = (
    "🤖 Справка по командам:\n\n"
    "/start — приветствие и настройка профиля\n"
    "/search — ручной поиск вакансий прямо сейчас\n"
    "/vacancies — список уже присланных вакансий\n"
    "/settings — настройки фильтров, банка и профиля\n"
    "/status — текущий профиль и статистика\n"
    "/recheck — перепроверка пропущенных вакансий\n"
    "/help — эта справка\n\n"
    "💡 Клавиатура внизу дублирует команды, а в сообщениях вакансий "
    "есть кнопки «Подробнее», «Сопроводительное», «Откликнулся»."
)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=main_keyboard())


@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    user = await router.obj.db.get_user(user_id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    profile = json.loads(user.get("profile") or "{}")
    bank = "✅ загружен" if user.get("profile_bank") else "— не загружен"
    stats = await router.obj.db.sent_stats(user_id)
    usage = await router.obj.db.llm_usage_today()
    last_search = user.get("last_search_at") or "ещё не было"
    skills = skills_line(profile.get("skills"), limit=20)
    resume = user.get("resume_text")
    resume_line = "📄 Резюме: не сохранено"
    if resume:
        resume_line = f"📄 Резюме: загружено {str(user.get('resume_at') or '')[:10]} · {len(resume)} симв."
    rc = await router.obj.db.recheck_stats()
    recheck_line = f"🔁 Перепроверка: {rc['pending']} в очереди · найдено {rc['sent']}"
    lines = [
        "📊 Статус:\n",
        f"👤 {profile.get('name') or '—'} · {profile.get('title') or '—'}",
        f"🛠 Навыки: {skills}",
        f"💳 Ожидание по ЗП: {profile.get('salary_expectation') or '—'}",
        f"🎯 Порог соответствия: {user.get('match_threshold') or config.DEFAULT_MATCH_THRESHOLD}/10",
        f"⏱ Интервал поиска: {user.get('search_interval_hours') or config.DEFAULT_SEARCH_INTERVAL_HOURS} ч",
        f"🔔 Уведомления: {'вкл' if user.get('notifications_enabled', 1) else 'выкл'}",
        f"🎫 Аккредитация: {'только IT-аккредитованные' if user.get('only_accredited', 1) else 'любые'}",
        f"📁 Банк профиля: {bank}",
        resume_line,
        "",
        f"💼 Прислано вакансий: {stats['sent']}",
        f"✅ Откликнулся: {stats['responded']}",
        f"🚫 Скрыто: {stats['hidden']}",
        f"🕐 Последний поиск: {last_search}",
        f"⏳ Идёт поиск сейчас: {'да' if router.obj.is_searching(user_id) else 'нет'}",
        recheck_line,
        f"🔌 LLM сегодня: {usage['calls']} вызовов · {usage['tokens']} токенов",
    ]
    if usage["models"]:
        lines.append("   " + " · ".join(
            f"{m['model'].split('/')[-1]}: {m['calls']}" for m in usage["models"]))
    await message.answer("\n".join(lines))


# ================= Список вакансий =================

async def show_vacancy_list(who, page: int = 0, sort: str = "date",
                            filter_: str = "all", edit: bool = True):
    """who — CallbackQuery (edit) или Message (answer)."""
    user_id = who.from_user.id
    data = await router.obj.db.list_sent_vacancies(
        user_id, page=page, page_size=PAGE_SIZE, sort=sort, filter_=filter_)
    items, total = data["items"], data["total"]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    def target(tag, **kw):
        if hasattr(who, "message"):
            return who.message.edit_text(tag, **kw)
        return who.answer(tag, **kw)

    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В настройки", callback_data="settings")],
        ])
        text = "🗂 Присланных вакансий пока нет.\n" \
               "Жми «🔍 Поиск» или подожди фонового поиска — всё появится здесь."
        await target(text, reply_markup=kb)
        return

    first = page * PAGE_SIZE + 1
    last = min((page + 1) * PAGE_SIZE, total)
    text = f"🗂 Присланные вакансии · {first}–{last} из {total}"
    kb = vacancy_list_keyboard(items, page, total_pages, sort, filter_)
    await target(text, reply_markup=kb)


@router.message(Command("recheck"))
async def cmd_recheck(message: Message):
    user_id = message.from_user.id
    user = await router.obj.db.get_user(user_id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала настрой профиль: /start")
        return
    db = router.obj.db
    stats = await db.recheck_stats()
    base = ""
    if stats["pending"] == 0:
        added = await db.recheck_seed()
        stats = await db.recheck_stats()
        base = f"Очередь была пуста — добавил пропущенных: {added}.\n\n"
    models = [m for m in (config.GROQ_SCORE_MODEL, config.GROQ_MODEL) if m]
    used = await db.llm_tokens_today(models, source="recheck")
    await message.answer(
        base
        + "🔁 Перепроверка пропущенных вакансий:\n"
        + f"⏳ В очереди: {stats['pending']}\n"
        + f"✅ Проверено: {stats['done']}\n"
        + f"↩ Найдено и прислано: {stats['sent']}\n"
        + f"❌ Ошибок: {stats['failed']}\n"
        + f"🔌 Сегодня на перепроверку: {used} / {config.RECHECK_TOKEN_BUDGET_PER_DAY} токенов\n"
        + f"⏱ Автозапуск: каждые {config.RECHECK_INTERVAL_MIN} мин"
    )


@router.message(Command("vacancies"))
async def cmd_vacancies(message: Message):
    await show_vacancy_list(message, 0, "date", "all", edit=False)


@router.callback_query(F.data.startswith("vl:"))
async def cb_vl(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 4:
        return
    _, page, sort, filter_ = parts
    await show_vacancy_list(call, int(page), sort, filter_, edit=True)
    await call.answer()


@router.callback_query(F.data == "vl_none")
async def cb_vl_none(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("vl_open:"))
async def cb_vl_open(call: CallbackQuery):
    vid = call.data.split(":", 1)[1]
    msg_id = await router.obj.db.get_sent_message(call.from_user.id, vid)
    if msg_id:
        try:
            await call.message.bot.copy_message(
                chat_id=call.from_user.id,
                from_chat_id=call.from_user.id,
                message_id=msg_id,
            )
            await call.answer("Вот оригинал сообщения ↑")
            return
        except Exception as e:
            log.warning("copy_message %s не удался: %s", vid, e)
    rec = await router.obj.db.get_vacancy(vid)
    if not rec:
        await call.answer("Вакансия не найдена.", show_alert=True)
        return
    await call.message.answer(
        format_card(rec),
        reply_markup=vacancy_keyboard(vid),
        disable_web_page_preview=True,
    )
    await call.answer()


# ================= Reply-клавиатура =================

@router.message(F.text.in_({"📊 Статус", "❓ Помощь", "💼 Вакансии"}))
async def btn_menu(message: Message):
    if message.text == "📊 Статус":
        await cmd_status(message)
    elif message.text == "❓ Помощь":
        await cmd_help(message)
    else:
        await cmd_vacancies(message)