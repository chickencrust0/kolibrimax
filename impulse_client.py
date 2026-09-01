"""
impulse_client.py — клиент REST API impulseCRM.

Подтверждено документацией (Справочник по API impulseCRM):
    * URL вида {домен}.impulsecrm.ru/{путь-API}/{сущность}/{экшен}
    * 5 экшенов: list (POST), load (GET), update (POST, id есть/нет —
      правка/создание), delete (POST)
    * список параметров list: fields / limit / page / sort / columns

Подтверждено РЕАЛЬНЫМ примером запроса из личного кабинета (раздел
«Настройка API» → «Пример запроса» — это специфично для конкретного
аккаунта, но следующие факты почти наверняка верны для всех аккаунтов
impulseCRM, так как это особенности самого API, а не настройки школы):
    * Заголовок авторизации — "Authorization: Basic <ключ>", где <ключ> —
      это ЦЕЛИКОМ значение API-ключа из личного кабинета, БЕЗ
      дополнительного base64("login:ключ") на нашей стороне (в примере
      нет вызова base64_encode — значение уже готово к подстановке).
      Это НЕ стандартная HTTP Basic-авторизация, хотя называется так же.
    * Путь по умолчанию — /api/public/{entity}/{action}.
    * `page` нумеруется С ЕДИНИЦЫ (page=1 — первая страница), не с нуля.
    * `sort` — словарь {"поле": "asc"|"desc"}, а не список.
    * `columns` — словарь {"поле": значение} для точного совпадения,
      {"поле": {"from": X, "to": Y}} для диапазона.
    * Даты/время в columns-фильтрах — Unix-timestamp в секундах (не
      строки). Вероятно, так же хранятся сами даты в полях записей —
      функции _to_datetime_str()/_date_key() ниже понимают оба варианта
      (timestamp и строку) на случай, если это верно не для всех полей.
    * Составные названия полей — camelCase (lastName/middleName в
      client), а не snake_case.

НЕ подтверждено вендором (см. раздел 10 справочника — открытые вопросы):
    * состав полей schedule/reservation/*_account (пример показывал
      только client) -> settings.IMPULSE_FIELD_* — сверьте
      impulse_introspect.py
    * обёртка ответа списка (items/total и т.п.) -> _extract_items()
      перебирает несколько вероятных вариантов
    * частичный или полный update -> отправляем ПОЛНУЮ модель записи

Схема-адаптер: get_lessons()/get_lesson() отдают уроки в ТОЙ ЖЕ форме,
в которой их ожидает остальной бот (cache.py, bot/formatting.py,
хендлеры) — {id, date, time_from, time_to, teacher_ids, customer_ids,
status, topic, homework, note, lesson_type_name}. Исходная сырая запись
schedule сохраняется под ключом "_raw" — она нужна update_lesson(), чтобы
отправить обратно ПОЛНУЮ модель, а не только изменённые поля.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

import settings
from ssl_utils import build_ssl_context

logger = logging.getLogger(__name__)

_SSL_CONTEXT = build_ssl_context(settings.IMPULSE_SSL_VERIFY)

STATUS_LABELS = {
    settings.STATUS_PLANNED: "📌 запланирован",
    settings.STATUS_CANCELLED: "❌ отменён",
    settings.STATUS_CONDUCTED: "✅ проведён",
}


class ImpulseCRMError(Exception):
    pass


class _RateLimiter:
    def __init__(self, rps: float):
        self._min_interval = 1.0 / max(rps, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


def _ref_id(value: Any) -> Any:
    """
    Достаёт id из значения поля-ссылки. Подтверждено снятой схемой
    вашего аккаунта: ссылки на другие сущности (client, teacher, branch
    и т.п.) приходят ВЛОЖЕННЫМ ОБЪЕКТОМ {"id": ..., ...}, а не плоским
    id-скаляром, как предполагалось раньше. Понимает оба варианта.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return value


def _ref_ids(value: Any) -> List[Any]:
    """То же самое, но для случаев, где ссылок может быть несколько
    (список объектов) или где на вход может прийти как список, так и
    одиночное значение."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        result = []
        for v in value:
            ref = _ref_id(v)
            if ref is not None:
                result.append(ref)
        return result
    ref = _ref_id(value)
    return [ref] if ref is not None else []


def _normalize_phone(phone: Any) -> str:
    return "".join(filter(str.isdigit, str(phone or "")))


def _phone_matches(phone1: Any, phone2: Any) -> bool:
    clean1, clean2 = _normalize_phone(phone1), _normalize_phone(phone2)
    if len(clean1) < 10 or len(clean2) < 10:
        return False
    return clean1[-10:] == clean2[-10:]


def _date_key(value: Any) -> str:
    """YYYY-MM-DD из значения поля даты — для сравнения строками."""
    normalized = _to_datetime_str(value)
    if not normalized:
        return ""
    return normalized.split(" ")[0]


def _to_datetime_str(value: Any) -> str:
    """
    Приводит значение поля даты/времени к строке "YYYY-MM-DD HH:MM:SS".

    Пример запроса из личного кабинета фильтрует `created` через
    Unix-timestamp в секундах — вероятно, так же хранятся и другие
    дата-поля, но это не подтверждено конкретно для schedule, поэтому
    здесь распознаются оба варианта: число (timestamp) и строка
    (ISO/DD.MM.YYYY и т.п., отдаётся как есть — её понимает
    bot/formatting.py).
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _timestamp_to_str(value)
    s = str(value).strip()
    if s.isdigit() and len(s) >= 9:
        return _timestamp_to_str(int(s))
    return s


