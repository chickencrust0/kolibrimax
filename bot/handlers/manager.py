"""
bot/handlers/manager.py — рассылка, заявки на перенос, сводка за период.

Портировано из alfacrm-bot: пункты меню — callback_query
(F.data == "menu:manager:...") вместо F.text == "<подпись кнопки>".
"""

import asyncio
import logging
from datetime import datetime

import settings
from impulse_client import ImpulseCRMClient, ImpulseCRMError
from cache import LessonCache
from database import Database
from bot.formatting import answer_blocks, esc, fmt_date_long, fmt_db_time, safe_call
from bot.handlers.common import fetch_lessons, get_lesson_snapshot, get_lesson_summary
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import BroadcastStates, ManagerReplyStates, ManagerSummaryStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    absence_decision_keyboard,
    recipients_keyboard,
    support_manager_keyboard,
    support_user_keyboard,
    transfer_decision_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="manager")


def _is_manager(max_user_id: int) -> bool:
    return max_user_id in settings.MANAGER_IDS


def _parse_date(text: str):
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ, возвращает date или None."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ==================== РАССЫЛКА ====================

@router.callback_query(F.data == "menu:manager:broadcast")
async def broadcast_start(callback: Callback, state: FSMContext) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    await state.set_state(BroadcastStates.waiting_for_content)
    await callback.message.answer(
        "📢 Отправьте текст рассылки или фото (можно с подписью).\n"
        "Если нужно только фото — отправьте его без текста."
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_for_content, F.text)
async def broadcast_content_text(message: Msg, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text, broadcast_photo=None)
    await _ask_recipients(message, state)


@router.message(BroadcastStates.waiting_for_content, F.photo)
async def broadcast_content_photo(message: Msg, state: FSMContext) -> None:
    await state.update_data(
        broadcast_text="",
        broadcast_photo=message.photo.token,
    )
    await _ask_recipients(message, state)


async def _ask_recipients(message: Msg, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_for_recipient)
    await message.answer("Кому отправить?", reply_markup=recipients_keyboard())


