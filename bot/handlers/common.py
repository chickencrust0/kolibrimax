"""
bot/handlers/common.py — общие помощники для хендлеров и планировщика.

Портировано из alfacrm-bot: карты имён кешируются 5 минут, получение
уроков идёт через кеш с откатом на CRM, сборка сводки менеджера — без
изменений логики, только impulse вместо alfacrm.

impulseCRM не публикует признак «активный ученик vs лид» (в AlfaCRM
это было поле is_study) — load_customer_map() здесь берёт ВСЕХ клиентов
из сущности client. Если в вашей impulseCRM есть отдельный статус/пайплайн
для лидов, отфильтруйте его на уровне impulse_client.load_all_clients()
после того как сверите схему скриптом impulse_introspect.py.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import settings
from impulse_client import ImpulseCRMClient
from database import Database
from bot.formatting import (  # noqa: F401  (реэкспорт)
    answer_blocks,
    build_summary,
    in_date_range,
    send_blocks,
)

logger = logging.getLogger(__name__)

# Карты имён меняются редко — держим их 5 минут.
_MAP_TTL = 300
_maps_cache: Dict[str, tuple] = {}


async def _cached(key: str, loader) -> Dict[Any, str]:
    cached = _maps_cache.get(key)
    if cached and (time.monotonic() - cached[1]) < _MAP_TTL:
        return cached[0]
    try:
        data = await loader()
    except Exception as e:
        logger.error(f"Не удалось загрузить карту '{key}': {e}")
        return cached[0] if cached else {}
    _maps_cache[key] = (data, time.monotonic())
    return data


def invalidate_name_maps() -> None:
    """Сбросить кеш имён (например, после добавления педагога в CRM)."""
    _maps_cache.clear()


async def load_teacher_map(impulse: ImpulseCRMClient) -> Dict[Any, str]:
    """{teacher_id: имя} по всем страницам."""
    async def loader():
        items = await impulse.load_all_teachers()
        return {t["id"]: impulse.extract_user_name(t) for t in items if t.get("id")}

    return await _cached("teachers", loader)


async def load_customer_map(impulse: ImpulseCRMClient) -> Dict[Any, str]:
    """{customer_id: имя} по всем клиентам."""
    async def loader():
        items = await impulse.load_all_clients()
        return {c["id"]: impulse.extract_user_name(c) for c in items if c.get("id")}

    return await _cached("customers", loader)


# ==================== УРОКИ ====================

def apply_lesson_notes(
    lessons: Sequence[Dict[str, Any]], db: Optional[Database]
) -> List[Dict[str, Any]]:
    """
    Накладывает на уроки тему, ДЗ и статус из локальной БД.

    У сущности schedule в impulseCRM таких полей нет (см. schema.md и
    комментарий в database.py), поэтому CRM всегда отдаёт их пустыми.
    Единая точка наложения нужна, чтобы родитель, преподаватель, сводка
    менеджера и напоминания видели одно и то же.

    Уроки НЕ мутируются на месте: объекты из кеша переиспользуются между
    запросами, и правка in-place протекла бы в чужие ответы.
    """
    if db is None or not lessons:
        return list(lessons)

    try:
        notes = db.get_lesson_notes([l.get("id") for l in lessons])
    except Exception as e:
        logger.error(f"Не удалось прочитать локальные заметки к урокам: {e}")
        return list(lessons)

    if not notes:
        return list(lessons)

    result = []
    for lesson in lessons:
        note = notes.get(str(lesson.get("id")))
        if not note:
            result.append(lesson)
            continue
        merged = dict(lesson)
        if note.get("topic"):
            merged["topic"] = note["topic"]
        if note.get("homework"):
            merged["homework"] = note["homework"]
        if note.get("status") is not None:
            merged["status"] = note["status"]
        result.append(merged)
    return result


async def fetch_lessons(
    impulse: ImpulseCRMClient,
    cache=None,
    *,
    db: Optional[Database] = None,
    teacher_id: Optional[Any] = None,
    customer_id: Optional[Any] = None,
    status: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Единая точка получения уроков.

    Берём из кеша, если он покрывает период целиком, иначе идём в CRM.
    Фильтрация по датам делается здесь в обоих случаях, чтобы все
    вызывающие получали одинаково точный результат.

    ВАЖНО: фильтр по статусу применяется ПОСЛЕ наложения локальных
    заметок. Иначе запрос «проведённые уроки» отсекал бы всё ещё на
    уровне CRM, где статуса нет вовсе и все уроки числятся
    запланированными — из-за этого раздел ДЗ у родителя был пуст.
    """
    if cache is not None and cache.covers(date_from, date_to):
        lessons = cache.get_lessons(
            teacher_id=teacher_id,
            customer_id=customer_id,
            date_from=date_from,
            date_to=date_to,
        )
    else:
        lessons = await impulse.get_lessons(
            teacher_id=teacher_id,
            customer_id=customer_id,
            date_from=date_from,
            date_to=date_to,
        )
        if date_from or date_to:
            lessons = [l for l in lessons if in_date_range(l, date_from, date_to)]

    lessons = apply_lesson_notes(lessons, db)

    if status is not None:
        lessons = [l for l in lessons if l.get("status") == status]
    return lessons


async def get_lesson_snapshot(
    lesson_id: Any,
    impulse: ImpulseCRMClient,
    cache=None,
    db: Optional[Database] = None,
) -> Optional[Dict[str, Any]]:
    """Текущее состояние урока: сперва кеш, потом CRM, сверху — заметки."""
    lesson = cache.get_lesson(lesson_id) if cache is not None else None
    if lesson is None:
        lesson = await impulse.get_lesson(lesson_id)
    if lesson is None:
        return None
    return apply_lesson_notes([lesson], db)[0]


async def get_lesson_summary(
    lessons: Sequence[Dict[str, Any]],
    db: Database,
    impulse: ImpulseCRMClient,
    period_label: str,
) -> List[str]:
    """
    Сводка по урокам для менеджера, сгруппированная по дням.

    Возвращает СПИСОК готовых сообщений (format="html"): нарезка идёт
    по границам карточек, поэтому HTML-теги не рвутся посередине.
    """
    if not lessons:
        return build_summary([], period_label=period_label)

    teachers = await load_teacher_map(impulse)
    customers = await load_customer_map(impulse)

    lesson_ids = [str(l["id"]) for l in lessons if l.get("id")]
    try:
        hw_counts = db.get_homework_file_counts(lesson_ids)
    except Exception as e:
        logger.error(f"Не удалось получить счётчики файлов ДЗ: {e}")
        hw_counts = {}

    return build_summary(
        lessons,
        period_label=period_label,
        teachers=teachers,
        customers=customers,
        hw_counts=hw_counts,
        today=settings.today(),
    )


def manager_ids(db: Database) -> List[int]:
    """
    Все получатели уведомлений менеджера.

    Менеджером можно стать двумя путями — попасть в ADMIN_MAX_IDS или
    ввести пароль по /manager. Если брать только settings.MANAGER_IDS,
    вошедшие по паролю не получат ни заявок, ни обращений, ни сводок,
    поэтому оба источника объединяются здесь, а не в каждом хендлере.
    """
    ids = list(settings.MANAGER_IDS)
    try:
        for uid in db.get_manager_ids():
            if uid not in ids:
                ids.append(uid)
    except Exception as e:
        logger.warning(f"Не удалось получить менеджеров из БД: {e}")
    return ids


def is_manager(db: Database, max_user_id: int) -> bool:
    return max_user_id in manager_ids(db)