def _timestamp_to_str(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=settings.TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError, TypeError):
        return str(ts)


def _service_ref(account: Dict[str, Any]) -> Dict[str, Any]:
    """
    Краткая ссылка на абонемент — в реальных запросах внутреннего API
    абонемент передаётся как {"id":..,"number":..,"entity":"groupAccount"},
    а не целым объектом.
    """
    if not isinstance(account, dict):
        return {"id": account}
    return {
        "id": account.get("id"),
        "number": account.get("number"),
        "entity": account.get("entity") or "groupAccount",
    }


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _minutes_to_hhmm(minutes: Any) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "00:00"
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _parse_iso_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _expand_weekly(
    day: int,
    date_begin_ts: Any,
    date_end_ts: Any,
    window_from_iso: Optional[str],
    window_to_iso: Optional[str],
) -> List[Any]:
    """
    Разворачивает повторяющееся еженедельное правило schedule в список
    конкретных дат, попадающих и в диапазон правила (dateBegin/dateEnd),
    и в запрошенное окно.

    day — день недели ПО СОГЛАШЕНИЮ ISO (1=понедельник..7=воскресенье,
    как у date.isoweekday() в Python). Это НЕ подтверждено вендором
    напрямую — если расписание выглядит сдвинутым на день, поправьте
    здесь маппинг.
    """
    try:
        begin_date = datetime.fromtimestamp(int(date_begin_ts), tz=settings.TZ).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return []

    end_date = None
    if date_end_ts:
        try:
            end_date = datetime.fromtimestamp(int(date_end_ts), tz=settings.TZ).date()
        except (TypeError, ValueError, OSError, OverflowError):
            end_date = None

    window_from = _parse_iso_date(window_from_iso) or begin_date
    window_to = _parse_iso_date(window_to_iso) or (end_date or window_from)

    start = max(begin_date, window_from)
    stop = min(end_date, window_to) if end_date else window_to
    if start > stop:
        return []

    current = start
    while current.isoweekday() != day:
        current += timedelta(days=1)
        if current > stop:
            return []

    result = []
    while current <= stop:
        result.append(current)
        current += timedelta(days=7)
    return result


