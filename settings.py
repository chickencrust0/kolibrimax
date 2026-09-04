"""
settings.py — единственная точка чтения окружения.

Портировано из alfacrm-bot: тот же принцип единой точки правды для
конфигурации, но переменные окружения относятся к MAX и impulseCRM.

Некоторые константы (номера статусов, имена полей impulseCRM) отмечены
как ТРЕБУЕТ ПРОВЕРКИ — impulseCRM не публикует схему полей в открытой
документации (см. IMPULSE_API reference, раздел 10). Значения по
умолчанию — наиболее вероятные варианты; сверьте их со схемой вашего
аккаунта скриптом impulse_introspect.py и поправьте в .env при
необходимости.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore


def _int_list(raw: str):
    result = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ==================== ДОСТУПЫ: MAX ====================

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")

# webhook | polling. Long Polling не годится для продакшена (см. докс MAX),
# но не требует публичного домена с доверенным TLS-сертификатом —
# поэтому по умолчанию включён polling, как было в исходном боте с aiogram.
MAX_MODE = os.getenv("MAX_MODE", "polling").strip().lower()
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL", "").strip()
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
MAX_WEBHOOK_HOST = os.getenv("MAX_WEBHOOK_HOST", "0.0.0.0").strip()
MAX_WEBHOOK_PORT = _int("MAX_WEBHOOK_PORT", 8080)
MAX_WEBHOOK_PATH = os.getenv("MAX_WEBHOOK_PATH", "/webhook").strip()

MANAGER_IDS = _int_list(os.getenv("ADMIN_MAX_IDS", ""))

# Не более 2 сообщений/сек в один диалог (документировано MAX).
MAX_SEND_DELAY = _float("MAX_SEND_DELAY", 0.55)
MAX_BROADCAST_DELAY = _float("MAX_BROADCAST_DELAY", 0.55)
MAX_RPS = _float("MAX_RPS", 1.8)
MAX_TIMEOUT = _float("MAX_TIMEOUT", 30.0)
MAX_LONGPOLL_TIMEOUT = _int("MAX_LONGPOLL_TIMEOUT", 30)

# Отключать только для диагностики (антивирус/корпоративный прокси,
# подменяющий TLS) — см. ssl_utils.py. По умолчанию проверка включена.
MAX_SSL_VERIFY = _bool("MAX_SSL_VERIFY", True)

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "bot.db"))

# ==================== ДОСТУПЫ: impulseCRM ====================

IMPULSE_DOMAIN = os.getenv("IMPULSE_DOMAIN", "").strip().rstrip("/")
IMPULSE_LOGIN = os.getenv("IMPULSE_LOGIN", "").strip()
IMPULSE_API_KEY = os.getenv("IMPULSE_API_KEY", "").strip()

# ТРЕБУЕТ ПРОВЕРКИ: точный префикс пути между доменом и названием сущности
# не публикуется в базе знаний impulseCRM. Скопируйте его из кнопки
# «Пример запроса» в разделе «Настройка API» вашего аккаунта.
# Плейсхолдеры {entity} и {action} подставляются клиентом.
# ТРЕБУЕТ ПРОВЕРКИ: точный префикс пути между доменом и названием сущности
# не публикуется в базе знаний impulseCRM. Подтверждено примером запроса
# из личного кабинета: /api/public/{entity}/{action}. Если в вашем
# кабинете «Пример запроса» показывает другой путь — поправьте здесь.
IMPULSE_API_PATH = os.getenv("IMPULSE_API_PATH", "/api/public/{entity}/{action}").strip()

# ==================== ВНУТРЕННИЙ API (check_visits) ====================
#
# Отметка посещения делается НЕ через публичный API (/api/public/...), а
# через внутренний API веб-интерфейса impulseCRM: POST /api/check_visits/check.
# Эти эндпоинты найдены разбором фронтенда, а НЕ документированы вендором —
# отсюда следствия, которые важно понимать:
#   * авторизация может отличаться: браузер ходит с cookie-сессией, а бот —
#     с "Authorization: Basic <apiToken>". Примет ли внутренний эндпоинт
#     API-ключ — ПРОВЕРЬТЕ скриптом impulse_probe.py (он только читает);
#   * путь и формат тела могут измениться при обновлении CRM без
#     предупреждения, в отличие от публичного API;
#   * точная форма объектов account/target из кода фронтенда не видна —
#     сверьте по реальному запросу из DevTools (см. README, раздел про
#     проверку check_visits).
#
# Пока не проверено — держим выключенным: при IMPULSE_CHECK_VISITS_ENABLED=false
# кнопка «отметить посещение» честно скажет, что механизм не подключён,
# вместо того чтобы слать в CRM неизвестно что.
IMPULSE_CHECK_VISITS_ENABLED = _bool("IMPULSE_CHECK_VISITS_ENABLED", False)
# ПОДТВЕРЖДЕНО пробой: эти эндпоинты лежат под тем же префиксом
# /api/public/, что и публичный API (по /api/{path} сервер отдавал
# оболочку SPA, по /api/public/{path} — настоящий JSON и 405 Allow: POST).
IMPULSE_INTERNAL_PATH = os.getenv("IMPULSE_INTERNAL_PATH", "/api/public/{path}").strip()
IMPULSE_PATH_CHECK = os.getenv("IMPULSE_PATH_CHECK", "check_visits/check").strip()
IMPULSE_PATH_BURN = os.getenv("IMPULSE_PATH_BURN", "check_visits/burn_one").strip()
IMPULSE_PATH_VISITS = os.getenv("IMPULSE_PATH_VISITS", "check_visits/visits").strip()
IMPULSE_PATH_LAST_ACCOUNTS = os.getenv(
    "IMPULSE_PATH_LAST_ACCOUNTS", "client/last_accounts"
).strip()

# ПРОВЕРЕНО на реальном аккаунте: внутренний API НЕ принимает API-ключ —
# на "Authorization: Basic <ключ>" он отдаёт HTML страницы входа. Значит,
# для него нужна сессия браузера. Обходной путь: скопировать значение
# cookie сессии из DevTools (вкладка Application -> Cookies) и положить
# сюда целиком, в виде "PHPSESSID=abc123" (можно несколько через "; ").
# Это временное решение: cookie живёт ограниченное время и его придётся
# обновлять — постоянное решение см. в README, раздел 3.
IMPULSE_SESSION_COOKIE = os.getenv("IMPULSE_SESSION_COOKIE", "").strip()

IMPULSE_RPS = _float("IMPULSE_RPS", 3.0)
IMPULSE_TIMEOUT = _float("IMPULSE_TIMEOUT", 30.0)
IMPULSE_PAGE_SIZE = _int("IMPULSE_PAGE_SIZE", 200)
IMPULSE_MAX_PAGES = _int("IMPULSE_MAX_PAGES", 200)
IMPULSE_SSL_VERIFY = _bool("IMPULSE_SSL_VERIFY", True)

BRANCH_ID = os.getenv("BRANCH_ID", "").strip()  # опционально — если филиалов несколько

# Список параметров выборки (fields/limit/page/sort/columns) подтверждён
# документацией impulseCRM, а вот синтаксис фильтра `columns` (операторы
# сравнения, диапазоны дат) — нет (см. Справочник по API impulseCRM,
# раздел 5 и 10, пункт 3). Поэтому клиент НЕ пытается фильтровать по датам
# на стороне impulseCRM — он выгружает записи целиком (постранично) и
# фильтрует в Python. Это медленнее при очень больших базах, зато не
# зависит от угадывания синтаксиса, который может молча дать неверную
# выборку вместо ошибки.
IMPULSE_FIELD_CLIENT_PHONE = os.getenv("IMPULSE_FIELD_CLIENT_PHONE", "phone")
# Подтверждено примером запроса из личного кабинета: ФИО хранится тремя
# отдельными полями в camelCase (не единым "name", как предполагалось
# раньше) — CRM в целом использует camelCase, а не snake_case, для
# составных названий полей.
IMPULSE_FIELD_CLIENT_LAST_NAME = os.getenv("IMPULSE_FIELD_CLIENT_LAST_NAME", "lastName")
IMPULSE_FIELD_CLIENT_FIRST_NAME = os.getenv("IMPULSE_FIELD_CLIENT_FIRST_NAME", "name")
IMPULSE_FIELD_CLIENT_MIDDLE_NAME = os.getenv("IMPULSE_FIELD_CLIENT_MIDDLE_NAME", "middleName")

IMPULSE_FIELD_TEACHER_PHONE = os.getenv("IMPULSE_FIELD_TEACHER_PHONE", "phone")
# Не подтверждено для teacher отдельно (пример показывал только client) —
# предполагаем ту же схему ФИО, что и у client, сверьте impulse_introspect.py.
IMPULSE_FIELD_TEACHER_LAST_NAME = os.getenv("IMPULSE_FIELD_TEACHER_LAST_NAME", "lastName")
IMPULSE_FIELD_TEACHER_FIRST_NAME = os.getenv("IMPULSE_FIELD_TEACHER_FIRST_NAME", "name")
IMPULSE_FIELD_TEACHER_MIDDLE_NAME = os.getenv("IMPULSE_FIELD_TEACHER_MIDDLE_NAME", "middleName")

# ==================== СХЕМА impulseCRM (ТРЕБУЕТ ПРОВЕРКИ) ====================
#
# impulseCRM не публикует состав полей сущностей в открытой документации
# (см. Справочник по API impulseCRM, раздел 3 и 10). Ниже — рабочие
# предположения по именам полей; сверьте их скриптом
# impulse_introspect.py и при расхождении поправьте здесь через .env,
# ничего не меняя в коде.

# Сущность "занятие расписания". ВАЖНОЕ ОТКРЫТИЕ по снятой схеме вашего
# аккаунта (schedule): это НЕ одна строка на одно фактическое занятие
# (как lesson в AlfaCRM), а строка ПОВТОРЯЮЩЕГОСЯ ПРАВИЛА — day (день
# недели), minutesBegin/minutesEnd (время от полуночи), dateBegin/dateEnd
# (диапазон действия правила); date/timeFrom/timeTo у вас были пустыми.
# Ниже — поля для обоих вариантов: если у вас единичные (не повторяющиеся)
# занятия хранятся с прямой датой/временем, используются
# IMPULSE_FIELD_SCHEDULE_DATE/TIME_FROM/TIME_TO; для повторяющихся
# правил — блок DAY/MINUTES_BEGIN/MINUTES_END/DATE_BEGIN/DATE_END.
# bot/impulse_client.py разворачивает повторяющееся правило в конкретные
# занятия по дням недели в пределах окна кеша (settings.CACHE_DAYS_*).
IMPULSE_FIELD_SCHEDULE_DATE = os.getenv("IMPULSE_FIELD_SCHEDULE_DATE", "date")
IMPULSE_FIELD_SCHEDULE_TIME_FROM = os.getenv("IMPULSE_FIELD_SCHEDULE_TIME_FROM", "timeFrom")
IMPULSE_FIELD_SCHEDULE_TIME_TO = os.getenv("IMPULSE_FIELD_SCHEDULE_TIME_TO", "timeTo")
IMPULSE_FIELD_SCHEDULE_DAY = os.getenv("IMPULSE_FIELD_SCHEDULE_DAY", "day")
IMPULSE_FIELD_SCHEDULE_MINUTES_BEGIN = os.getenv("IMPULSE_FIELD_SCHEDULE_MINUTES_BEGIN", "minutesBegin")
IMPULSE_FIELD_SCHEDULE_MINUTES_END = os.getenv("IMPULSE_FIELD_SCHEDULE_MINUTES_END", "minutesEnd")
IMPULSE_FIELD_SCHEDULE_DATE_BEGIN = os.getenv("IMPULSE_FIELD_SCHEDULE_DATE_BEGIN", "dateBegin")
IMPULSE_FIELD_SCHEDULE_DATE_END = os.getenv("IMPULSE_FIELD_SCHEDULE_DATE_END", "dateEnd")

# Подтверждено: "teacher" (единственное число, вложенный объект {id,...}),
# а не "teacherId". Для ГРУППОВЫХ занятий (schedule.group непусто, а
# schedule.teacher пуст) преподаватель берётся из group.teacher1/teacher2.
IMPULSE_FIELD_SCHEDULE_TEACHER = os.getenv("IMPULSE_FIELD_SCHEDULE_TEACHER", "teacher")
IMPULSE_FIELD_SCHEDULE_GROUP = os.getenv("IMPULSE_FIELD_SCHEDULE_GROUP", "group")
IMPULSE_FIELD_SCHEDULE_STATUS = os.getenv("IMPULSE_FIELD_SCHEDULE_STATUS", "status")
IMPULSE_FIELD_SCHEDULE_TOPIC = os.getenv("IMPULSE_FIELD_SCHEDULE_TOPIC", "topic")
IMPULSE_FIELD_SCHEDULE_HOMEWORK = os.getenv("IMPULSE_FIELD_SCHEDULE_HOMEWORK", "homework")
IMPULSE_FIELD_SCHEDULE_NOTE = os.getenv("IMPULSE_FIELD_SCHEDULE_NOTE", "note")

# Сущность "запись на занятие" — связывает client с schedule. Пустая в
# снятой схеме вашего аккаунта (нет записей на момент снятия) — имена
# полей ниже пока не подтверждены, сверьте после появления записей.
IMPULSE_FIELD_RESERVATION_SCHEDULE = os.getenv("IMPULSE_FIELD_RESERVATION_SCHEDULE", "schedule")
IMPULSE_FIELD_RESERVATION_CLIENT = os.getenv("IMPULSE_FIELD_RESERVATION_CLIENT", "client")

# Статусы занятия. impulseCRM может хранить это как булевы флаги
# (is_conducted/is_cancelled) вместо трёхзначного статуса AlfaCRM —
# ПРОВЕРЬТЕ схему перед запуском в бою.
STATUS_PLANNED = _int("STATUS_PLANNED", 1)
STATUS_CANCELLED = _int("STATUS_CANCELLED", 2)
STATUS_CONDUCTED = _int("STATUS_CONDUCTED", 3)
ALL_STATUSES = (STATUS_PLANNED, STATUS_CANCELLED, STATUS_CONDUCTED)

# Денежный баланс подтверждённо лежит ПРЯМО на client (поле deposit), а
# НЕ в отдельной сущности абонемента — раньше предполагалось иначе.
IMPULSE_FIELD_CLIENT_BALANCE = os.getenv("IMPULSE_FIELD_CLIENT_BALANCE", "deposit")

# Остаток занятий — в сущности абонемента. У вашего аккаунта заполнен
# group_account (групповые абонементы: trainingsTotal/trainingsUsed/
# trainingsLeft), а individual_account пуст — поэтому дефолт изменён на
# group_account. Если у вас индивидуальные занятия — поставьте
# individual_account и сверьте реальные имена полей impulse_introspect.py
# (структура может отличаться от group_account).
IMPULSE_ACCOUNT_ENTITY = os.getenv("IMPULSE_ACCOUNT_ENTITY", "group_account")

# Абонементы бывают четырёх типов (group/individual/self/rent — см.
# Справочник по API impulseCRM, раздел 6). Раньше бот смотрел только в
# один (IMPULSE_ACCOUNT_ENTITY), поэтому у клиента с индивидуальным
# абонементом остаток занятий всегда выходил нулевым. Теперь
# просматриваются все перечисленные ниже; несуществующие/пустые
# пропускаются без ошибки.
IMPULSE_ACCOUNT_ENTITIES = [
    e.strip() for e in os.getenv(
        "IMPULSE_ACCOUNT_ENTITIES",
        "group_account,individual_account,self_account",
    ).split(",") if e.strip()
]
if IMPULSE_ACCOUNT_ENTITY and IMPULSE_ACCOUNT_ENTITY not in IMPULSE_ACCOUNT_ENTITIES:
    IMPULSE_ACCOUNT_ENTITIES.insert(0, IMPULSE_ACCOUNT_ENTITY)
# Подтверждено: "client" — ВЛОЖЕННЫЙ ОБЪЕКТ {id, ...}, а не плоское
# clientId. impulse_client._ref_id() достаёт id и из вложенного объекта,
# и из плоского скаляра — какой бы вариант тут ни стоял, код справится.
IMPULSE_FIELD_ACCOUNT_CLIENT = os.getenv("IMPULSE_FIELD_ACCOUNT_CLIENT", "client")
IMPULSE_FIELD_ACCOUNT_PAID = os.getenv("IMPULSE_FIELD_ACCOUNT_PAID", "trainingsTotal")
IMPULSE_FIELD_ACCOUNT_USED = os.getenv("IMPULSE_FIELD_ACCOUNT_USED", "trainingsUsed")
# ГЛАВНОЕ ПОЛЕ БАЛАНСА: остаток занятий в абонементе. CRM считает его
# сама (trainingsLeft), и это надёжнее, чем вычитать used из total —
# при заморозке, продлении и подарочных занятиях CRM учитывает их в
# trainingsLeft, а арифметика total-used их теряет. Если поля нет,
# impulse_client откатывается к total-used.
IMPULSE_FIELD_ACCOUNT_LEFT = os.getenv("IMPULSE_FIELD_ACCOUNT_LEFT", "trainingsLeft")
IMPULSE_FIELD_ACCOUNT_ACTIVE = os.getenv("IMPULSE_FIELD_ACCOUNT_ACTIVE", "active")
IMPULSE_FIELD_ACCOUNT_CLOSED = os.getenv("IMPULSE_FIELD_ACCOUNT_CLOSED", "closed")
IMPULSE_FIELD_ACCOUNT_FREEZE = os.getenv("IMPULSE_FIELD_ACCOUNT_FREEZE", "freeze")
IMPULSE_FIELD_ACCOUNT_TYPE_NAME = os.getenv("IMPULSE_FIELD_ACCOUNT_TYPE_NAME", "typeName")
IMPULSE_FIELD_ACCOUNT_END_DATE = os.getenv("IMPULSE_FIELD_ACCOUNT_END_DATE", "endDate")
IMPULSE_FIELD_ACCOUNT_DAYS_LEFT = os.getenv("IMPULSE_FIELD_ACCOUNT_DAYS_LEFT", "daysLeft")
# Список групп, на которые распространяется абонемент. Это ЕДИНСТВЕННАЯ
# доступная связь «клиент → группа» помимо reservation: в вашем аккаунте
# reservation пуста, поэтому без этой связи расписание родителя всегда
# оказывалось пустым (см. impulse_client._customer_index).
IMPULSE_FIELD_ACCOUNT_GROUPS = os.getenv("IMPULSE_FIELD_ACCOUNT_GROUPS", "groups")

# Разовые посещения — ещё один возможный носитель связи «ученик ↔ группа».
# Если про ученика есть точная запись на занятия, членство в группе для
# него не применяется. Иначе ребёнок, записанный на два занятия из
# четырёх, увидит в расписании все четыре занятия группы.
# false — складывать оба источника (старое поведение).
STRICT_ENROLLMENT = _bool("STRICT_ENROLLMENT", True)

IMPULSE_SINGLE_ENTITIES = [
    e.strip() for e in os.getenv(
        "IMPULSE_SINGLE_ENTITIES", "group_single,individual_single",
    ).split(",") if e.strip()
]

# Клиент, записанный на занятие напрямую (индивидуальные занятия), и
# «цель» занятия (группа или клиент) — подтверждено снятой схемой
# schedule: поля client/target присутствуют.
IMPULSE_FIELD_SCHEDULE_CLIENT = os.getenv("IMPULSE_FIELD_SCHEDULE_CLIENT", "client")
IMPULSE_FIELD_SCHEDULE_TARGET = os.getenv("IMPULSE_FIELD_SCHEDULE_TARGET", "target")
# Направление (style) — у schedule оно пустое, зато заполнено у group.
IMPULSE_FIELD_GROUP_STYLE = os.getenv("IMPULSE_FIELD_GROUP_STYLE", "style")

# Справочники (клиенты, педагоги, абонементы) меняются редко —
# держим их в памяти клиента столько секунд (раздел 8.5 справочника).
IMPULSE_LOOKUP_TTL = _int("IMPULSE_LOOKUP_TTL", 120)

# У schedule в impulseCRM НЕТ полей темы/ДЗ/статуса (подтверждено снятой
# схемой). Поэтому тема, домашнее задание и отметка «проведён» хранятся
# в собственной БД бота (таблица lesson_notes). Включайте запись в CRM
# только если вендор подтвердит наличие этих полей — иначе каждая
# попытка сохранить ДЗ будет валиться с ошибкой.
IMPULSE_WRITE_BACK = _bool("IMPULSE_WRITE_BACK", False)


# Кеш держит только окно вокруг сегодняшнего дня.
CACHE_DAYS_BACK = _int("CACHE_DAYS_BACK", 30)
CACHE_DAYS_FORWARD = _int("CACHE_DAYS_FORWARD", 60)
CACHE_REFRESH_MINUTES = _int("CACHE_REFRESH_MINUTES", 5)

# Время вечерней сводки по неявкам менеджеру (по часовому поясу филиала).
ABSENCE_DIGEST_HOUR = _int("ABSENCE_DIGEST_HOUR", 21)
ABSENCE_DIGEST_MINUTE = _int("ABSENCE_DIGEST_MINUTE", 0)

MAX_LESSON_CARDS = _int("MAX_LESSON_CARDS", 20)
LOW_BALANCE_THRESHOLD = _int("LOW_BALANCE_THRESHOLD", 2)

# ==================== ВОРОНКА НОВЫХ КЛИЕНТОВ ====================

# ==================== АКТИВНОСТЬ В БОТЕ ====================
#
# Действия посетителей БОЛЬШЕ НЕ ПРИСЫЛАЮТСЯ менеджеру сообщениями.
# Раньше каждая серия действий уходила отдельной сводкой в чат, и при
# десятке посетителей лента менеджера становилась нечитаемой — в ней
# терялись заявки, обращения и решения по неявкам, то есть ровно то,
# на что менеджеру надо отвечать.
#
# Теперь активность пишется в таблицу activity_log и показывается только
# по кнопке «👀 Активность в боте» в меню менеджера.
MANAGER_NOTIFY_ACTIVITY = _bool("MANAGER_NOTIFY_ACTIVITY", True)

# Сколько последних посетителей показывать по нажатию кнопки и сколько
# действий раскрывать по каждому.
ACTIVITY_USERS_LIMIT = _int("ACTIVITY_USERS_LIMIT", 15)
ACTIVITY_EVENTS_PER_USER = _int("ACTIVITY_EVENTS_PER_USER", 12)

# Сколько дней хранить журнал активности (чистится ночным заданием).
ACTIVITY_KEEP_DAYS = _int("ACTIVITY_KEEP_DAYS", 14)

# Метка сборки. Печатается в лог при запуске, чтобы можно было
# убедиться, какая версия кода реально работает на сервере: пара правок
# «не применилась» просто потому, что на сервере лежали старые файлы.
BUILD = "2026-09-04.3 (остаток+заморозка в меню, адресное ДЗ, заморозка педагогом, базы рассылки, кто вошёл)"

SCHOOL_NAME = os.getenv("SCHOOL_NAME", "Детская академия развития «Колибри»")
SCHOOL_SITE = os.getenv("SCHOOL_SITE", "https://kolibri-academy.ru/")
# Возраст, который школа принимает. Всё, что вне диапазона, не отсекается
# грубо — заявка всё равно создаётся, но менеджеру уходит пометка.
MIN_AGE = _int("MIN_AGE", 3)
MAX_AGE = _int("MAX_AGE", 16)


# ==================== ДОСТУП МЕНЕДЖЕРА ====================
#
# Помимо списка ADMIN_MAX_IDS менеджером можно стать по команде
# /manager с паролем — это позволяет подключать новых сотрудников без
# правки .env и перезапуска бота. Пароль читается из окружения, но
# имеет значение по умолчанию прямо здесь, чтобы бот работал сразу.
#
# ВАЖНО: смените пароль в .env (MANAGER_PASSWORD) — значение ниже
# известно всем, у кого есть исходники.
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "kolibri-manager").strip()

# Счётчик оставшихся БЕСПРИЧИННЫХ заморозок хранится в CRM в поле
# АДРЕСА ПРОЖИВАНИЯ клиента: отдельного поля под это в impulseCRM нет.
# Значение строковое, пустое поле означает 0 оставшихся заморозок.
#
# Раньше здесь был email — поле оказалось занято настоящей почтой, из-за
# чего счётчик и почта затирали друг друга.
#
# Точное имя поля адреса вендором не опубликовано, поэтому оно не зашито
# намертво: клиент проверяет кандидатов из IMPULSE_FIELD_CLIENT_FREEZES_ALT
# по реальной записи клиента и берёт первое существующее. Если знаете имя
# точно — задайте IMPULSE_FIELD_CLIENT_FREEZES в .env, тогда поиск не
# выполняется вовсе.
IMPULSE_FIELD_CLIENT_FREEZES = os.getenv("IMPULSE_FIELD_CLIENT_FREEZES", "").strip()
IMPULSE_FIELD_CLIENT_FREEZES_ALT = [
    p.strip()
    for p in os.getenv(
        "IMPULSE_FIELD_CLIENT_FREEZES_ALT",
        "address,livingAddress,addressLiving,homeAddress,residenceAddress,"
        "actualAddress,addressFact,registrationAddress",
    ).split(",")
    if p.strip()
]

# ==================== ЗАМОРОЗКИ: ОКНО ДО ЗАНЯТИЯ ====================
#
# Родитель может заморозить занятие (и уважительно, и беспричинно)
# только пока до его начала осталось не меньше этого числа часов.
# Позже занятие сгорает — об этом приходит уведомление (см.
# scheduler.notify_burned_lessons).
FREEZE_DEADLINE_HOURS = _float("FREEZE_DEADLINE_HOURS", 5.0)

# Через сколько минут ПОСЛЕ начала занятия боту можно считать его
# сгоревшим и уведомить родителя. Запас нужен, чтобы преподаватель успел
# проставить посещаемость: сразу после звонка отметок ещё нет.
BURN_NOTIFY_AFTER_MINUTES = _int("BURN_NOTIFY_AFTER_MINUTES", 90)

# Как часто планировщик ищет сгоревшие занятия (минуты).
BURN_CHECK_INTERVAL_MINUTES = _int("BURN_CHECK_INTERVAL_MINUTES", 15)

# На сколько дней вперёд смотреть в разделе «Заморозить занятие».
FREEZE_LOOKAHEAD_DAYS = _int("FREEZE_LOOKAHEAD_DAYS", 14)

# ==================== ВРЕМЯ ====================

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
TZ_ERROR = None

if ZoneInfo is None:  # pragma: no cover
    TZ = timezone.utc
    TZ_ERROR = "zoneinfo недоступен (нужен Python 3.9+)"
else:
    try:
        TZ = ZoneInfo(TIMEZONE)
    except Exception as e:
        TZ = timezone.utc
        TZ_ERROR = (
            f"часовой пояс '{TIMEZONE}' не найден ({e.__class__.__name__}). "
            f"Установите пакет tzdata: pip install tzdata"
        )


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()
