"""
bot/handlers/parent.py — расписание, ДЗ, баланс, заявка на перенос со
стороны родителя.

Портировано из alfacrm-bot: пункты меню теперь callback_query
(F.data == "menu:parent:...") вместо F.text == "<подпись кнопки>" —
см. докстринг bot/handlers/teacher.py про отсутствие reply-клавиатуры в MAX.
"""

import asyncio
import logging
from datetime import timedelta

import settings
from impulse_client import ImpulseCRMClient, ImpulseCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    STATUS_CANCELLED,
    STATUS_CONDUCTED,
    answer_blocks,
    build_schedule,
    esc,
    format_homework_card,
    lesson_sort_key,
    format_lesson,  # noqa: F401  (используется через build_schedule)
    safe_call,
)
from bot.handlers.common import fetch_lessons
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import ParentTransferStates
from max_api.context import Callback, Msg
from max_api.keyboards import transfer_decision_keyboard

logger = logging.getLogger(__name__)
router = Router(name="parent")


def _parent(db: Database, max_user_id: int):
    user = db.get_user(max_user_id)
    return user if user and user["role"] == "parent" else None


@router.callback_query(F.data == "menu:parent:schedule")
async def parent_schedule(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для родителей.")
        return
    await callback.answer()

    today = settings.today()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=7)).isoformat()

    try:
        lessons = await fetch_lessons(
            impulse, cache,
            customer_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except ImpulseCRMError as e:
        await callback.message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    lessons = [l for l in lessons if l.get("status") != STATUS_CANCELLED]

    blocks = build_schedule(
        lessons,
        role="parent",
        title="Расписание на неделю",
        empty_text="На ближайшую неделю занятий нет.",
        today=today,
    )
    await answer_blocks(callback.message, blocks)


@router.callback_query(F.data == "menu:parent:homework")
async def parent_homework(
    callback: Callback, db: Database, impulse: ImpulseCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для родителей.")
        return
    await callback.answer()

    today = settings.today()
    date_from = (today - timedelta(days=14)).isoformat()

    try:
        # Период указывается всегда — без него уходил бы запрос вообще
        # без ограничений, выгружая всю историю занятий ученика ради
        # двух недель ДЗ.
        lessons = await fetch_lessons(
            impulse, cache,
            customer_id=user["crm_id"],
            status=STATUS_CONDUCTED,
            date_from=date_from,
            date_to=today.isoformat(),
        )
    except ImpulseCRMError as e:
        await callback.message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    lessons_with_hw = [l for l in lessons if (l.get("homework") or "").strip()]
    if not lessons_with_hw:
        await callback.message.answer("📚 Домашних заданий за последние 2 недели нет.")
        return

    await callback.message.answer(
        f"📚 <b>Домашние задания</b> ({len(lessons_with_hw)})", parse_mode="HTML"
    )

    for lesson in sorted(lessons_with_hw, key=lesson_sort_key, reverse=True):
        files = db.get_homework_files(lesson.get("id")) if lesson.get("id") else []
        card = format_homework_card(lesson, files_count=len(files), today=today)

        await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda c=card: callback.message.answer(c, parse_mode="HTML"))

        for f in files:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
            att_type = "image" if f.get("file_type") == "photo" else "file"
            try:
                await safe_call(lambda token=f["file_id"], t=att_type: callback.bot.send_message(
                    user_id=callback.from_user.id,
                    attachments=[{"type": t, "payload": {"token": token}}],
                ))
            except Exception as e:
                logger.warning(f"Не удалось отправить файл ДЗ {f.get('file_id')}: {e}")


@router.callback_query(F.data == "menu:parent:balance")
async def parent_balance(callback: Callback, db: Database, impulse: ImpulseCRMClient) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для родителей.")
        return
    await callback.answer()

    try:
        customer = await impulse.get_customer_info(user["crm_id"])
    except ImpulseCRMError as e:
        await callback.message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    if not customer:
        await callback.message.answer("❌ Не удалось получить данные.")
        return

    paid = int(customer.get("paid_lesson_count") or 0)
    used = int(customer.get("paid_count") or 0)
    remaining = max(0, paid - used)

    await callback.message.answer(
        f"💰 <b>Абонемент</b>\n\n"
        f"💵 <b>Баланс:</b> {esc(customer.get('balance', '0'))} руб.\n"
        f"📅 <b>Оплачено занятий:</b> {paid}\n"
        f"✅ <b>Проведено:</b> {used}\n"
        f"🎟 <b>Осталось:</b> {remaining}\n"
        f"➡️ <b>Следующее занятие:</b> {esc(customer.get('next_lesson_date') or '—')}\n"
        f"⬅️ <b>Последнее посещение:</b> {esc(customer.get('last_attend_date') or '—')}",
        parse_mode="HTML",
    )


# ==================== ЗАЯВКА НА ПЕРЕНОС ====================

@router.callback_query(F.data == "menu:parent:transfer")
async def parent_transfer_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _parent(db, callback.from_user.id):
        await callback.answer("❌ Только для родителей.")
        return

    await state.set_state(ParentTransferStates.waiting_for_comment)
    example_date = (settings.today() + timedelta(days=1)).strftime("%d.%m.%Y")
    await callback.message.answer(
        "🔁 <b>Заявка на перенос</b>\n\n"
        "Напишите дату, время урока и причину переноса.\n"
        "Заявка будет отправлена менеджеру.\n\n"
        f"<i>Пример: {example_date}, 15:00, хотим перенести на следующий день</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ParentTransferStates.waiting_for_comment, F.text)
async def parent_transfer_send(message: Msg, state: FSMContext, db: Database) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    comment = message.text
    await state.clear()

    request_id = db.create_transfer_request(
        message.from_user.id, None, comment, author_role="parent"
    )
    sent_at = settings.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in settings.MANAGER_IDS:
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                user_id=mid,
                text=(
                    f"🔁 <b>Заявка на перенос №{request_id} (от родителя)</b>\n\n"
                    f"📅 <b>Получена:</b> {sent_at}\n"
                    f"👤 <b>Родитель:</b> {esc(user['full_name'])}\n"
                    f"🆔 <b>CRM ID:</b> {esc(user['crm_id'])}\n"
                    f"📞 <b>Телефон:</b> {esc(user['phone'])}\n"
                    f"💬 <b>Комментарий:</b> {esc(comment)}"
                ),
                fmt="html",
                attachments=transfer_decision_keyboard(request_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