@router.message(BroadcastStates.waiting_for_recipient)
async def broadcast_recipient_ignore(message: Msg) -> None:
    await message.answer(
        "Пожалуйста, выберите получателей с помощью кнопок ниже.",
        reply_markup=recipients_keyboard(),
    )


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_execute(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return

    action = callback.data.split(":")[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    text = data.get("broadcast_text") or ""
    photo_token = data.get("broadcast_photo")
    if not text and not photo_token:
        await callback.message.edit_text("❌ Нечего отправлять — начните заново.")
        await callback.answer()
        return

    if action == "teacher":
        recipients = db.get_all_users_by_role("teacher")
    elif action == "parent":
        recipients = db.get_all_users_by_role("parent")
    else:
        recipients = db.get_all_users_by_role("teacher") + db.get_all_users_by_role("parent")

    await callback.message.edit_text(f"📤 Отправляю… ({len(recipients)} получателей)")
    await callback.answer()

    success = failed = 0
    for i, user in enumerate(recipients):
        if i:
            # Без паузы рассылка на сотню человек упирается в лимит MAX
            # (2 сообщения/сек в один диалог) и часть просто теряется.
            await asyncio.sleep(settings.MAX_BROADCAST_DELAY)
        try:
            if photo_token:
                await safe_call(lambda u=user: callback.bot.send_message(
                    user_id=u["max_user_id"],
                    text=text,
                    attachments=[{"type": "image", "payload": {"token": photo_token}}],
                ))
            else:
                await safe_call(lambda u=user: callback.bot.send_message(
                    user_id=u["max_user_id"], text=text
                ))
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Рассылка не дошла до {user['max_user_id']}: {e}")

    await callback.message.edit_text(
        f"✅ Отправлено: {success}/{len(recipients)}"
        + (f"\n⚠️ Не доставлено: {failed}" if failed else "")
    )


# ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

@router.callback_query(F.data == "menu:manager:transfers")
async def transfer_list(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    requests = db.get_pending_transfer_requests()
    if not requests:
        await callback.message.answer("📭 Заявок нет.")
        return

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}

    for i, req in enumerate(requests):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        author_role = req.get("author_role") or "teacher"
        lesson_id = req.get("lesson_id") or "—"
        await safe_call(lambda r=req, al=author_role, lid=lesson_id: callback.message.answer(
            f"🔁 <b>Заявка №{r['id']}</b>\n\n"
            f"📅 <b>Создана:</b> {esc(fmt_db_time(r.get('created_at')))}\n"
            f"{role_label.get(al, '👤 Автор')}: {esc(r.get('author_name') or '—')}\n"
            f"📞 <b>Телефон:</b> {esc(r.get('author_phone') or '—')}\n"
            f"🆔 <b>Урок ID:</b> {esc(lid)}\n"
            f"💬 <b>Комментарий:</b> {esc(r.get('comment') or '—')}",
            parse_mode="HTML",
            reply_markup=transfer_decision_keyboard(r["id"]),
        ))


async def _resolve_transfer(
    callback: Callback, db: Database, status: str, label: str
) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return

    request_id = int(callback.data.split(":")[1])
    request = db.get_transfer_request(request_id)
    if not request:
        await callback.answer("Заявка не найдена.")
        return

    # resolve_* возвращает False, если заявку уже закрыл другой менеджер.
    if not db.resolve_transfer_request(request_id, status, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Заявка №{request_id} уже обработана.")
        await callback.answer()
        return

    try:
        await callback.bot.send_message(
            user_id=request["teacher_max_id"], text=f"{label} Заявка №{request_id}."
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить автора заявки {request_id}: {e}")

    await callback.message.edit_text(f"{label} Заявка №{request_id}.")
    await callback.answer()


@router.callback_query(F.data.startswith("transfer_ok:"))
async def transfer_approve(callback: Callback, db: Database) -> None:
    await _resolve_transfer(callback, db, "approved", "✅ Одобрено:")


@router.callback_query(F.data.startswith("transfer_no:"))
async def transfer_reject(callback: Callback, db: Database) -> None:
    await _resolve_transfer(callback, db, "rejected", "❌ Отклонено:")


# ==================== СВОДКА ЗА ПЕРИОД ====================

@router.callback_query(F.data == "menu:manager:summary")
async def summary_period_start(callback: Callback, state: FSMContext) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    await state.set_state(ManagerSummaryStates.waiting_for_date_from)
    today = settings.today()
    await callback.message.answer(
        "📅 Введите <b>начальную</b> дату.\n"
        f"Формат: <code>{today.isoformat()}</code> или <code>{today.strftime('%d.%m.%Y')}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ManagerSummaryStates.waiting_for_date_from, F.text)
async def summary_date_from(message: Msg, state: FSMContext) -> None:
    parsed = _parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
            parse_mode="HTML",
        )
        return

    await state.update_data(date_from=parsed.isoformat())
    await state.set_state(ManagerSummaryStates.waiting_for_date_to)
    await message.answer(
        "📅 Теперь <b>конечную</b> дату.\n"
        "<i>Отправьте «-», чтобы взять тот же день.</i>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_to, F.text)
async def summary_date_to(
    message: Msg,
    state: FSMContext,
    db: Database,
    impulse: ImpulseCRMClient,
    cache: LessonCache,
) -> None:
    if not _is_manager(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    if not date_from:
        await state.clear()
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return

    if message.text.strip() in ("-", "—"):
        date_to = date_from
    else:
        parsed = _parse_date(message.text)
        if not parsed:
            await message.answer(
                "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
                parse_mode="HTML",
            )
            return
        date_to = parsed.isoformat()

    await state.clear()

    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await message.answer("🔍 Собираю сводку…")
    logger.info(f"📅 Сводка за {date_from} – {date_to} (branch_id={settings.BRANCH_ID})")

    try:
        lessons = await fetch_lessons(impulse, cache, date_from=date_from, date_to=date_to)
        logger.info(f"📊 Уроков в периоде: {len(lessons)}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения уроков: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка получения уроков: {esc(str(e))}", parse_mode="HTML")
        return

    if date_from == date_to:
        day = datetime.strptime(date_from, "%Y-%m-%d").date()
        period_label = f"за {fmt_date_long(day)}"
    else:
        period_label = f"за период {date_from} – {date_to}"

    try:
        blocks = await get_lesson_summary(lessons, db, impulse, period_label)
        await answer_blocks(message, blocks)
        logger.info(f"✅ Сводка отправлена ({len(blocks)} сообщений)")
    except Exception as e:
        logger.error(f"❌ Ошибка формирования/отправки сводки: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")


# ==================== НЕЯВКИ ====================
#
# Преподаватель отмечает неявку в боте (она хранится в БД бота, т.к. в
# impulseCRM статуса «не пришёл» нет). Менеджер решает: списать занятие
# с абонемента (POST check_visits/burn_one) или признать причину
# уважительной — тогда в CRM ничего не уходит.

async def send_absence_list(message, db: Database, date_iso: str) -> None:
    absences = db.get_absences_for_date(date_iso, status="pending")
    if not absences:
        await message.answer(f"✅ Неявок за {esc(date_iso)} нет.", parse_mode="HTML")
        return

    await message.answer(
        f"❌ <b>Неявки за {esc(date_iso)}</b>: {len(absences)}", parse_mode="HTML"
    )
    for i, a in enumerate(absences):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda r=a: message.answer(
            f"👤 <b>{esc(r.get('client_name') or r['client_id'])}</b>\n"
            f"📅 <b>Дата:</b> {esc(r['lesson_date'])}\n"
            f"🆔 <b>Занятие:</b> {esc(r['lesson_id'])}\n"
            f"🕐 <b>Отмечено:</b> {esc(fmt_db_time(r.get('created_at')))}",
            parse_mode="HTML",
            reply_markup=absence_decision_keyboard(r["id"]),
        ))


@router.callback_query(F.data == "menu:manager:absences")
async def manager_absences(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()
    await send_absence_list(callback.message, db, settings.today().isoformat())


@router.callback_query(F.data.startswith("burn:"))
async def absence_burn(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return

    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return
    if absence["status"] != "pending":
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return

    lesson = await get_lesson_snapshot(absence["lesson_id"], impulse, cache)
    if not lesson:
        await callback.answer("❌ Занятие не найдено в CRM.")
        return

    try:
        day = datetime.strptime(absence["lesson_date"], "%Y-%m-%d").date()
        date_ts = int(datetime(day.year, day.month, day.day, tzinfo=settings.TZ).timestamp())
    except ValueError:
        await callback.answer("❌ Некорректная дата занятия.")
        return

    accounts_by_client = await impulse.get_accounts_by_client()
    account = impulse.pick_active_account(accounts_by_client.get(absence["client_id"], []))
    if not account:
        await callback.answer("❌ У ученика нет действующего абонемента.")
        return

    raw = lesson.get("_raw") or {}
    target_values = {
        "minutesBegin": raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_BEGIN),
        "minutesEnd": raw.get(settings.IMPULSE_FIELD_SCHEDULE_MINUTES_END),
        "date": date_ts,
    }

    try:
        await impulse.burn_visit(
            absence["client_id"],
            account,
            lesson.get("_target") or {},
            date_ts,
            target_values=target_values,
        )
    except ImpulseCRMError as e:
        await callback.answer()
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    # resolve_* вернёт False, если неявку уже закрыл другой менеджер.
    if not db.resolve_absence(absence_id, "burned", callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return

    await callback.message.edit_text(
        f"🔥 Занятие списано (сгорело): "
        f"{esc(absence.get('client_name') or absence['client_id'])}, "
        f"{esc(absence['lesson_date'])}"
    )
    await callback.answer("🔥 Списано")


@router.callback_query(F.data.startswith("excuse:"))
async def absence_excuse(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return

    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return

    if not db.resolve_absence(absence_id, "excused", callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return

    await callback.message.edit_text(
        f"🙏 Причина признана уважительной: "
        f"{esc(absence.get('client_name') or absence['client_id'])}, "
        f"{esc(absence['lesson_date'])}\nЗанятие не списано."
    )
    await callback.answer("🙏 Не списано")


# ==================== ПОДДЕРЖКА (сторона менеджера) ====================

@router.callback_query(F.data == "menu:manager:support")
async def manager_support_list(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    tickets = db.get_open_tickets()
    if not tickets:
        await callback.message.answer("📭 Открытых обращений нет.")
        return

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}
    for i, t in enumerate(tickets):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        messages = db.get_ticket_messages(t["id"], limit=5)
        tail = "\n".join(
            f"{'👤' if m['sender_side'] == 'user' else '👔'} {esc(m['text'])[:200]}"
            for m in messages[-3:]
        )
        await safe_call(lambda r=t, body=tail: callback.message.answer(
            f"🆘 <b>Обращение №{r['id']}</b>\n"
            f"{role_label.get(r.get('user_role'), '👤 Пользователь')}: "
            f"{esc(r.get('user_name') or '—')}\n"
            f"📞 <b>Телефон:</b> {esc(r.get('user_phone') or '—')}\n"
            f"📅 <b>Создано:</b> {esc(fmt_db_time(r.get('created_at')))}\n\n"
            f"{body}",
            parse_mode="HTML",
            reply_markup=support_manager_keyboard(r["id"]),
        ))


@router.callback_query(F.data.startswith("sup_reply:"))
async def manager_support_reply_start(callback: Callback, state: FSMContext) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    ticket_id = int(callback.data.split(":")[1])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(ManagerReplyStates.waiting_for_reply)
    await callback.message.answer(f"✍️ Напишите ответ по обращению №{ticket_id}:")
    await callback.answer()


@router.message(ManagerReplyStates.waiting_for_reply, F.text)
async def manager_support_reply_send(message: Msg, state: FSMContext, db: Database) -> None:
    if not _is_manager(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    await state.clear()

    ticket = db.get_ticket(ticket_id) if ticket_id else None
    if not ticket:
        await message.answer("❌ Обращение не найдено.")
        return
    if ticket["status"] != "open":
        await message.answer(f"ℹ️ Обращение №{ticket_id} уже закрыто.")
        return

    db.add_ticket_message(ticket_id, message.from_user.id, "manager", message.text)
    try:
        await message.bot.send_message(
            user_id=ticket["user_max_id"],
            text=f"👔 <b>Ответ менеджера</b>\n\n{esc(message.text)}",
            fmt="html",
            attachments=support_user_keyboard(ticket_id),
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        logger.warning(f"Не удалось отправить ответ по обращению {ticket_id}: {e}")
        await message.answer("⚠️ Не удалось доставить ответ пользователю.")
