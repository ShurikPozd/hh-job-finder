import os
from dotenv import load_dotenv

load_dotenv()

# ===== Telegram =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0) or None

# ===== Groq (LLM) =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
def _normalize_groq_model(name: str) -> str:
    # id qwen/qwen3.6-27b на Groq не существует (404), на аккаунте — qwen/qwen3.8-27b
    if "qwen3.6" in name:
        return name.replace("qwen3.6", "qwen3.8")
    return name


GROQ_MODEL = _normalize_groq_model(os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"))
# Парсинг резюме (длинный вход): редкая операция (~9k токенов на резюме),
# поэтому держим его на compound-mini и не тратим пул qwen.
GROQ_PARSE_MODEL = os.getenv("GROQ_PARSE_MODEL", "groq/compound-mini").strip()
# Модель скоринга вакансий (главный потребитель квоты). Пусто = GROQ_MODEL.
# qwen: 200k токенов/день; compound-mini работает на llama-3.3-70b-versatile
# с лимитом всего 100k/день — поэтому он только резерв.
GROQ_SCORE_MODEL = _normalize_groq_model(
    os.getenv("GROQ_SCORE_MODEL", "qwen/qwen3.8-27b").strip())
GROQ_TIMEOUT_SEC = int(os.getenv("GROQ_TIMEOUT_SEC") or 45)
# Бесплатный тир Groq: 1000 output-токенов/мин (OTPM) на qwen, и лимитер
# резервирует запрошенный max_tokens — поэтому держим его маленьким.
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS") or 600)
GROQ_SCORE_MAX_TOKENS = int(os.getenv("GROQ_SCORE_MAX_TOKENS") or 400)
GROQ_LETTER_MAX_TOKENS = int(os.getenv("GROQ_LETTER_MAX_TOKENS") or 800)
GROQ_BANK_MAX_TOKENS = int(os.getenv("GROQ_BANK_MAX_TOKENS") or 2500)
# Письма должны читать банк ЦЕЛИКОМ, но лимит qwen 8000 TPM/запрос (Groq
# отвечает на превышение 413 «Request too large») не даёт передать больше
# ~7k токенов промпта за один вызов. Банк уплотнён, чтобы влезать в
# GROQ_LETTER_BANK_MAX_CHARS симв.; при превышении — лог-предупреждение.
GROQ_LETTER_BANK_MAX_CHARS = int(os.getenv("GROQ_LETTER_BANK_MAX_CHARS") or 16000)
# Описание вакансии в промпте письма: суть всегда в начале, полный текст не
# нужен — режем, чтобы освободить место под банк в бюджете ITPM (7000).
GROQ_LETTER_DESC_MAX_CHARS = int(os.getenv("GROQ_LETTER_DESC_MAX_CHARS") or 2000)

# Карточка кандидата в промпте письма: детали проектов целиком несёт банк,
# поэтому профиль передаём компактно (обучение, языки, локация + сводки проектов
# без описаний). Защита от 413 по ITPM при «распухании» профиля.
GROQ_LETTER_PROFILE_MAX_CHARS = int(os.getenv("GROQ_LETTER_PROFILE_MAX_CHARS") or 3500)

# ===== Бюджет LLM =====
# Оценка стоимости одной вакансии (вход+выход) для планирования прогона.
SCORE_EST_TOKENS = int(os.getenv("SCORE_EST_TOKENS") or 1500)
# Бюджеты на прогон и на сутки. Общий пул qwen (200k/день) делят также
# tg-saver и расширение пользователя — берём только часть.
SCORE_TOKEN_BUDGET_PER_RUN = int(os.getenv("SCORE_TOKEN_BUDGET_PER_RUN") or 18000)
SCORE_TOKEN_BUDGET_PER_DAY = int(os.getenv("SCORE_TOKEN_BUDGET_PER_DAY") or 60000)

# ===== Перепроверка пропущенных вакансий =====
# Очередь seen-без-vacancies: воркер каждые N минут берёт пачку, фильтрует по
# аккредитации и скорингу, находки присылает с пометкой «↩ найдена при перепроверке».
# Бюджет отдельный от поиска (учитывается через llm_usage.source='recheck').
RECHECK_ENABLED = int(os.getenv("RECHECK_ENABLED") or 0) == 1
RECHECK_BATCH = int(os.getenv("RECHECK_BATCH") or 5)
RECHECK_INTERVAL_MIN = int(os.getenv("RECHECK_INTERVAL_MIN") or 20)
RECHECK_TOKEN_BUDGET_PER_DAY = int(os.getenv("RECHECK_TOKEN_BUDGET_PER_DAY") or 20000)

# ===== БД =====
DB_PATH = os.getenv("DB_PATH", os.path.join("data", "hh_job_finder.db"))

# ===== Банк профиля =====
# Если задан путь к файлу банка (.md/.txt) — автоматически грузим его в
# users.profile_bank OWNER_ID при старте (резерв: /settings → Банк → загрузить).
BANK_FILE = os.getenv("BANK_FILE", "").strip() or None

# ===== hh.ru поиск: дефолты =====
HH_AREA = int(os.getenv("HH_AREA") or 1)  # 1 = Москва
HH_EXPERIENCE = os.getenv("HH_EXPERIENCE", "noExperience,between1And3")
HH_SCHEDULE = os.getenv("HH_SCHEDULE", "fullDay,remote")
HH_PERIOD = int(os.getenv("HH_PERIOD") or 14)  # дней глубины поиска, max 30
HH_PER_PAGE = int(os.getenv("HH_PER_PAGE") or 20)
HH_DEFAULT_KEYWORDS = [
    k.strip()
    for k in (os.getenv("HH_DEFAULT_KEYWORDS") or
              "python developer,junior python,qa automation,junior qa,fullstack python")
    .split(",")
    if k.strip()
]
HH_MIN_SALARY = int(os.getenv("HH_MIN_SALARY") or 0)

# ===== Логика уведомлений =====
DEFAULT_MATCH_THRESHOLD = int(os.getenv("DEFAULT_MATCH_THRESHOLD") or 6)  # 0..10
DEFAULT_SEARCH_INTERVAL_HOURS = float(os.getenv("DEFAULT_SEARCH_INTERVAL_HOURS") or 3)
# Тик фонового планировщика: чаще интервала юзера, внутри решается кого гонять.
SCHEDULER_TICK_MIN = int(os.getenv("SCHEDULER_TICK_MIN") or 30)
SEARCH_INTERVAL_MIN = float(os.getenv("SEARCH_INTERVAL_MIN") or 1)
SEARCH_INTERVAL_MAX = float(os.getenv("SEARCH_INTERVAL_MAX") or 168)

# ===== Реестр Минцифры =====
# api.crftr.net мёртв (таймаутит с 2026) — по умолчанию выключен,
# реестр проверяется через CSV data.gov.ru
REGISTRY_URL = os.getenv("REGISTRY_URL", "").strip()
REGISTRY_CACHE_TTL_DAYS = int(os.getenv("REGISTRY_CACHE_TTL_DAYS") or 30)
# Слова-маркеры аккредитации в тексте описания вакансии
ACCREDITATION_MARKERS = [
    m.strip()
    for m in (os.getenv("ACCREDITATION_MARKERS") or
              "аккредитац,аккредитована,аккредитованная,IT-аккредитац,аккредитацию,отсрочка,бронь,бронирован,военн")
    .split(",")
    if m.strip()
]

# ===== HTTP (health check, Render PORT) =====
HTTP_PORT = int(os.getenv("PORT") or os.getenv("HTTP_PORT") or 0)

# ===== Прокси (для локального запуска из РФ) =====
TG_PROXY = os.getenv("TG_PROXY", "").strip() or None  # например socks5://127.0.0.1:9050
GROQ_PROXY = os.getenv("GROQ_PROXY", "").strip() or None
# Таймаут API Telegram: скачивание файлов резюме через SOCKS идёт медленно,
# дефолта 60 сек не хватает (ловили TimeoutError на 31-й секунде)
TG_API_TIMEOUT = float(os.getenv("TG_API_TIMEOUT") or 150)

# Количество одновременных запросов к hh.ru
HH_CONCURRENCY = int(os.getenv("HH_CONCURRENCY") or 3)
# Задержка между запросами к hh.ru (сек), чтобы не ловить 429
HH_RATE_LIMIT_DELAY = float(os.getenv("HH_RATE_LIMIT_DELAY") or 0.3)

# ===== Облачный бэкап (Render free: эфемерный диск) =====
# Настройки как в tg-saver-bot-cloud: дамп БД в GitHub через Git Data API
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip() or None
GITHUB_REPO = os.getenv("GITHUB_REPO", "ShurikPozd/hh-job-finder-backups").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_PATH = os.getenv("GITHUB_PATH", "backups").strip().strip("/")
BACKUP_INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS") or 4)