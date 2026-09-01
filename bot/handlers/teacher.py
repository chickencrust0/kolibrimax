"""
bot/handlers/teacher.py — расписание, отчёт, закрытие урока, ДЗ, заявка
на перенос со стороны преподавателя.

Портировано из alfacrm-bot. Изменения:
  * пункты меню («📅 Моё расписание» и т.п.) были кнопками постоянной
    reply-клавиатуры и матчились по тексту — в MAX это inline-кнопки
    меню, поэтому здесь они callback_query с data="menu:teacher:...";
  * все inline-клавиатуры собираются через max_api.keyboards;
  * message.bot.send_message(...) вызывается с именованными аргументами
    MaxBot вместо позиционных aiogram.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import settings
from impulse_client import ImpulseCRMClient, ImpulseCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    STATUS_CANCELLED,
    STATUS_CONDUCTED,
    STATUS_PLANNED,
    answer_blocks,
    build_schedule,
    day_header,
    esc,
    format_lesson,
    group_by_day,
    parse_lesson_date,
    safe_call,
)
from bot.handlers.common import fetch_lessons, get_lesson_snapshot, load_customer_map
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import DateRangeStates, HomeworkStates, TeacherTransferStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    lesson_action_keyboard,
    lesson_attendance_keyboard,
    schedule_period_keyboard,
    transfer_decision_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="teacher")


def _teacher(db: Database, max_user_id: int):
    user = db.get_user(max_user_id)
    return user if user and user["role"] == "teacher" else None


def _parse_iso(text: str):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# ==================== ВЫБОР ПЕРИОДА ====================

@router.callback_query(F.data == "menu:teacher:schedule")
async def teacher_schedule_menu(callback: Callback, db: Database, state: FSMContext) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Только для преподавателей.")
        return
    await state.clear()
    await callback.message.answer(
        "📅 <b>Выберите период:</b>", reply_markup=schedule_period_keyboard(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "schedule:custom")
async def custom_date_from(callback: Callback, db: Database, state: FSMContext) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Только для преподавателей.")
        return
    await state.set_state(DateRangeStates.waiting_for_date_from)
    await callback.message.edit_text(
        "📅 Введите начальную дату в формате <b>ГГГГ-ММ-ДД</b>\n"
        f"Пример: <code>{settings.today().isoformat()}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DateRangeStates.waiting_for_date_from, F.text)
async def custom_date_to(message: Msg, state: FSMContext) -> None:
    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
        return
    await state.update_data(date_from=message.text.strip())
    await state.set_state(DateRangeStates.waiting_for_date_to)
    await message.answer("📅 Введите конечную дату в формате <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")


@router.message(DateRangeStates.waiting_for_date_to, F.text)
async def show_custom_schedule(
    message: Msg,
    state: FSMContext,
    db: Database,
    impulse: ImpulseCRMClient,
    cache: LessonCache,
) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    date_to = message.text.strip()
    await state.clear()

    if not date_from:
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await show_schedule(message, impulse, cache, user, date_from, date_to, db=db)


@router.callback_query(F.data.startswith("schedule:"))
async def handle_schedule_period(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для преподавателей.")
        return

    period = callback.data.split(":")[1]
    today = settings.today()

    if period == "today":
        date_from = date_to = today.isoformat()
    elif period == "tomorrow":
        date_from = date_to = (today + timedelta(days=1)).isoformat()
    elif period == "week":
        date_from, date_to = today.isoformat(), (today + timedelta(days=7)).isoformat()
    elif period == "month":
        date_from, date_to = today.isoformat(), (today + timedelta(days=30)).isoformat()
    else:
        await callback.answer()
        return

    await callback.answer()
    await show_schedule(callback.message, impulse, cache, user, date_from, date_to, db=db)


async def show_schedule(
    message,
    impulse: ImpulseCRMClient,
    cache: LessonCache,
    user,
    date_from: str,
    date_to: str,
    db: Optional[Database] = None,
) -> None:
    """
    Короткий период — карточки с кнопками действий.
    Длинный — сгруппированный текст: сотня сообщений подряд упирается
    в лимит MAX (2 сообщения/сек в диалог), и часть просто не доходит.
    """
    try:
        lessons = await fetch_lessons(
            impulse, cache,
            teacher_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except ImpulseCRMError as e:
        await message.answer(f"❌ Ошибка получения расписания: {esc(str(e))}", parse_mode="HTML")
        return

    if not lessons:
        hint = "" if cache.is_ready() else "\n\n<i>Кеш ещё обновляется, попробуйте через минуту.</i>"
        await message.answer(
            f"📅 Нет уроков в период с <b>{esc(date_from)}</b> по <b>{esc(date_to)}</b>{hint}",
            parse_mode="HTML",
        )
        return

    customers = await load_customer_map(impulse)
    today = settings.today()

    if len(lessons) > settings.MAX_LESSON_CARDS:
        blocks = build_schedule(
            lessons,
            role="teacher",
            title=f"Расписание {date_from} – {date_to}",
            customers=customers,
            today=today,
            note="Кнопки действий доступны при выборе более короткого периода.",
        )
        await answer_blocks(message, blocks)
        return

    await message.answer(
        f"📅 <b>Расписание</b>\n"
        f"Период: {esc(date_from)} – {esc(date_to)}\n"
        f"Всего уроков: <b>{len(lessons)}</b>",
        parse_mode="HTML",
    )

    for day, day_lessons in group_by_day(lessons):
        await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda d=day: message.answer(day_header(d, today), parse_mode="HTML"))
        for lesson in day_lessons:
            card = format_lesson(lesson, role="teacher", customers=customers)
            # Ученики занятия с кнопкой отметки посещения у каждого.
            students = [
                (cid, customers.get(cid, f"ID:{cid}"))
                for cid in lesson.get("customer_ids") or []
            ]
            if students and lesson.get("id"):
                absent_ids = set(db.get_absent_client_ids(lesson["id"])) if db else set()
                keyboard = lesson_attendance_keyboard(
                    lesson["id"], students, absent=absent_ids
                )
            elif lesson.get("status") == STATUS_PLANNED and lesson.get("id"):
                keyboard = lesson_action_keyboard(lesson["id"])
            else:
                keyboard = None
            await asyncio.sleep(settings.MAX_SEND_DELAY)
            await safe_call(
                lambda c=card, k=keyboard: message.answer(c, parse_mode="HTML", reply_markup=k)
            )


# ==================== ОТМЕТКА ПОСЕЩЕНИЯ ====================
#
# Пишет через внутренний API impulseCRM (POST check_visits/check), который
# найден разбором фронтенда и вендором не документирован — см. комментарий
# в settings.py. Пока IMPULSE_CHECK_VISITS_ENABLED=false, клиент бросает
# понятную ошибку, и сюда она приходит текстом для преподавателя.

def _lesson_date_ts(lesson) -> int:
    """Unix-timestamp начала суток занятия — в таком виде дату ждёт
    внутренний API check_visits."""
    day = parse_lesson_date(lesson)
    if not day:
        raise ValueError("у занятия нет даты")
    return int(
        datetime(day.year, day.month, day.day, tzinfo=settings.TZ).timestamp()
    )


async def _toggle_attendance(
    callback: Callback,
    db: Database,
    impulse: ImpulseCRMClient,
    cache: LessonCache,
    *,
    present: bool,
) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    # callback.data: att:<lesson_id>:<client_id>, где lesson_id у
    # повторяющегося занятия сам содержит двоеточие ("116:2026-09-01"),
    # поэтому client_id отделяем справа.
    payload = callback.data.split(":", 1)[1]
    lesson_id, _, client_id_raw = payload.rpartition(":")
    if not lesson_id or not client_id_raw:
        await callback.answer("❌ Не разобрал занятие/ученика.")
        return

    try:
        client_id = int(client_id_raw)
    except ValueError:
        client_id = client_id_raw

    lesson = await get_lesson_snapshot(lesson_id, impulse, cache)
    if not lesson:
        await callback.answer("❌ Занятие не найдено.")
        return

    try:
        date_ts = _lesson_date_ts(lesson)
    except ValueError:
        await callback.answer("❌ У занятия не определена дата.")
        return

    accounts_by_client = await impulse.get_accounts_by_client()
    account = impulse.pick_active_account(accounts_by_client.get(client_id, []))
    if not account:
        await callback.answer("❌ У ученика нет действующего абонемента.")
        return

    target = lesson.get("_target") or {}

    raw = lesson.get("_raw") or {}
    minutes_begin = raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_BEGIN)
    minutes_end = raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_END)
    duration = None
    if minutes_begin is not None and minutes_end is not None:
        try:
            duration = int(minutes_end) - int(minutes_begin)
        except (TypeError, ValueError):
            duration = None
    hall = raw.get("hall") or {}
    hall_id = hall.get("id") if isinstance(hall, dict) else hall

    try:
        if present:
            await impulse.check_visit(
                client_id, account, target, date_ts,
                minutes_begin=minutes_begin, duration=duration, hall_id=hall_id,
            )
        else:
            await impulse.burn_visit(
                client_id, account, target, date_ts,
                target_values={
                    "minutesBegin": minutes_begin,
                    "minutesEnd": minutes_end,
                    "date": date_ts,
                },
            )
    except ImpulseCRMError as e:
        await callback.answer()
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    # Отметка присутствия снимает ранее поставленную неявку и наоборот —
    # ученик не может быть одновременно и на занятии, и в списке неявок.
    if present:
        db.unmark_absent(lesson_id, client_id)

    await _refresh_attendance_card(callback, db, impulse, lesson, lesson_id, date_ts)
    await callback.answer("✅ Отмечено" if present else "↩️ Отметка снята")


async def _refresh_attendance_card(
    callback: Callback,
    db: Database,
    impulse: ImpulseCRMClient,
    lesson,
    lesson_id: Any,
    date_ts: int,
) -> None:
    customers = await load_customer_map(impulse)
    students = [
        (cid, customers.get(cid, f"ID:{cid}")) for cid in lesson.get("customer_ids") or []
    ]
    marked = await _marked_set(impulse, date_ts, lesson)
    absent = set(db.get_absent_client_ids(lesson_id))
    await callback.message.edit_text(
        callback.message.text or format_lesson(lesson, role="teacher", customers=customers),
        reply_markup=lesson_attendance_keyboard(lesson_id, students, marked, absent),
    )


async def _marked_set(impulse: ImpulseCRMClient, date_ts: int, lesson) -> set:
    """
    Кто уже отмечен на эту дату.

    ВАЖНО (подтверждено реальным ответом API): в записи visit НЕТ поля
    client_id. Связь с учеником идёт через visit.service -> id абонемента
    (groupAccount), а уже абонемент ссылается на клиента. Поэтому
    сопоставляем visit.service.id с абонементами клиентов, а не ищем
    несуществующий client_id.

    Если внутренний API недоступен (а сейчас он не принимает API-ключ,
    см. README раздел 3) — возвращаем пустое множество, кнопки просто
    останутся неотмеченными.
    """
    try:
        visits = await impulse.get_visits(date_ts)
    except Exception:
        return set()

    items = visits if isinstance(visits, list) else (visits or {}).get("items") or []
    if not items:
        return set()

    # id абонемента -> id клиента
    try:
        accounts_by_client = await impulse.get_accounts_by_client()
    except Exception:
        return set()
    client_by_account = {
        a.get("id"): cid
        for cid, accounts in accounts_by_client.items()
        for a in accounts
        if a.get("id") is not None
    }

    target_group_id = lesson.get("group_id")
    marked = set()
    for v in items:
        if not isinstance(v, dict):
            continue
        # Отсекаем визиты по другим занятиям того же дня.
        if target_group_id is not None:
            v_target = v.get("target") or {}
            if isinstance(v_target, dict) and v_target.get("id") is not None:
                if str(v_target["id"]) != str(target_group_id):
                    continue

        service = v.get("service") or {}
        account_id = service.get("id") if isinstance(service, dict) else service
        cid = client_by_account.get(account_id)
        if cid is None:
            # запасной путь на случай других форм ответа
            cid = v.get("clientId") or v.get("client_id")
            if cid is None and isinstance(v.get("client"), dict):
                cid = v["client"].get("id")
        if cid is not None:
            marked.add(cid)
    return marked


@router.callback_query(F.data.startswith("att:"))
async def mark_attendance(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    await _toggle_attendance(callback, db, impulse, cache, present=True)


@router.callback_query(F.data.startswith("unatt:"))
async def unmark_attendance(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    await _toggle_attendance(callback, db, impulse, cache, present=False)


# ==================== НЕЯВКА («не пришёл») ====================
#
# Неявка НЕ пишется в CRM сразу: в impulseCRM нет статуса «не пришёл»,
# есть только списание занятия (burn_one). Решение списывать или нет —
# за менеджером, поэтому отметка преподавателя живёт в БД бота и
# попадает в вечернюю сводку менеджеру (см. scheduler.send_absence_digest).

async def _toggle_absence(
    callback: Callback,
    db: Database,
    impulse: ImpulseCRMClient,
    cache: LessonCache,
    *,
    absent: bool,
) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    payload = callback.data.split(":", 1)[1]
    lesson_id, _, client_id_raw = payload.rpartition(":")
    if not lesson_id or not client_id_raw:
        await callback.answer("❌ Не разобрал занятие/ученика.")
        return
    try:
        client_id = int(client_id_raw)
    except ValueError:
        await callback.answer("❌ Некорректный ID ученика.")
        return

    lesson = await get_lesson_snapshot(lesson_id, impulse, cache)
    if not lesson:
        await callback.answer("❌ Занятие не найдено.")
        return

    lesson_day = parse_lesson_date(lesson)
    if not lesson_day:
        await callback.answer("❌ У занятия не определена дата.")
        return

    if absent:
        customers = await load_customer_map(impulse)
        db.mark_absent(
            lesson_id,
            client_id,
            customers.get(client_id, f"ID:{client_id}"),
            lesson_day.isoformat(),
            callback.from_user.id,
        )
    else:
        db.unmark_absent(lesson_id, client_id)

    try:
        date_ts = _lesson_date_ts(lesson)
    except ValueError:
        date_ts = 0

    await _refresh_attendance_card(callback, db, impulse, lesson, lesson_id, date_ts)
    await callback.answer("❌ Отмечена неявка" if absent else "↩️ Неявка снята")


@router.callback_query(F.data.startswith("abs:"))
async def mark_absent(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    await _toggle_absence(callback, db, impulse, cache, absent=True)


@router.callback_query(F.data.startswith("unabs:"))
async def unmark_absent(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    await _toggle_absence(callback, db, impulse, cache, absent=False)


# ==================== ОТЧЁТ ====================

@router.callback_query(F.data == "menu:teacher:report")
async def teacher_report(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для преподавателей.")
        return
    await callback.answer()

    today = settings.today()
    date_from = (today - timedelta(days=30)).isoformat()

    try:
        lessons = await fetch_lessons(
            impulse, cache,
            teacher_id=user["crm_id"], date_from=date_from, date_to=today.isoformat(),
        )
    except ImpulseCRMError as e:
        await callback.message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    total = len(lessons)
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    not_closed = total - conducted - cancelled
    with_hw = sum(1 for l in lessons if (l.get("homework") or "").strip())

    await callback.message.answer(
        f"📊 <b>Отчёт за 30 дней</b>\n"
        f"Период: {date_from} – {today.isoformat()}\n\n"
        f"📅 <b>Всего:</b> {total}\n"
        f"✅ <b>Проведено:</b> {conducted}\n"
        f"❌ <b>Отменено:</b> {cancelled}\n"
        f"⚠️ <b>Не закрыто:</b> {not_closed}\n\n"
        f"📚 <b>С ДЗ:</b> {with_hw}\n"
        f"📝 <b>Без ДЗ:</b> {max(0, conducted - with_hw)}\n\n"
        f"{'⚠️ Есть незакрытые уроки!' if not_closed > 0 else '✅ Все уроки закрыты!'}",
        parse_mode="HTML",
    )


# ==================== ЗАКРЫТИЕ УРОКА ====================

@router.callback_query(F.data.startswith("close:"))
async def close_lesson(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    lesson_id = int(callback.data.split(":")[1])
    try:
        # Передаём текущую модель урока целиком: частичный апдейт
        # {"status": 3} рисковал затереть в CRM время и состав участников.
        current = await get_lesson_snapshot(lesson_id, impulse, cache)
        await impulse.mark_lesson_conducted(lesson_id, current=current)
        await cache.patch_lesson(lesson_id, {"status": STATUS_CONDUCTED})
        db.mark_reminder_sent(lesson_id, "closed", callback.from_user.id)

        original = callback.message.text or ""
        await callback.message.edit_text(
            f"{original}\n\n✅ <b>Отмечен как проведённый</b>", parse_mode="HTML"
        )
        await callback.answer("✅ Урок проведён!")
    except ImpulseCRMError as e:
        await callback.answer(f"❌ Ошибка: {e}")


# ==================== ДОМАШНЕЕ ЗАДАНИЕ ====================

@router.callback_query(F.data.startswith("hw:"))
async def attach_hw_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(HomeworkStates.waiting_for_text_or_file)
    await callback.message.answer("📝 Отправьте текст или файл ДЗ.")
    await callback.answer()


@router.message(HomeworkStates.waiting_for_text_or_file, F.text)
async def attach_hw_text(
    message: Msg, state: FSMContext, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    try:
        current = await get_lesson_snapshot(lesson_id, impulse, cache)
        await impulse.set_homework(lesson_id, message.text, current=current)
        await cache.patch_lesson(lesson_id, {"homework": message.text})
        await message.answer("✅ ДЗ сохранено!")
    except ImpulseCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
    await state.clear()


@router.message(HomeworkStates.waiting_for_text_or_file, F.document | F.photo)
async def attach_hw_file(
    message: Msg, state: FSMContext, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    if message.document:
        file_token = message.document.token
        file_name = message.document.file_name or "файл"
        file_type = "document"
    else:
        file_token = message.photo.token
        file_name = "фото"
        file_type = "photo"

    db.add_homework_file(lesson_id, file_token, file_name, file_type)

    try:
        current = await get_lesson_snapshot(lesson_id, impulse, cache)
        existing_hw = ((current or {}).get("homework") or "").strip()
        note = f"[📎 {file_name}]"
        new_hw = f"{existing_hw}\n{note}" if existing_hw else note
        await impulse.set_homework(lesson_id, new_hw, current=current)
        await cache.patch_lesson(lesson_id, {"homework": new_hw})
        await message.answer("✅ Файл прикреплён!")
    except ImpulseCRMError as e:
        await message.answer(
            f"⚠️ Файл сохранён локально. Ошибка CRM: {esc(str(e))}", parse_mode="HTML"
        )

    await state.clear()


# ==================== ЗАЯВКА НА ПЕРЕНОС ====================

@router.callback_query(F.data.startswith("transfer:"))
async def transfer_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(TeacherTransferStates.waiting_for_comment)
    await callback.message.answer("🔁 Напишите желаемую дату/время и причину переноса.")
    await callback.answer()


@router.message(TeacherTransferStates.waiting_for_comment, F.text)
async def transfer_finish(message: Msg, state: FSMContext, db: Database) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    await state.clear()

    request_id = db.create_transfer_request(
        message.from_user.id, lesson_id, message.text, author_role="teacher"
    )
    sent_at = settings.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in settings.MANAGER_IDS:
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                user_id=mid,
                text=(
                    f"🔁 <b>Заявка на перенос №{request_id}</b>\n\n"
                    f"📅 <b>Получена:</b> {sent_at}\n"
                    f"👨‍🏫 <b>Преподаватель:</b> {esc(user['full_name'])}\n"
                    f"🆔 <b>Урок ID:</b> {esc(lesson_id or '—')}\n"
                    f"💬 <b>Комментарий:</b> {esc(message.text)}"
                ),
                fmt="html",
                attachments=transfer_decision_keyboard(request_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
