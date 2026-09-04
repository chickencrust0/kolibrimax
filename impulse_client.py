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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

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


def _short_error(body: str) -> str:
    """
    Короткое описание ошибки вместо сырого тела ответа.

    При 404 impulseCRM отдаёт HTML-страницу, и она целиком уезжала в чат
    родителю: «Ошибка: HTTP 404 ... <!DOCTYPE html> <html> <head> ...».
    Здесь HTML сворачивается в одну осмысленную фразу.
    """
    text = (body or "").strip()
    if text.startswith("<"):
        return (
            "сервер вернул HTML-страницу ошибки вместо JSON — вероятно, "
            "такого метода нет по этому пути (проверьте IMPULSE_API_PATH)"
        )
    text = " ".join(text.split())
    return text[:200] if text else "пустой ответ"


class ImpulseCRMError(Exception):
    """status — HTTP-код, если ошибка пришла от сервера."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


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


# Служебные ветки записи: в них связей не бывает, а вложенность большая.
_SKIP_BRANCHES = ("creator", "updater", "branch", "settings", "status", "pipeline")


def _collect_refs(node: Any, entity: str, depth: int = 0) -> List[Any]:
    """
    Рекурсивно собирает id всех вложенных объектов с заданным "entity".

    Зачем не читать конкретное поле по имени: impulseCRM не публикует
    состав полей, и связь «абонемент → группа» может лежать не только в
    `groups`, но и в `packets`, `clientSubscription` или ещё где-то —
    это зависит от того, как школа завела данные. Зато ЛЮБАЯ ссылка на
    другую сущность приходит вложенным объектом {"id": ..., "entity":
    "group", ...}, и по этому признаку её можно найти, как бы поле ни
    называлось.

    Глубина ограничена: объекты вкладываются друг в друга (абонемент →
    клиент → статус → воронка), и без ограничения обход уходит далеко от
    сути и начинает возвращать чужие связи.
    """
    if depth > 3:
        return []

    found: List[Any] = []

    if isinstance(node, dict):
        if node.get("entity") == entity:
            ref = node.get("id")
            # Внутрь найденного объекта не спускаемся: у вложенного
            # клиента может быть свой список групп, и это уже не наша связь.
            return [ref] if ref is not None else []
        for key, value in node.items():
            if key in _SKIP_BRANCHES:
                continue
            found.extend(_collect_refs(value, entity, depth + 1))

    elif isinstance(node, (list, tuple)):
        for value in node:
            found.extend(_collect_refs(value, entity, depth + 1))

    return found


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


def date_to_ts(day) -> int:
    """
    Дата -> Unix-timestamp полуночи **UTC**.

    Именно так impulseCRM хранит все даты: во всех ответах API значения
    дат нацело делятся на 86400 (visitDate=1788566400 -> 2026-09-05
    00:00:00 UTC, dateBegin, beginDate и т.д.).

    Раньше здесь бралась полночь по часовому поясу филиала, и метка
    уезжала на смещение пояса (для Москвы — на 3 часа назад). Сервер не
    находил занятия на такую дату, МОЛЧА ничего не делал и возвращал
    200 — посещение в CRM не появлялось, а бот считал, что всё прошло.
    """
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    )


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


def _same_id(a: Any, b: Any) -> bool:
    """
    Сравнение id из разных источников. SQLite отдаёт crm_id как int,
    CRM — то int, то строку; прежнее `customer_id in l["customer_ids"]`
    молча не срабатывало при расхождении типа, и расписание родителя
    оказывалось пустым даже при корректных данных.
    """
    if a is None or b is None:
        return False
    return str(a) == str(b)


class _TTLCache:
    """Справочники меняются редко (раздел 8.5 справочника по API)."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: Dict[str, Any] = {}
        self._stamps: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get(self, key: str, loader):
        now = time.monotonic()
        if key in self._data and (now - self._stamps[key]) < self.ttl:
            return self._data[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if key in self._data and (now - self._stamps[key]) < self.ttl:
                return self._data[key]
            value = await loader()
            self._data[key] = value
            self._stamps[key] = time.monotonic()
            return value

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._data.clear()
            self._stamps.clear()
        else:
            self._data.pop(key, None)
            self._stamps.pop(key, None)


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
        self._lookups = _TTLCache(settings.IMPULSE_LOOKUP_TTL)
        # Ставится в True, если сервер ответил 404 на экшен load: в части
        # установок impulseCRM его нет, и повторять попытки бессмысленно.
        self._load_unsupported = False

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
                            f"HTTP {response.status} на {entity}/{action}: "
                            f"{_short_error(body)}",
                            status=response.status,
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
        Формат обёртки ответа списка не подтверждён вендором — перебираем
        несколько вероятных вариантов вместо падения на неожиданной форме.
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

    async def _cached_list(self, entity: str) -> List[Dict[str, Any]]:
        """Полный список сущности с коротким кешем (см. _TTLCache)."""
        return await self._lookups.get(entity, lambda: self.list_(entity))

    async def _scan_for(self, entity: str, record_id: Any) -> Optional[Dict[str, Any]]:
        for item in await self._cached_list(entity):
            if _same_id(item.get("id"), record_id):
                return item
        return None

    async def load(self, entity: str, record_id: Any) -> Optional[Dict[str, Any]]:
        """
        Одна запись по id.

        Экшен `load` — единственный, работающий по GET (справочник,
        раздел 4). Но в некоторых установках impulseCRM его вовсе нет:
        сервер отдаёт HTML-страницу 404, исключение всплывало наружу, и
        родитель получал в чат кусок HTML вместо баланса.

        Поэтому при 404 бот запоминает, что `load` в этом аккаунте
        недоступен, и дальше берёт запись из общего списка — тот
        кешируется, так что повторных полных выгрузок не будет. Один раз
        пишем об этом в лог, чтобы поведение не выглядело загадочным.
        """
        if self._load_unsupported:
            return await self._scan_for(entity, record_id)

        try:
            response = await self._request(
                "GET", entity, "load", params={"id": record_id}
            )
        except ImpulseCRMError as e:
            if e.status not in (404, 405, 501):
                raise
            self._load_unsupported = True
            logger.warning(
                f"⚠️ Экшен load недоступен в этом аккаунте ({entity}/load → "
                f"HTTP {e.status}). Переключаюсь на поиск по списку — "
                "работать будет, просто чуть медленнее."
            )
            return await self._scan_for(entity, record_id)

        items = self._extract_items(response)
        for item in items:
            if _same_id(item.get("id"), record_id):
                return item
        if items:
            return items[0]
        # load отработал, но записи нет — вдруг она есть в списке.
        return await self._scan_for(entity, record_id)

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
        return await self._cached_list("teacher")

    async def load_all_clients(self) -> List[Dict[str, Any]]:
        return await self._cached_list("client")

    async def load_all_accounts(self) -> List[Dict[str, Any]]:
        """
        Абонементы всех типов одним списком. У каждой записи проставляется
        служебное поле "_entity" — по нему видно, из какой сущности она
        пришла (group_account / individual_account / ...).

        Отсутствующая или пустая сущность не считается ошибкой: в вашем
        аккаунте заполнен только group_account, но у другой школы это
        может быть individual_account.
        """
        async def loader():
            collected: List[Dict[str, Any]] = []
            for entity in settings.IMPULSE_ACCOUNT_ENTITIES:
                try:
                    items = await self.list_(entity)
                except ImpulseCRMError as e:
                    logger.warning(f"⚠️ Абонементы {entity} недоступны: {e}")
                    continue
                for item in items:
                    item = dict(item)
                    item["_entity"] = entity
                    collected.append(item)
            return collected

        return await self._lookups.get("accounts", loader)

    def invalidate_lookups(self) -> None:
        self._lookups.invalidate()

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

    # ==================== АБОНЕМЕНТЫ И ОСТАТОК ЗАНЯТИЙ ====================
    #
    # Баланс в этом боте — это ОСТАТОК ЗАНЯТИЙ В АБОНЕМЕНТЕ, а не деньги.
    # CRM считает остаток сама (поле trainingsLeft) — берём его, а не
    # разность trainingsTotal - trainingsUsed: при заморозке, продлении и
    # подарочных занятиях CRM учитывает их в trainingsLeft, а арифметика
    # total-used их теряет. Разность остаётся запасным вариантом, если
    # поля trainingsLeft в аккаунте нет.

    @staticmethod
    def _is_account_active(account: Dict[str, Any]) -> bool:
        """
        Сгоревшие и закрытые абонементы не должны попадать в остаток —
        иначе родитель увидит занятия от прошлогоднего абонемента.
        """
        if account.get("deleted") or account.get("archived"):
            return False
        if account.get(settings.IMPULSE_FIELD_ACCOUNT_CLOSED):
            return False
        active = account.get(settings.IMPULSE_FIELD_ACCOUNT_ACTIVE)
        # Поля active может не быть — тогда считаем абонемент действующим,
        # чтобы не занулить баланс на аккаунте с другой схемой.
        return True if active is None else bool(active)

    @staticmethod
    def _account_lessons_left(account: Dict[str, Any]) -> int:
        left = account.get(settings.IMPULSE_FIELD_ACCOUNT_LEFT)
        if left is not None:
            try:
                return max(0, int(left))
            except (TypeError, ValueError):
                pass
        try:
            total = int(account.get(settings.IMPULSE_FIELD_ACCOUNT_PAID) or 0)
            used = int(account.get(settings.IMPULSE_FIELD_ACCOUNT_USED) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, total - used)

    async def get_subscriptions(self, client_id: Any) -> List[Dict[str, Any]]:
        """
        Действующие абонементы клиента в нормализованной форме:
        {name, left, total, used, frozen, end_date, days_left, entity}.
        """
        accounts = await self.load_all_accounts()
        client_field = settings.IMPULSE_FIELD_ACCOUNT_CLIENT
        result: List[Dict[str, Any]] = []
        for a in accounts:
            if not _same_id(_ref_id(a.get(client_field)), client_id):
                continue
            if not self._is_account_active(a):
                continue
            try:
                total = int(a.get(settings.IMPULSE_FIELD_ACCOUNT_PAID) or 0)
            except (TypeError, ValueError):
                total = 0
            try:
                used = int(a.get(settings.IMPULSE_FIELD_ACCOUNT_USED) or 0)
            except (TypeError, ValueError):
                used = 0
            result.append({
                "id": a.get("id"),
                "entity": a.get("_entity"),
                "name": a.get(settings.IMPULSE_FIELD_ACCOUNT_TYPE_NAME) or "Абонемент",
                "left": self._account_lessons_left(a),
                "total": total,
                "used": used,
                "frozen": bool(a.get(settings.IMPULSE_FIELD_ACCOUNT_FREEZE)),
                "end_date": _date_key(a.get(settings.IMPULSE_FIELD_ACCOUNT_END_DATE)),
                "days_left": a.get(settings.IMPULSE_FIELD_ACCOUNT_DAYS_LEFT),
            })
        result.sort(key=lambda x: (x["end_date"] or "9999-99-99", x["name"]))
        return result

    async def get_balances_map(self) -> Dict[str, Dict[str, Any]]:
        """
        {client_id (строкой): {"left": int, "paid": int, "used": int}} одним
        проходом по всем сущностям абонементов — планировщику нужно
        посчитать остаток сразу у всех клиентов, и вызывать
        get_customer_info в цикле означало бы N полных перевыгрузок.

        Ключи ПРИВЕДЕНЫ К СТРОКЕ: crm_id в SQLite — int, id из CRM бывает
        и int, и строкой; раньше при расхождении типов lookup молча
        промахивался и баланс показывался нулевым.

        Денежный баланс сюда не входит: он лежит прямо на client.deposit.
        """
        accounts = await self.load_all_accounts()
        client_field = settings.IMPULSE_FIELD_ACCOUNT_CLIENT
        result: Dict[str, Dict[str, Any]] = {}
        for a in accounts:
            cid = _ref_id(a.get(client_field))
            if cid is None or not self._is_account_active(a):
                continue
            entry = result.setdefault(str(cid), {"left": 0, "paid": 0, "used": 0})
            entry["left"] += self._account_lessons_left(a)
            try:
                entry["paid"] += int(a.get(settings.IMPULSE_FIELD_ACCOUNT_PAID) or 0)
                entry["used"] += int(a.get(settings.IMPULSE_FIELD_ACCOUNT_USED) or 0)
            except (TypeError, ValueError):
                pass
        return result

    async def get_customer_info(self, customer_id: Any) -> Optional[Dict[str, Any]]:
        """
        Клиент + остаток занятий. Поля lessons_left / paid_lesson_count /
        paid_count / subscriptions читает bot/handlers/parent.py.
        """
        client = await self.load("client", customer_id)
        if not client:
            for c in await self.load_all_clients():
                if _same_id(c.get("id"), customer_id):
                    client = c
                    break
        if not client:
            return None

        result = dict(client)
        result["balance"] = client.get(settings.IMPULSE_FIELD_CLIENT_BALANCE) or 0
        result["lessons_left"] = 0
        result["paid_lesson_count"] = 0
        result["paid_count"] = 0
        result["subscriptions"] = []

        try:
            subscriptions = await self.get_subscriptions(customer_id)
            result["subscriptions"] = subscriptions
            result["lessons_left"] = sum(s["left"] for s in subscriptions)
            result["paid_lesson_count"] = sum(s["total"] for s in subscriptions)
            result["paid_count"] = sum(s["used"] for s in subscriptions)
        except ImpulseCRMError as e:
            logger.warning(
                f"⚠️ Не удалось получить остаток занятий клиента {customer_id}: {e}"
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

    @staticmethod
    def _schedule_target(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Объект «куда отмечать посещение» для check_visits.

        CRM отвечает «Выберите куда отмечать посещение», если это поле
        пустое, поэтому берём первый непустой объект из target/group и
        отдаём его ЦЕЛИКОМ — фронтенд кладёт в запрос полный объект, а не
        ссылку {id}. Для индивидуальных занятий целью выступает клиент.
        """
        for field in (
            settings.IMPULSE_FIELD_SCHEDULE_TARGET,
            settings.IMPULSE_FIELD_SCHEDULE_GROUP,
            settings.IMPULSE_FIELD_SCHEDULE_CLIENT,
        ):
            value = raw.get(field)
            if isinstance(value, dict) and value.get("id") is not None:
                return value
        return {}

    @staticmethod
    def _schedule_group_id(raw: Dict[str, Any]) -> Any:
        """
        id группы занятия. Смотрим и в `group`, и в `target`: у групповых
        занятий в снятой схеме заполнены оба, но у части записей может
        быть только target (там лежит «цель» — группа или клиент).
        """
        for field in (
            settings.IMPULSE_FIELD_SCHEDULE_GROUP,
            settings.IMPULSE_FIELD_SCHEDULE_TARGET,
        ):
            value = raw.get(field)
            if isinstance(value, dict) and value.get("entity") in (None, "group"):
                ref = value.get("id")
                if ref is not None:
                    return ref
        return None

    @staticmethod
    def _schedule_style_name(raw: Dict[str, Any]) -> str:
        """
        Название направления. У schedule поле style пустое, зато оно есть
        у вложенной группы — без этого все занятия в расписании
        назывались одинаково («Урок») и различить их было невозможно.
        """
        direct = raw.get("style")
        if isinstance(direct, dict) and direct.get("name"):
            return str(direct["name"])
        for field in (
            settings.IMPULSE_FIELD_SCHEDULE_GROUP,
            settings.IMPULSE_FIELD_SCHEDULE_TARGET,
        ):
            container = raw.get(field)
            if isinstance(container, dict):
                style = container.get(settings.IMPULSE_FIELD_GROUP_STYLE)
                if isinstance(style, dict) and style.get("name"):
                    return str(style["name"])
        return "Занятие"

    @staticmethod
    def _customers_for(
        index: Dict[str, Any],
        schedule_id: Any,
        group_id: Any,
        direct_refs: List[Any],
        occ_date: str,
    ) -> List[Any]:
        """
        Кто ходит на КОНКРЕТНОЕ занятие конкретной даты.

        Источники неравноценны, и это главное:

          ТОЧНЫЕ — запись ученика на занятие (reservation, разовые
          посещения, прямая ссылка schedule.client). Здесь сказано
          именно то, на что ученик записан.

          ГРУБЫЙ — членство в группе через абонемент. Он говорит лишь
          «ученик относится к этой группе», но у группы может быть
          несколько занятий в неделю, а ходит ребёнок не на все.

        Поэтому источники НЕ складываются. Если про ученика есть хоть
        одна точная запись, для него берутся только точные — грубый
        источник к нему не применяется вовсе. Членство в группе
        используется лишь для тех, про кого точных записей нет совсем.

        Раньше оба источника объединялись, и ребёнок, записанный на два
        занятия из четырёх, видел в расписании все четыре.

        Точная запись может быть привязана к дате (ключ "116:2026-09-01")
        или ко всему правилу (ключ "116") — проверяются оба.
        """
        # .get, а не [...]: индекс собирается в одном месте, но передаётся
        # снаружи, и отсутствие ключа не должно ронять всё расписание.
        by_schedule: Dict[str, List[Any]] = index.get("by_schedule") or {}
        by_group: Dict[str, List[Any]] = index.get("by_group") or {}
        precise: Set[str] = index.get("precise_clients") or set()

        result: List[Any] = []
        seen: Set[str] = set()

        def add(candidate: Any) -> None:
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                result.append(candidate)

        for candidate in (
            list(by_schedule.get(f"{schedule_id}:{occ_date}", []))
            + list(by_schedule.get(str(schedule_id), []))
            + list(direct_refs)
        ):
            add(candidate)

        if group_id is not None and settings.STRICT_ENROLLMENT:
            for candidate in by_group.get(str(group_id), []):
                if str(candidate) not in precise:
                    add(candidate)
        elif group_id is not None:
            for candidate in by_group.get(str(group_id), []):
                add(candidate)

        return result

    def _expand_schedule(
        self,
        raw: Dict[str, Any],
        index: Dict[str, Dict[Any, List[Any]]],
        window_from: Optional[str],
        window_to: Optional[str],
    ) -> List[Dict[str, Any]]:
        schedule_id = raw.get("id")
        group_id = self._schedule_group_id(raw)
        direct_refs = _ref_ids(raw.get(settings.IMPULSE_FIELD_SCHEDULE_CLIENT))

        base = {
            "teacher_ids": self._schedule_teacher_ids(raw),
            "group_id": group_id,
            # Полный объект цели — нужен для check_visits/check.
            "_target": self._schedule_target(raw),
            "status": self._normalize_status(raw),
            "topic": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_TOPIC) or "").strip(),
            "homework": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_HOMEWORK) or "").strip(),
            "note": (raw.get(settings.IMPULSE_FIELD_SCHEDULE_NOTE) or "").strip(),
            "lesson_type_name": self._schedule_style_name(raw),
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
            lesson["customer_ids"] = self._customers_for(
                index, schedule_id, group_id, direct_refs, lesson["date"]
            )
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
            # Состав учеников считается ДЛЯ КАЖДОЙ ДАТЫ отдельно, а не один
            # раз на правило: запись на занятие может быть сделана на
            # конкретное число, и тогда остальные даты того же правила
            # ученику показывать не нужно.
            lesson["customer_ids"] = self._customers_for(
                index, schedule_id, group_id, direct_refs, occ_date.isoformat()
            )
            lesson["time_from"] = f"{occ_date.isoformat()} {_minutes_to_hhmm(minutes_begin)}:00"
            lesson["time_to"] = (
                f"{occ_date.isoformat()} {_minutes_to_hhmm(minutes_end)}:00"
                if minutes_end is not None else ""
            )
            lesson["_raw"] = raw
            lesson["_recurring"] = True
            lessons.append(lesson)
        return lessons

    async def _customer_index(self) -> Dict[str, Dict[Any, List[Any]]]:
        """
        Две карты принадлежности учеников:
          by_schedule — {schedule_id: [client_id]} — прямые записи;
          by_group    — {group_id: [client_id]}    — членство в группе.

        Источники перебираются ВСЕ, потому что impulseCRM позволяет
        завести данные по-разному, и какой вариант используется в
        конкретной школе — из документации не следует:
          * reservation           — штатная запись на групповое занятие;
          * *_account (абонементы) — абонемент, привязанный к группе;
          * *_single (разовые)     — разовое посещение;
          * schedule.client        — индивидуальное занятие.

        Имена полей НЕ угадываются: ссылки ищутся по вложенным объектам
        с "entity" (см. _collect_refs). Прежняя версия читала одно поле
        `groups` у одной сущности — и если школа завела связь иначе,
        расписание у родителей молча оставалось пустым.
        """
        async def loader():
            # Ключи — СТРОКИ. Точная запись может быть привязана к
            # конкретной дате — тогда ключ вида "116:2026-09-01".
            by_schedule: Dict[str, List[Any]] = {}
            by_group: Dict[str, List[Any]] = {}
            precise_clients: Set[str] = set()
            stats: Dict[str, int] = {}

            def remember(target: Dict[str, List[Any]], key: Any, client_id: Any) -> None:
                bucket = target.setdefault(str(key), [])
                if not any(_same_id(existing, client_id) for existing in bucket):
                    bucket.append(client_id)

            def remember_precise(key: Any, client_id: Any, source: str) -> None:
                remember(by_schedule, key, client_id)
                precise_clients.add(str(client_id))
                stats[source] = stats.get(source, 0) + 1

            def record_date(record: Dict[str, Any]) -> Optional[str]:
                """Дата точной записи, если она указана."""
                for field in ("date", "day", "dateBegin", "visitDate", "trainingDate"):
                    value = record.get(field)
                    if value in (None, "", 0):
                        continue
                    parsed = _date_key(_to_datetime_str(value))
                    if parsed and len(parsed) == 10:
                        return parsed
                return None

            # 1. reservation — штатная запись на занятие. Самый точный источник.
            try:
                reservations = await self.list_("reservation")
            except ImpulseCRMError as e:
                logger.warning(f"⚠️ Сущность reservation недоступна: {e}")
                reservations = []
            for r in reservations:
                clients = _collect_refs(r, "client")
                if not clients:
                    continue
                occ_date = record_date(r)
                for schedule_ref in _collect_refs(r, "schedule"):
                    key = f"{schedule_ref}:{occ_date}" if occ_date else schedule_ref
                    for client_ref in clients:
                        remember_precise(key, client_ref, "reservation")

            # 2. Абонементы и разовые посещения.
            #
            # У одной записи может быть и ссылка на занятие, и ссылка на
            # группу. Ссылка на занятие точнее, поэтому если она есть, то
            # группа из этой же записи НЕ берётся — иначе абонемент,
            # выписанный на два занятия из четырёх, открывал бы ученику
            # все четыре занятия группы.
            for record, entity in await self._membership_records():
                clients = _collect_refs(record, "client")
                if not clients:
                    continue

                schedule_refs = _collect_refs(record, "schedule")
                if schedule_refs:
                    occ_date = record_date(record)
                    for schedule_ref in schedule_refs:
                        key = f"{schedule_ref}:{occ_date}" if occ_date else schedule_ref
                        for client_ref in clients:
                            remember_precise(key, client_ref, entity)
                    continue

                for group_ref in _collect_refs(record, "group"):
                    for client_ref in clients:
                        remember(by_group, group_ref, client_ref)
                        stats[f"{entity} (по группе)"] = (
                            stats.get(f"{entity} (по группе)", 0) + 1
                        )

            if stats:
                detail = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
                logger.info(
                    f"🔗 Связей «ученик ↔ занятие» найдено — {detail}. "
                    f"Групп с учениками: {len(by_group)}, "
                    f"занятий с точными записями: {len(by_schedule)}, "
                    f"учеников с точными записями: {len(precise_clients)}"
                )
                if precise_clients and by_group:
                    logger.info(
                        f"ℹ️ Для {len(precise_clients)} учеников используются только "
                        "их точные записи на занятия; членство в группе для них не "
                        "применяется (STRICT_ENROLLMENT=true)."
                    )
            else:
                logger.warning(
                    "⚠️ НИ ОДНОЙ связи «ученик ↔ группа/занятие» в CRM не найдено. "
                    "Расписание и домашние задания у родителей будут пустыми — "
                    "бот не может знать, кто на какие занятия ходит. "
                    "Укажите группу в абонементе ученика либо запишите его на "
                    "занятие (reservation). Диагностика: python impulse_probe.py"
                )
            return {
                "by_schedule": by_schedule,
                "by_group": by_group,
                "precise_clients": precise_clients,
            }

        return await self._lookups.get("customer_index", loader)

    async def _membership_records(self):
        """
        Записи, которые могут связывать клиента с группой или занятием:
        абонементы всех типов плюс разовые посещения. Отдаёт пары
        (запись, имя_сущности) — имя нужно только для лога.
        """
        result = []
        for record in await self.load_all_accounts():
            result.append((record, record.get("_entity") or "account"))

        async def load_singles():
            collected = []
            for entity in settings.IMPULSE_SINGLE_ENTITIES:
                try:
                    items = await self.list_(entity)
                except ImpulseCRMError as e:
                    logger.debug(f"Разовые занятия {entity} недоступны: {e}")
                    continue
                for item in items:
                    collected.append((item, entity))
            return collected

        result.extend(await self._lookups.get("singles", load_singles))
        return result

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

        index = await self._customer_index()
        raw_schedules = await self.list_("schedule")

        lessons: List[Dict[str, Any]] = []
        for raw in raw_schedules:
            lessons.extend(self._expand_schedule(raw, index, window_from, window_to))

        # Сравнение через _same_id, а не `in`: id из SQLite приходит int,
        # из CRM — как повезёт, и строгое сравнение молча давало пустоту.
        if teacher_id is not None:
            lessons = [
                l for l in lessons
                if any(_same_id(t, teacher_id) for t in l["teacher_ids"])
            ]
        if customer_id is not None:
            lessons = [
                l for l in lessons
                if any(_same_id(c, customer_id) for c in l["customer_ids"])
            ]
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
        index = await self._customer_index()
        lesson_id = str(lesson_id)

        if ":" in lesson_id:
            raw_id, _, date_part = lesson_id.partition(":")
            raw = await self.load("schedule", raw_id)
            if not raw:
                return None
            occurrences = self._expand_schedule(raw, index, date_part, date_part)
            return occurrences[0] if occurrences else None

        raw = await self.load("schedule", lesson_id)
        if not raw:
            return None
        occurrences = self._expand_schedule(raw, index, None, None)
        return occurrences[0] if occurrences else None

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
        if not settings.IMPULSE_WRITE_BACK:
            # У schedule в impulseCRM нет полей темы/ДЗ/статуса — писать
            # туда нечего. Тема, ДЗ и отметка «проведён» сохраняются в
            # собственной БД бота (database.set_lesson_note), а сюда
            # заходить не нужно. Включается через IMPULSE_WRITE_BACK=true,
            # когда вендор подтвердит наличие полей.
            raise ImpulseCRMError(
                "Запись в CRM отключена (IMPULSE_WRITE_BACK=false): у сущности "
                "schedule нет полей темы, ДЗ и статуса."
            )

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

    @staticmethod
    def _assert_ok(response: Any, what: str) -> Any:
        """
        Проверяет, что внутренний API действительно выполнил действие.

        Нужно потому, что при неподходящих данных (например, дата не
        совпала ни с одним занятием) сервер отвечает 200 и пустым/
        отрицательным телом — операция не выполняется, но выглядит как
        успех. Раньше тело ответа вообще не читалось, и бот рапортовал
        «отмечено», когда в CRM ничего не появлялось.
        """
        if isinstance(response, dict):
            for key in ("success", "result", "ok"):
                value = response.get(key)
                if value is False:
                    message = (
                        response.get("message")
                        or response.get("error")
                        or "CRM отклонила операцию без пояснения"
                    )
                    raise ImpulseCRMError(f"{what}: {message}")
            for key in ("error", "errors"):
                if response.get(key):
                    raise ImpulseCRMError(f"{what}: {response[key]}")
        logger.info(f"{what}: ответ CRM = {str(response)[:300]}")
        return response

    async def check_visit(
        self,
        client_id: Any,
        account: Dict[str, Any],
        target: Dict[str, Any],
        date_ts: int,
        force: bool = False,
    ) -> Any:
        r"""
        Отметить клиента присутствующим.

        Форма payload — из разобранного кода фронтенда (CheckVisitsChecker):
            {client_id, account, target, date, force?}

        ВАЖНО про имена полей. Ранее здесь отправлялись clientId/service/
        branchId/minutesBegin/duration/hallId — это была ОШИБКА: такая
        структура приходит в ОТВЕТЕ check_visits/visits (список визитов),
        а не ожидается в теле check. Сервер тогда не находил id клиента и
        падал с Doctrine ORMException «The identifier id is missing for a
        query of App\Entity\Client\Client».

        account и target передаются ЦЕЛИКОМ, как их отдал API
        (group_account/list и schedule.target), а не сокращённой ссылкой:
        фронтенд кладёт в запрос полные объекты.

        client_id дублируется как clientId — на случай, если разные версии
        CRM читают его под разными именами. Лишнее поле безвредно, а
        отсутствие нужного роняет запрос.
        """
        if not settings.IMPULSE_CHECK_VISITS_ENABLED:
            raise ImpulseCRMError(
                "Отметка посещений выключена (IMPULSE_CHECK_VISITS_ENABLED=false). "
                "Сначала проверьте внутренний API скриптом impulse_probe.py."
            )
        if client_id is None:
            raise ImpulseCRMError("check_visit: не передан id клиента")
        if not account:
            raise ImpulseCRMError(
                "check_visit: не найден абонемент клиента — отметить посещение не по чему"
            )
        if not target or not (target.get("id") if isinstance(target, dict) else None):
            # Ровно на это CRM отвечает «Выберите куда отмечать посещение».
            raise ImpulseCRMError(
                "У занятия не определена цель (группа или клиент) — "
                "CRM не примет отметку посещения"
            )

        payload: Dict[str, Any] = {
            "client_id": client_id,
            "clientId": client_id,
            "account": account,
            "target": target,
            "date": date_ts,
        }
        if force:
            payload["force"] = True
        logger.info(
            f"→ check_visits/check: клиент {client_id}, абонемент "
            f"{account.get('id')}, цель {target.get('id')}, дата {date_ts}"
        )
        response = await self._internal_request(
            "POST", settings.IMPULSE_PATH_CHECK, json_body=payload
        )
        return self._assert_ok(response, "Отметка посещения")

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
        if client_id is None:
            raise ImpulseCRMError("burn_visit: не передан id клиента")
        if not account:
            raise ImpulseCRMError("burn_visit: не найден абонемент клиента")

        payload: Dict[str, Any] = {
            "client_id": client_id,
            "clientId": client_id,
            "account": account,
            "target": target,
            "date": date_ts,
        }
        if target_values:
            payload["targetValues"] = target_values
        if reservation:
            payload["reservation"] = reservation
        logger.info(
            f"→ check_visits/burn_one: клиент {client_id}, дата {date_ts}"
        )
        response = await self._internal_request(
            "POST", settings.IMPULSE_PATH_BURN, json_body=payload
        )
        return self._assert_ok(response, "Списание занятия")

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

    # ==================== БЕСПРИЧИННЫЕ ЗАМОРОЗКИ ====================
    #
    # Счётчик живёт в поле АДРЕСА ПРОЖИВАНИЯ клиента: отдельного поля под
    # это в impulseCRM нет. Раньше использовался email — но он занят
    # настоящей почтой, и счётчик с ней конфликтовал.
    #
    # Точное имя поля адреса вендором не опубликовано, поэтому оно не
    # угадывается «на вечно»: имя определяется по реальной записи клиента
    # (первый из settings.IMPULSE_FIELD_CLIENT_FREEZES_ALT, который в ней
    # есть) и запоминается на время жизни процесса. Явно заданный в .env
    # IMPULSE_FIELD_CLIENT_FREEZES отменяет поиск.

    _freezes_field: Optional[str] = None

    @classmethod
    def freezes_field(cls, client: Optional[Dict[str, Any]] = None) -> str:
        """Имя поля CRM, в котором лежит счётчик беспричинных заморозок."""
        if settings.IMPULSE_FIELD_CLIENT_FREEZES:
            return settings.IMPULSE_FIELD_CLIENT_FREEZES
        if cls._freezes_field:
            return cls._freezes_field

        candidates = settings.IMPULSE_FIELD_CLIENT_FREEZES_ALT or ["address"]
        if client:
            for name in candidates:
                if name in client:
                    cls._freezes_field = name
                    logger.info(
                        f"🏠 Счётчик беспричинных заморозок хранится в поле "
                        f"'{name}' карточки клиента"
                    )
                    return name
            logger.warning(
                "⚠️ Ни одно из полей адреса "
                f"({', '.join(candidates)}) не найдено в карточке клиента. "
                f"Использую '{candidates[0]}' — при необходимости задайте "
                "IMPULSE_FIELD_CLIENT_FREEZES в .env."
            )
        # Ничего не запоминаем: следующая запись может оказаться полнее.
        return candidates[0]

    @classmethod
    def parse_free_freezes(cls, client: Dict[str, Any]) -> int:
        field = cls.freezes_field(client)
        raw = client.get(field)
        if raw is None:
            return 0
        text = str(raw).strip()
        if not text:
            return 0

        # Значением считается ТОЛЬКО целиком числовая строка.
        #
        # Здесь нельзя выбирать цифры из строки, как делалось для email:
        # адрес почти всегда заполнен по-настоящему, и «ул. Ленина, д. 5»
        # превратилось бы в пять беспричинных заморозок. Лучше показать
        # ноль и написать в лог, чем выдать клиенту заморозки из номера
        # дома.
        if text.lstrip("+-").isdigit():
            try:
                return max(0, int(text))
            except ValueError:
                pass

        logger.warning(
            f"⚠️ В поле {field} клиента {client.get('id')} не число: "
            f"{text!r} — считаю 0 заморозок. Чтобы выдать заморозки, "
            f"впишите в это поле только число."
        )
        return 0

    async def _load_client(self, client_id: Any) -> Optional[Dict[str, Any]]:
        client = await self.load("client", client_id)
        if not client:
            for c in await self.load_all_clients():
                if str(c.get("id")) == str(client_id):
                    return c
        return client

    async def get_free_freezes(self, client_id: Any) -> int:
        client = await self._load_client(client_id)
        return self.parse_free_freezes(client or {})

    async def set_free_freezes(self, client_id: Any, value: int) -> int:
        """
        Записывает остаток беспричинных заморозок обратно в CRM.

        Отправляется ПОЛНАЯ запись клиента (как и в update_lesson):
        частичный payload рискует обнулить поля, которых в нём не было.
        """
        client = await self._load_client(client_id)
        if not client:
            raise ImpulseCRMError(f"Клиент {client_id} не найден в CRM")

        record = dict(client)
        record[self.freezes_field(client)] = str(max(0, int(value)))
        record["id"] = client.get("id", client_id)
        await self.update("client", record)
        logger.info(f"✅ Остаток беспричинных заморозок клиента {client_id}: {value}")
        return max(0, int(value))

    async def spend_free_freeze(self, client_id: Any) -> int:
        """
        Расходует одну беспричинную заморозку. Возвращает остаток ПОСЛЕ
        списания. Если заморозок не осталось — ошибка, чтобы вызывающий
        мог показать это человеку, а не уводить счётчик в минус.
        """
        left = await self.get_free_freezes(client_id)
        if left <= 0:
            raise ImpulseCRMError("Беспричинных заморозок не осталось")
        return await self.set_free_freezes(client_id, left - 1)

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