class ImpulseCRMClient:
    def __init__(
        self,
        domain: str,
        login: str,
        api_key: str,
        api_path_template: Optional[str] = None,
    ):
        self.base_url = domain.rstrip("/")
        self.login = login
        self.api_key = api_key
        # См. докстринг модуля: пример из личного кабинета показывает
        # "Authorization: Basic <ключ>" БЕЗ base64_encode() на стороне
        # клиента — ключ уже используется как есть, а не как пара
        # login:password. Поэтому здесь НЕ aiohttp.BasicAuth (который
        # сам считает base64("login:key") — это дало бы неверный
        # заголовок), а ручное значение.
        self._auth_header = f"Basic {api_key}"
        self.api_path_template = api_path_template or settings.IMPULSE_API_PATH
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._limiter = _RateLimiter(settings.IMPULSE_RPS)

    # ==================== ИНФРАСТРУКТУРА ====================

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
                    self.session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=settings.IMPULSE_TIMEOUT),
                        connector=connector,
                    )
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    def _url(self, entity: str, action: str) -> str:
        path = self.api_path_template.format(entity=entity, action=action)
        return f"{self.base_url}{path}"

    async def _request(
        self,
        method: str,
        entity: str,
        action: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        attempts: int = 3,
    ) -> Any:
        session = await self._get_session()
        url = self._url(entity, action)

        last_error: Optional[str] = None
        for attempt in range(attempts):
            await self._limiter.acquire()
            try:
                async with session.request(
                    method, url, params=params, json=json_body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": self._auth_header,
                    },
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        last_error = f"HTTP {response.status}"
                        backoff = 0.5 * (2 ** attempt)
                        logger.warning(
                            f"⚠️ {last_error} на {entity}/{action}, повтор через {backoff:.1f}с"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if response.status >= 400:
                        body = await response.text()
                        # Раздел 8.7 справочника: логируем тело запроса при
                        # ошибках — при недокументированном API это единственный
                        # способ понять, что не так.
                        logger.error(
                            f"impulseCRM {method} {entity}/{action} -> {response.status}\n"
                            f"Запрос: params={params} json={json_body}\nОтвет: {body[:500]}"
                        )
                        raise ImpulseCRMError(
                            f"HTTP {response.status} на {entity}/{action}: {body[:300]}"
                        )

                    raw_text = await response.text()
                    if not raw_text.strip():
                        # Пустое тело при статусе <400 обычно значит, что
                        # запрос ушёл не туда (неверный IMPULSE_API_PATH,
                        # редирект на страницу входа и т.п.), а не что список
                        # пуст — пустой список list возвращает как [] в JSON,
                        # а не пустую строку.
                        logger.warning(
                            f"⚠️ impulseCRM {method} {entity}/{action} вернул пустое тело "
                            f"(HTTP {response.status}). Проверьте IMPULSE_API_PATH — похоже, "
                            f"запрос ушёл не туда."
                        )
                        return {}
                    try:
                        return json.loads(raw_text)
                    except json.JSONDecodeError:
                        logger.error(
                            f"impulseCRM {method} {entity}/{action} вернул не-JSON "
                            f"(HTTP {response.status}): {raw_text[:500]}"
                        )
                        raise ImpulseCRMError(
                            f"Не-JSON ответ на {entity}/{action}: {raw_text[:200]}"
                        )
            except asyncio.TimeoutError:
                last_error = "таймаут"
                await asyncio.sleep(0.5 * (2 ** attempt))
            except aiohttp.ClientError as e:
                last_error = f"сеть: {e}"
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise ImpulseCRMError(f"Запрос {entity}/{action} не удался ({last_error})")

    @staticmethod
    def _extract_items(response: Any) -> List[Dict[str, Any]]:
        """
        Обёртка ответа списка ПОДТВЕРЖДЕНА реальным ответом API:
        {"total": N, "items": [...]}. Остальные варианты оставлены
        запасными на случай других эндпоинтов.
        """
        if response is None:
            return []
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in ("items", "data", "list", "result", "results"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
            # Похоже на одиночную запись (например, ответ load)?
            if "id" in response:
                return [response]
        logger.warning(f"⚠️ Неожиданная форма ответа impulseCRM, ключи: {response!r}")
        return []

    @staticmethod
    def _extract_total(response: Any, fallback: int) -> int:
        if isinstance(response, dict):
            for key in ("total", "count", "total_count"):
                value = response.get(key)
                if isinstance(value, int):
                    return value
        return fallback

    async def list_(
        self,
        entity: str,
        *,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        sort: Optional[Dict[str, str]] = None,
        columns: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Выгружает ВСЕ страницы сущности.

        sort — {"поле": "asc"|"desc"}, columns — {"поле": значение} или
        {"поле": {"from": X, "to": Y}} для диапазона (формат подтверждён
        примером запроса из личного кабинета — см. докстринг модуля).
        Без columns фильтрация делается в Python после выгрузки — это
        применимо к schedule/reservation, чей набор полей не подтверждён.
        Ограничено settings.IMPULSE_MAX_PAGES страницами по
        settings.IMPULSE_PAGE_SIZE — этого с большим запасом хватает на
        масштаб одной школы.
        """
        page_size = limit or settings.IMPULSE_PAGE_SIZE
        body: Dict[str, Any] = {"limit": page_size}
        if fields:
            body["fields"] = fields
        if sort:
            body["sort"] = sort
        if columns:
            body["columns"] = columns

        all_items: List[Dict[str, Any]] = []
        # Подтверждено примером запроса: page нумеруется с 1, а не с 0.
        page = 1
        last_page = page + settings.IMPULSE_MAX_PAGES
        while page < last_page:
            body["page"] = page
            response = await self._request("POST", entity, "list", json_body=body)
            items = self._extract_items(response)
            if not items:
                break
            all_items.extend(items)
            total = self._extract_total(response, fallback=len(all_items))
            if total and len(all_items) >= total:
                break
            if len(items) < page_size:
                # Отдали меньше страницы — дальше пусто, даже если total неизвестен.
                break
            page += 1
        else:
            logger.warning(f"⚠️ Достигнут лимит страниц на {entity}/list")
        return all_items

    async def load(self, entity: str, record_id: Any) -> Optional[Dict[str, Any]]:
        response = await self._request("GET", entity, "load", params={"id": record_id})
        items = self._extract_items(response)
        for item in items:
            if str(item.get("id")) == str(record_id):
                return item
        return items[0] if items else None

    async def update(self, entity: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """update без record['id'] = создание, с id = правка (см. справочник, раздел 4)."""
        return await self._request("POST", entity, "update", json_body=record)

    async def delete(self, entity: str, record_id: Any) -> Dict[str, Any]:
        return await self._request("POST", entity, "delete", json_body={"id": record_id})

    # ==================== ТЕЛЕФОНЫ / ПОЛЬЗОВАТЕЛИ ====================

    @staticmethod
    def _phones_of(record: Dict[str, Any], field: str) -> List[str]:
        raw = record.get(field)
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw]
        if raw:
            return [str(raw)]
        return []

    async def load_all_teachers(self) -> List[Dict[str, Any]]:
        return await self.list_("teacher")

    async def load_all_clients(self) -> List[Dict[str, Any]]:
        return await self.list_("client")

    async def find_teacher_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        for teacher in await self.load_all_teachers():
            if any(
                _phone_matches(phone, p)
                for p in self._phones_of(teacher, settings.IMPULSE_FIELD_TEACHER_PHONE)
            ):
                return teacher
        return None

    async def find_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        for client in await self.load_all_clients():
            if any(
                _phone_matches(phone, p)
                for p in self._phones_of(client, settings.IMPULSE_FIELD_CLIENT_PHONE)
            ):
                return client
        return None

    async def get_teacher_info(self, teacher_id: Any) -> Optional[Dict[str, Any]]:
        record = await self.load("teacher", teacher_id)
        if record:
            return record
        for teacher in await self.load_all_teachers():
            if str(teacher.get("id")) == str(teacher_id):
                return teacher
        return None

    async def get_balances_map(self) -> Dict[Any, Dict[str, Any]]:
        """
        {client_id: {"paid": int, "used": int}} одним проходом по сущности
        абонемента — используется и get_customer_info, и планировщиком
        (там нужно посчитать это сразу у всех клиентов, и вызывать
        get_customer_info в цикле было бы N лишних полных перевыгрузок
        списка абонементов).

        Денежный баланс сюда не входит — подтверждено снятой схемой:
        он лежит прямо на client.deposit (см. get_customer_info), а не
        в сущности абонемента.
        """
        accounts = await self.list_(settings.IMPULSE_ACCOUNT_ENTITY)
        client_field = settings.IMPULSE_FIELD_ACCOUNT_CLIENT
        result: Dict[Any, Dict[str, Any]] = {}
        for a in accounts:
            # Подтверждено: ссылка на клиента — вложенный объект {id,...},
            # не плоский id (см. _ref_id).
            cid = _ref_id(a.get(client_field))
            if cid is None:
                continue
            entry = result.setdefault(cid, {"paid": 0, "used": 0})
            entry["paid"] += int(a.get(settings.IMPULSE_FIELD_ACCOUNT_PAID) or 0)
            entry["used"] += int(a.get(settings.IMPULSE_FIELD_ACCOUNT_USED) or 0)
        return result

    async def get_customer_info(self, customer_id: Any) -> Optional[Dict[str, Any]]:
        """
        Клиент + баланс. Подтверждено снятой схемой аккаунта: денежный
        баланс — поле client.deposit; остаток занятий — сущность
        абонемента (settings.IMPULSE_ACCOUNT_ENTITY, по умолчанию
        group_account). Запись обогащается полями paid_lesson_count/
        paid_count/balance/next_lesson_date/last_attend_date, чтобы
        parent.py мог читать их так же, как раньше у AlfaCRM.
        """
        client = await self.load("client", customer_id)
        if not client:
            for c in await self.load_all_clients():
                if str(c.get("id")) == str(customer_id):
                    client = c
                    break
        if not client:
            return None

        result = dict(client)
        result["balance"] = client.get(settings.IMPULSE_FIELD_CLIENT_BALANCE) or 0
        result.setdefault("paid_lesson_count", 0)
        result.setdefault("paid_count", 0)
        result.setdefault("next_lesson_date", None)
        result.setdefault("last_attend_date", None)

        try:
            balances = await self.get_balances_map()
            entry = balances.get(customer_id)
            if entry:
                result["paid_lesson_count"] = entry["paid"]
                result["paid_count"] = entry["used"]
        except ImpulseCRMError as e:
            logger.warning(
                f"⚠️ Не удалось получить остаток занятий клиента {customer_id} "
                f"из {settings.IMPULSE_ACCOUNT_ENTITY}: {e}"
            )

        return result

    # ==================== УРОКИ (schedule + reservation) ====================
    #
    # ВАЖНО (подтверждено снятой схемой вашего аккаунта): schedule — это НЕ
    # одна строка на одно фактическое занятие, а строка ПОВТОРЯЮЩЕГОСЯ
    # ПРАВИЛА (day/minutesBegin/minutesEnd/dateBegin/dateEnd), если поля
    # date/timeFrom/timeTo пусты. _expand_schedule() ниже разворачивает
    # такое правило в конкретные занятия по дням недели в пределах
    # запрошенного периода — у каждого вхождения СИНТЕТИЧЕСКИЙ id вида
    # "<id_правила>:<дата>", а не настоящий id записи CRM.
    #
    # ОТКРЫТЫЙ ВОПРОС: reservation и *_single (individual_single,
    # group_single) пусты в вашем аккаунте, поэтому неизвестно, какой
    # сущностью/экшеном impulseCRM фиксирует посещение/статус КОНКРЕТНОЙ
    # даты повторяющегося занятия. update_lesson() ниже поэтому явно
    # отказывает в записи по синтетическому id — чтобы не отправить в CRM
    # обновление, которое случайно попадёт не в ту запись или тихо
    # изменит всё повторяющееся правило целиком. Когда выяснится нужный
    # механизм (например, через поддержку impulseCRM), get_lesson/
    # update_lesson нужно будет донастроить.

    @staticmethod
    def _normalize_status(raw: Dict[str, Any]) -> int:
        value = raw.get(settings.IMPULSE_FIELD_SCHEDULE_STATUS)
        try:
            status = int(value)
            if status in settings.ALL_STATUSES:
                return status
        except (TypeError, ValueError):
            pass
        # У schedule в вашем аккаунте вообще нет поля статуса в снятой
        # схеме — по умолчанию считаем занятие запланированным, чтобы оно
        # не терялось из расписания.
        return settings.STATUS_PLANNED

    def _schedule_teacher_ids(self, raw: Dict[str, Any]) -> List[Any]:
        teacher_ids = _ref_ids(raw.get(settings.IMPULSE_FIELD_SCHEDULE_TEACHER))
        if teacher_ids:
            return teacher_ids
        # Групповое занятие: прямого teacher нет — берём из group.teacher1/teacher2.
        group_raw = raw.get(settings.IMPULSE_FIELD_SCHEDULE_GROUP)
        if isinstance(group_raw, dict):
            for f in ("teacher1", "teacher2"):
                ref = _ref_id(group_raw.get(f))
                if ref is not None:
                    teacher_ids.append(ref)
        return teacher_ids

    def _expand_schedule(
        self,
        raw: Dict[str, Any],
        reservations_by_schedule: Dict[Any, List[Any]],
        window_from: Optional[str],
        window_to: Optional[str],
    ) -> List[Dict[str, Any]]:
        schedule_id = raw.get("id")
        base = {
            "teacher_ids": self._schedule_teacher_ids(raw),
            "customer_ids": reservations_by_schedule.get(schedule_id, []),
            "status": self._normalize_status(raw),
            "topic": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_TOPIC) or "").strip(),
            "homework": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_HOMEWORK) or "").strip(),
            "note": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_NOTE) or "").strip(),
            "lesson_type_name": "Урок",
            # group_id и _target нужны, чтобы построить payload отметки
            # посещения (POST check_visits/check): target в запросе —
            # объект занятия/группы, а не id.
            "group_id": _ref_id(raw.get(settings.IMPULSE_FIELD_SCHEDULE_GROUP)),
            "_target": raw.get("target") or raw.get(settings.IMPULSE_FIELD_SCHEDULE_GROUP) or raw,
        }

        date_raw = _to_datetime_str(raw.get(settings.IMPULSE_FIELD_SCHEDULE_DATE))
        time_from_raw = _to_datetime_str(raw.get(settings.IMPULSE_FIELD_SCHEDULE_TIME_FROM))
        time_to_raw = _to_datetime_str(raw.get(settings.IMPULSE_FIELD_SCHEDULE_TIME_TO))

        if date_raw or time_from_raw:
            # Прямая (неповторяющаяся) запись — один конкретный урок,
            # настоящий id записи CRM.
            lesson = dict(base)
            lesson["id"] = schedule_id
            lesson["date"] = date_raw or time_from_raw.split(" ")[0]
            lesson["time_from"] = time_from_raw or date_raw
            lesson["time_to"] = time_to_raw
            lesson["_raw"] = raw
            lesson["_recurring"] = False
            return [lesson]

        # Повторяющееся правило.
        day = raw.get(settings.IMPULSE_FIELD_SCHEDULE_DAY)
        minutes_begin = raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_BEGIN)
        minutes_end = raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_END)
        date_begin_ts = raw.get(settings.IMPULSE_FIELD_SCHEDULE_DATE_BEGIN)
        date_end_ts = raw.get(settings.IMPULSE_FIELD_SCHEDULE_DATE_END)

        if day is None or minutes_begin is None or not date_begin_ts:
            return []  # недостаточно данных даже для разворачивания правила

        try:
            day_int = int(day)
        except (TypeError, ValueError):
            return []

        occurrence_dates = _expand_weekly(day_int, date_begin_ts, date_end_ts, window_from, window_to)
        lessons = []
        for occ_date in occurrence_dates:
            lesson = dict(base)
            lesson["id"] = f"{schedule_id}:{occ_date.isoformat()}"
            lesson["date"] = occ_date.isoformat()
            lesson["time_from"] = f"{occ_date.isoformat()} {_minutes_to_hhmm(minutes_begin)}:00"
            lesson["time_to"] = (
                f"{occ_date.isoformat()} {_minutes_to_hhmm(minutes_end)}:00"
                if minutes_end is not None else ""
            )
            lesson["_raw"] = raw
            lesson["_recurring"] = True
            lessons.append(lesson)
        return lessons

    async def _fill_group_members(self, lessons: List[Dict[str, Any]]) -> None:
        """
        Проставляет customer_ids для занятий, у которых их не дала
        reservation, используя членство в группе из абонементов
        (group_account.groups -> client). Один проход по абонементам на
        весь список занятий.
        """
        need = [l for l in lessons if not l.get("customer_ids") and l.get("group_id") is not None]
        if not need:
            return
        try:
            accounts = await self.list_(settings.IMPULSE_ACCOUNT_ENTITY)
        except ImpulseCRMError as e:
            logger.warning(f"⚠️ Не удалось получить состав групп из абонементов: {e}")
            return

        by_group: Dict[str, List[Any]] = {}
        for a in accounts:
            cid = _ref_id(a.get(settings.IMPULSE_FIELD_ACCOUNT_CLIENT))
            if cid is None:
                continue
            for g in a.get("groups") or []:
                gid = str(_ref_id(g))
                if cid not in by_group.setdefault(gid, []):
                    by_group[gid].append(cid)

        for lesson in need:
            lesson["customer_ids"] = list(by_group.get(str(lesson["group_id"]), []))

    async def _reservations_by_schedule(self) -> Dict[Any, List[Any]]:
        reservations = await self.list_("reservation")
        by_schedule: Dict[Any, List[Any]] = {}
        s_field = settings.IMPULSE_FIELD_RESERVATION_SCHEDULE
        c_field = settings.IMPULSE_FIELD_RESERVATION_CLIENT
        for r in reservations:
            schedule_ref = _ref_id(r.get(s_field))
            client_ref = _ref_id(r.get(c_field))
            if schedule_ref is None or client_ref is None:
                continue
            by_schedule.setdefault(schedule_ref, []).append(client_ref)
        return by_schedule

    async def get_lessons(
        self,
        teacher_id: Optional[Any] = None,
        customer_id: Optional[Any] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Уроки за период в нормализованной форме (см. докстринг модуля и
        блок УРОКИ выше про разворачивание повторяющихся правил).

        Фильтрация по teacher_id/customer_id/status делается в Python
        после выгрузки — состав полей schedule не подтверждён вендором
        настолько, чтобы доверять серверному фильтру `columns`. Даты,
        наоборот, обязательны для разворачивания повторяющихся правил —
        без окна разворачивать пришлось бы на всю историю, поэтому при
        отсутствии date_from/date_to подставляется окно кеша
        (settings.CACHE_DAYS_BACK/FORWARD).
        """
        window_from = date_from or (
            settings.today() - timedelta(days=settings.CACHE_DAYS_BACK)
        ).isoformat()
        window_to = date_to or (
            settings.today() + timedelta(days=settings.CACHE_DAYS_FORWARD)
        ).isoformat()

        reservations_by_schedule = await self._reservations_by_schedule()
        raw_schedules = await self.list_("schedule")

        lessons: List[Dict[str, Any]] = []
        for raw in raw_schedules:
            lessons.extend(self._expand_schedule(raw, reservations_by_schedule, window_from, window_to))

        # reservation в этом аккаунте пуста, поэтому состав группового
        # занятия достаём из абонементов (group_account.groups) — одним
        # проходом на все занятия, а не по запросу на каждое.
        await self._fill_group_members(lessons)

        if teacher_id is not None:
            lessons = [l for l in lessons if teacher_id in l["teacher_ids"]]
        if customer_id is not None:
            lessons = [l for l in lessons if customer_id in l["customer_ids"]]
        if status is not None:
            lessons = [l for l in lessons if l["status"] == status]
        if date_from or date_to:
            lessons = [
                l for l in lessons
                if _date_key(l["date"] or l["time_from"])
                and (not date_from or _date_key(l["date"] or l["time_from"]) >= date_from)
                and (not date_to or _date_key(l["date"] or l["time_from"]) <= date_to)
            ]

        logger.info(f"📊 Получено уроков из API: {len(lessons)} ({date_from} – {date_to})")
        return lessons

    async def get_lesson(self, lesson_id: Any) -> Optional[Dict[str, Any]]:
        reservations_by_schedule = await self._reservations_by_schedule()

        if isinstance(lesson_id, str) and ":" in lesson_id:
            raw_id, _, date_part = lesson_id.partition(":")
            raw = await self.load("schedule", raw_id)
            if not raw:
                return None
            occurrences = self._expand_schedule(raw, reservations_by_schedule, date_part, date_part)
            if not occurrences:
                return None
            await self._fill_group_members(occurrences)
            return occurrences[0]

        raw = await self.load("schedule", lesson_id)
        if not raw:
            return None
        occurrences = self._expand_schedule(raw, reservations_by_schedule, None, None)
        if not occurrences:
            return None
        await self._fill_group_members(occurrences)
        return occurrences[0]

    async def update_lesson(
        self,
        lesson_id: Any,
        updates: Dict[str, Any],
        current: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Обновляет занятие, отправляя ПОЛНУЮ сырую модель записи (см.
        докстринг модуля про неподтверждённую семантику update) —
        частичный payload рискует либо не пройти валидацию, либо (в
        худшем случае) обнулить поля, которых не было в запросе.
        """
        if isinstance(lesson_id, str) and ":" in lesson_id:
            # См. блок УРОКИ выше: это вычисленное вхождение повторяющегося
            # правила, не отдельная запись CRM — писать в неё нельзя, пока
            # не выяснен настоящий механизм фиксации посещения/статуса
            # конкретной даты.
            raise ImpulseCRMError(
                "Это занятие — вхождение повторяющегося группового "
                "расписания, а не отдельная запись в CRM. impulseCRM пока "
                "не показал, как отмечать посещение/статус конкретной даты "
                "такого занятия (reservation и *_single пусты в вашем "
                "аккаунте) — уточните у поддержки impulseCRM."
            )

        if current is None:
            current = await self.get_lesson(lesson_id)
        if not current:
            raise ImpulseCRMError(f"Занятие {lesson_id} не найдено в CRM")

        raw = dict(current.get("_raw") or {"id": lesson_id})
        field_map = {
            "status": settings.IMPULSE_FIELD_SCHEDULE_STATUS,
            "homework": settings.IMPULSE_FIELD_SCHEDULE_HOMEWORK,
            "topic": settings.IMPULSE_FIELD_SCHEDULE_TOPIC,
            "note": settings.IMPULSE_FIELD_SCHEDULE_NOTE,
            "date": settings.IMPULSE_FIELD_SCHEDULE_DATE,
            "time_from": settings.IMPULSE_FIELD_SCHEDULE_TIME_FROM,
            "time_to": settings.IMPULSE_FIELD_SCHEDULE_TIME_TO,
        }
        for key, value in updates.items():
            raw[field_map.get(key, key)] = value
        raw["id"] = lesson_id

        await self.update("schedule", raw)
        logger.info(f"✅ Занятие {lesson_id} обновлено: {list(updates.keys())}")

        merged = dict(current)
        merged.update(updates)
        merged["_raw"] = raw
        return merged

    async def mark_lesson_conducted(
        self, lesson_id: Any, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(
            lesson_id, {"status": settings.STATUS_CONDUCTED}, current=current
        )

    async def set_homework(
        self, lesson_id: Any, homework_text: str, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(lesson_id, {"homework": homework_text}, current=current)

    # ==================== ПОСЕЩЕНИЯ (внутренний API check_visits) ====================
    #
    # Эндпоинты найдены разбором фронтенда impulseCRM, вендором НЕ
    # документированы — см. большой комментарий в settings.py. Всё, что
    # пишет данные, работает только при IMPULSE_CHECK_VISITS_ENABLED=true.

    async def _internal_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{settings.IMPULSE_INTERNAL_PATH.format(path=path)}"
        await self._limiter.acquire()
        async with session.request(
            method, url, params=params, json=json_body,
            headers=self._internal_headers(),
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise ImpulseCRMError(
                    f"HTTP {response.status} на внутреннем {path}: {text[:300]}"
                )
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Самый вероятный симптом «API-ключ не годится для
                # внутреннего API»: вместо JSON приходит HTML страницы входа.
                raise ImpulseCRMError(
                    f"Внутренний {path} вернул не-JSON (вероятно, страница "
                    f"входа — API-ключ не подходит для внутреннего API): {text[:200]}"
                )

    def _internal_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Фронтенд ходит XHR-ом; по этому заголовку бэкенд отдаёт JSON,
            # а не HTML-редирект на страницу входа.
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        # Ключ отправляем ВСЕГДА, cookie — дополнительно, если задана.
        # Раньше cookie ЗАМЕЩАЛА ключ, и при протухшей cookie запрос уходил
        # вообще без авторизации, давая 401 даже там, где ключ подошёл бы.
        headers["Authorization"] = self._auth_header
        if settings.IMPULSE_SESSION_COOKIE:
            headers["Cookie"] = settings.IMPULSE_SESSION_COOKIE
        return headers

    async def get_visits(self, date_ts: int, **extra: Any) -> Any:
        """
        Список отметок посещения за дату. Только чтение.

        ПОДТВЕРЖДЕНО: внутренние эндпоинты impulseCRM отвечают только на
        POST — на GET Symfony возвращает MethodNotAllowedHttpException
        (Allow: POST). Раньше здесь был GET, и он не мог работать.
        """
        body = {"date": date_ts}
        body.update(extra)
        return await self._internal_request(
            "POST", settings.IMPULSE_PATH_VISITS, json_body=body
        )

    async def get_last_accounts(self, client_id: Any) -> Any:
        """
        Абонементы клиента через внутренний API
        (POST /api/client/last_accounts) — то, чем пользуется сам
        веб-интерфейс на странице отметки посещений. Полезнее, чем
        сканировать весь список group_account, но требует доступа к
        внутреннему API (см. README, раздел 3).
        """
        return await self._internal_request(
            "POST", settings.IMPULSE_PATH_LAST_ACCOUNTS,
            json_body={"clientId": client_id, "client_id": client_id},
        )

    async def check_visit(
        self,
        client_id: Any,
        account: Dict[str, Any],
        target: Dict[str, Any],
        date_ts: int,
        force: bool = False,
        minutes_begin: Any = None,
        duration: Any = None,
        hall_id: Any = None,
        branch_id: Any = None,
    ) -> Any:
        """
        Отметить клиента присутствующим.

        Форма payload взята из РЕАЛЬНОГО запроса, снятого в DevTools, и
        отличается от той, что предполагалась по разобранному коду
        фронтенда: поля называются clientId/service (а не
        client_id/account), и дополнительно передаются branchId,
        minutesBegin, duration, hallId. `service` — это ссылка на
        абонемент в краткой форме {id, number, entity}.
        """
        if not settings.IMPULSE_CHECK_VISITS_ENABLED:
            raise ImpulseCRMError(
                "Отметка посещений выключена (IMPULSE_CHECK_VISITS_ENABLED=false). "
                "Сначала проверьте внутренний API скриптом impulse_probe.py."
            )
        payload: Dict[str, Any] = {
            "date": date_ts,
            "service": _service_ref(account),
            "target": target,
            "clientId": client_id,
        }
        if branch_id is not None or settings.BRANCH_ID:
            payload["branchId"] = branch_id or _as_int(settings.BRANCH_ID)
        if minutes_begin is not None:
            payload["minutesBegin"] = minutes_begin
        if duration is not None:
            payload["duration"] = duration
        if hall_id is not None:
            payload["hallId"] = hall_id
        if force:
            payload["force"] = True
        return await self._internal_request(
            "POST", settings.IMPULSE_PATH_CHECK, json_body=payload
        )

    async def burn_visit(
        self,
        client_id: Any,
        account: Dict[str, Any],
        target: Dict[str, Any],
        date_ts: int,
        target_values: Optional[Dict[str, Any]] = None,
        reservation: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Снять отметку посещения (POST check_visits/burn_one)."""
        if not settings.IMPULSE_CHECK_VISITS_ENABLED:
            raise ImpulseCRMError(
                "Отметка посещений выключена (IMPULSE_CHECK_VISITS_ENABLED=false)."
            )
        payload: Dict[str, Any] = {
            "client_id": client_id,
            "account": account,
            "target": target,
            "date": date_ts,
        }
        if target_values:
            payload["targetValues"] = target_values
        if reservation:
            payload["reservation"] = reservation
        return await self._internal_request(
            "POST", settings.IMPULSE_PATH_BURN, json_body=payload
        )

    # ==================== СОСТАВ ГРУППЫ / АБОНЕМЕНТЫ ====================

    async def get_accounts_by_client(self) -> Dict[Any, List[Dict[str, Any]]]:
        """{client_id: [абонементы]} — сырые записи, нужны как объект
        `account` в payload отметки посещения."""
        accounts = await self.list_(settings.IMPULSE_ACCOUNT_ENTITY)
        result: Dict[Any, List[Dict[str, Any]]] = {}
        for a in accounts:
            cid = _ref_id(a.get(settings.IMPULSE_FIELD_ACCOUNT_CLIENT))
            if cid is not None:
                result.setdefault(cid, []).append(a)
        return result

    @staticmethod
    def pick_active_account(accounts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Выбирает действующий абонемент: сперва active и с остатком занятий."""
        if not accounts:
            return None
        for a in accounts:
            if a.get("active") and int(a.get("trainingsLeft") or 0) > 0:
                return a
        for a in accounts:
            if a.get("active"):
                return a
        return accounts[0]

    async def get_group_client_ids(self, group_id: Any) -> List[Any]:
        """
        Ученики группы. reservation в этом аккаунте пуста, поэтому состав
        группы берётся из абонементов: group_account.groups содержит
        ссылки на группы, к которым привязан абонемент клиента.
        """
        if group_id is None:
            return []
        accounts = await self.list_(settings.IMPULSE_ACCOUNT_ENTITY)
        client_ids: List[Any] = []
        for a in accounts:
            for g in a.get("groups") or []:
                if str(_ref_id(g)) == str(group_id):
                    cid = _ref_id(a.get(settings.IMPULSE_FIELD_ACCOUNT_CLIENT))
                    if cid is not None and cid not in client_ids:
                        client_ids.append(cid)
                    break
        return client_ids

    # ==================== УТИЛИТЫ ====================

    @staticmethod
    def extract_user_name(user: Dict[str, Any]) -> str:
        """
        ФИО из трёх отдельных полей (подтверждено примером запроса из
        личного кабинета: lastName/name/middleName у client — предполагаем
        ту же схему у teacher, см. settings.IMPULSE_FIELD_TEACHER_*).
        Русский порядок: Фамилия Имя Отчество. Если ни одно из трёх полей
        не заполнено, отступаем к одиночным полям name/fullName на случай
        другой схемы у конкретного аккаунта.
        """
        for last_f, first_f, middle_f in (
            (
                settings.IMPULSE_FIELD_CLIENT_LAST_NAME,
                settings.IMPULSE_FIELD_CLIENT_FIRST_NAME,
                settings.IMPULSE_FIELD_CLIENT_MIDDLE_NAME,
            ),
            (
                settings.IMPULSE_FIELD_TEACHER_LAST_NAME,
                settings.IMPULSE_FIELD_TEACHER_FIRST_NAME,
                settings.IMPULSE_FIELD_TEACHER_MIDDLE_NAME,
            ),
        ):
            parts = [
                str(user.get(f) or "").strip() for f in (last_f, first_f, middle_f)
            ]
            full = " ".join(p for p in parts if p)
            if full:
                return full

        for field in ("name", "fullName", "full_name", "title"):
            value = str(user.get(field) or "").strip()
            if value:
                return value
        return "Без имени"

    def extract_user_phone(self, user: Dict[str, Any]) -> str:
        for field in (settings.IMPULSE_FIELD_CLIENT_PHONE, settings.IMPULSE_FIELD_TEACHER_PHONE):
            phones = self._phones_of(user, field)
            if phones:
                return phones[0]
        return "Нет телефона"

    @staticmethod
    def get_lesson_status_label(status: int) -> str:
        return STATUS_LABELS.get(status, f"статус {status}")
