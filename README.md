# hh-job-finder

Telegram-бот поиска вакансий hh.ru для junior Python/QA разработчика.

Мультиюзер, фоновый поиск по расписанию, проверка IT-аккредитации работодателя
(3 источника), LLM-скоринг соответствия (Groq), генерация сопроводительных писем
из банка профиля.

## Возможности

- Поиск по ключевым словам (настраиваемый список) с фильтрами: регион, опыт, график, вилка ЗП
- **Проверка IT-аккредитации** работодателя по 3 источникам:
  1. **hh.ru** — бейдж «Аккредитованная ИТ-компания» на странице вакансии
  2. **Реестр Минцифры** — по ИНН работодателя (если ИНН указан; CSV с data.gov.ru)
  3. **Описание вакансии** — маркеры: «аккредитац», «отсрочка», «бронь» и т. п.
  В карточке указывается, по какому источнику подтверждена аккредитация.
- **LLM-скоринг** соответствия вакансии вакансии профилю (Groq qwen) — порог настраивается
- Кнопки на карточке: Подробнее · Сопроводительное · Откликнулся · Не интересно · Открыть на hh.ru
- «Не интересно» → скрыть вакансию или все вакансии работодателя
- **Банк профиля**: загрузка `.md`-файла или генерация через LLM — персональные сопроводительные
- Онбординг: загрузка резюме (`.txt/.docx/.pdf/.doc`) или ручной ввод

## Запуск локально

```bash
pip install -r requirements.txt
copy .env.example .env   # заполнить BOT_TOKEN, OWNER_ID, GROQ_API_KEY
python bot.py
```

Из РФ для доступа к Telegram нужен VPN: `TG_PROXY=socks5://127.0.0.1:PORT` в `.env`.

## Деплой на Render

Web Service (Docker). Health check: `GET /healthz` на переменной `PORT`.

## Переменные окружения (.env)

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token | — |
| `OWNER_ID` | Telegram ID владельца (доступ только ему) | — |
| `GROQ_API_KEY` | Ключ Groq для LLM | — |
| `GROQ_MODEL` | Модель Groq | `qwen/qwen3.6-27b` |
| `GROQ_MAX_TOKENS` | Лимит токенов ответа LLM (бюджет OTPM) | `600` |
| `DB_PATH` | Путь к SQLite | `data/hh_job_finder.db` |
| `HH_DEFAULT_KEYWORDS` | Дефолтные ключевые слова | `python developer,junior python,q...` |
| `HH_AREA` | Регион hh.ru (1 = Москва) | `1` |
| `HH_EXPERIENCE` | Опыт | `noExperience,between1And3` |
| `HH_SCHEDULE` | График | `fullDay,remote` |
| `HH_PERIOD` | Глубина поиска, дней | `14` |
| `DEFAULT_MATCH_THRESHOLD` | Порог соответствия | `6` |
| `DEFAULT_SEARCH_INTERVAL_HOURS` | Интервал фонового поиска | `3` |
| `TG_PROXY` | Прокси для Telegram (локально из РФ) | — |
| `PORT`/`HTTP_PORT` | Порт health check (Render) | `0` |

## Структура

```
bot.py              # точка входа: poll + HTTP health check + scheduler
config.py           # конфигурация из env
db.py               # SQLite (aiosqlite): users, vacancies, seen, hidden, registry cache
hh_client.py        # HTML-клиент hh.ru (публичный API закрыт с апреля 2026)
analyzer.py         # Groq: скоринг, сопроводительные, банк, парсинг резюме
registry.py         # проверка реестра Минцифры по ИНН
service.py          # пайплайн search → аккредитация → скоринг → отправка
scheduler.py        # APScheduler фоновый поиск
handlers/           # aiogram: start (онбординг), search, settings, callbacks
```

## Важно

- Публичный API `api.hh.ru` с апреля 2026 закрыт (403 для неавторизованных) —
  бот парсит HTML-страницы `hh.ru` через BeautifulSoup с дефолтным браузерным
  User-Agent. Не делайте больше ~3 запросов/сек (rate limit + антибот).
- Бесплатный тир Groq: ~1000 output-токенов/мин — вызовы LLM расходятся с паузой
  ~25 сек (настраивается `GROQ_CALL_GAP_SEC` в `analyzer.py`).