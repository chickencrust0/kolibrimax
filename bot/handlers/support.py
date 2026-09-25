"""
bot/handlers/support.py — диалог «пользователь ↔ менеджер» прямо в боте.

Как это работает:
  * пользователь (родитель или преподаватель) жмёт «🆘 Связаться с
    менеджером» — открывается обращение (support_tickets) и включается
    состояние SupportStates.chatting;
  * пока состояние активно, ЛЮБОЙ текст пользователя уходит менеджерам
    как сообщение обращения, а не в другие сценарии бота — поэтому этот
    роутер подключается ПЕРВЫМ (см. main.build_dispatcher);
  * менеджер отвечает кнопкой «✍️ Ответить» (bot/handlers/manager.py);
  * закрыть обращение может любая сторона — кнопкой «🔒».

Переписка хранится в БД бота (support_messages) — в impulseCRM для неё
нет подходящей сущности.
"""

import logging
from datetime import timedelta

import settings
from database import Database
from bot.formatting import esc, safe_call
from bot.handlers.common import (
    fetch_lessons,
    is_manager,
    manager_ids,
)
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import DirectChatStates, SupportStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    manager_menu_keyboard,
    parent_menu_keyboard,
    support_manager_keyboard,
    support_user_keyboard,
    direct_chat_keyboard,
    direct_people_keyboard,
    teacher_menu_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="support")


def _menu_for(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "parent":
        return parent_menu_keyboard()
    return manager_menu_keyboard()


@router.callback_query(F.data == "menu:support")
async def support_start(
    callback: Callback, db: Database, state: FSMContext, impulse, cache
) -> None:
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала войдите в профиль.")
        return

    if user.get("role") in ("parent", "teacher"):
        await _show_direct_people(callback, db, user, impulse, cache)
        await callback.answer()
        return
    await _start_manager_support(callback, db, state, user)


async def _start_manager_support(callback: Callback, db: Database, state: FSMContext, user) -> None:
    ticket_id = db.create_ticket(
        callback.from_user.id,
        user.get("full_name") or callback.from_user.full_name,
        user.get("role") or "",
        user.get("phone") or "",
    )
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(SupportStates.chatting)

    await callback.message.answer(
        f"👤 <b>Обращение №{ticket_id}</b>\n\n"
        "Напишите ваш вопрос — он уйдёт администратору. Все следующие сообщения "
        "тоже попадут в это обращение, пока вы его не завершите.",
        parse_mode="HTML",
        reply_markup=support_user_keyboard(ticket_id),
    )
    await callback.answer()


async def _show_direct_people(
    callback: Callback, db: Database, user: dict, impulse=None, cache=None
) -> None:
    """Show only teachers/parents sharing at least one CRM lesson."""
    if impulse is None:
        await callback.message.answer("⚠️ Не удалось загрузить список собеседников.")
        return
    start = (settings.today() - timedelta(days=365)).isoformat()
    end = (settings.today() + timedelta(days=365)).isoformat()
    if user["role"] == "parent":
        lessons = await fetch_lessons(
            impulse, cache, db=db, customer_id=user["crm_id"],
            date_from=start, date_to=end,
        )
        ids = {str(tid) for lesson in lessons for tid in lesson.get("teacher_ids") or []}
        people = []
        for teacher in await _users_by_crm_ids(db, ids, "teacher"):
            people.append((teacher["max_user_id"], teacher.get("full_name") or "Преподаватель"))
        target_role = "teacher"
    else:
        lessons = await fetch_lessons(
            impulse, cache, db=db, teacher_id=user["crm_id"],
            date_from=start, date_to=end,
        )
        ids = {str(cid) for lesson in lessons for cid in lesson.get("customer_ids") or []}
        people = []
        for parent in await _users_by_crm_ids(db, ids, "parent"):
            people.append((parent["max_user_id"], parent.get("full_name") or "Родитель"))
        target_role = "parent"
    if not people:
        await callback.message.answer(
            "Пока нет доступных собеседников по общим занятиям. "
            "Можно написать администратору.",
            reply_markup=direct_people_keyboard([], target_role),
        )
        return
    await callback.message.answer(
        "💬 Выберите собеседника. В список попадают только участники ваших общих занятий:",
        reply_markup=direct_people_keyboard(people, target_role),
    )


async def _users_by_crm_ids(db: Database, ids, role: str):
    result = []
    for crm_id in ids:
        user = db.get_user_by_crm_id(crm_id, role)
        if user and user.get("is_active", 1):
            result.append(user)
    return result


@router.callback_query(F.data == "menu:support:admin")
async def support_admin(callback: Callback, db: Database, state: FSMContext) -> None:
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала войдите в профиль.")
        return
    await _start_manager_support(callback, db, state, user)


@router.callback_query(F.data.startswith("dchat_to:"))
async def direct_chat_start(
    callback: Callback, db: Database, state: FSMContext, impulse, cache
) -> None:
    user = db.get_user(callback.from_user.id)
    if not user or user.get("role") not in ("parent", "teacher"):
        await callback.answer("❌ Недоступно.")
        return
    target_id = int(callback.data.split(":", 1)[1])
    target = db.get_user(target_id)
    expected_role = "teacher" if user.get("role") == "parent" else "parent"
    if not target or target.get("role") != expected_role:
        await callback.answer("❌ Собеседник не найден.")
        return
    # Recompute the allowed set to prevent forged callbacks.
    start = (settings.today() - timedelta(days=365)).isoformat()
    end = (settings.today() + timedelta(days=365)).isoformat()
    lessons = await fetch_lessons(
        impulse, cache, db=db,
        customer_id=user["crm_id"] if user["role"] == "parent" else None,
        teacher_id=user["crm_id"] if user["role"] == "teacher" else None,
        date_from=start, date_to=end,
    )
    allowed_crm_ids = {
        str(x)
        for lesson in lessons
        for x in (
            (lesson.get("teacher_ids") or [])
            if user["role"] == "parent"
            else (lesson.get("customer_ids") or [])
        )
    }
    if str(target.get("crm_id")) not in allowed_crm_ids:
        await callback.answer("❌ Этот пользователь не связан с вашими занятиями.")
        return
    thread_id = db.create_direct_thread(
        user["max_user_id"], user["role"], target["max_user_id"], target["role"]
    )
    await state.update_data(direct_thread_id=thread_id)
    await state.set_state(DirectChatStates.chatting)
    await callback.message.answer(
        f"💬 Диалог с <b>{esc(target.get('full_name') or 'собеседником')}</b> открыт.\n"
        "Напишите сообщение — оно будет доставлено собеседнику.",
        parse_mode="HTML",
        reply_markup=direct_chat_keyboard(thread_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dchat_reply:"))
async def direct_chat_reply(callback: Callback, db: Database, state: FSMContext) -> None:
    thread_id = int(callback.data.split(":", 1)[1])
    thread = db.get_direct_thread(thread_id)
    if not thread or thread["status"] != "open" or not db.direct_thread_has_user(thread, callback.from_user.id):
        await callback.answer("❌ Диалог недоступен.")
        return
    await state.update_data(direct_thread_id=thread_id)
    await state.set_state(DirectChatStates.chatting)
    await callback.message.answer("✍️ Напишите сообщение собеседнику:")
    await callback.answer()


@router.message(DirectChatStates.chatting, F.text)
async def direct_chat_message(message: Msg, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    thread_id = data.get("direct_thread_id")
    thread = db.get_direct_thread(thread_id) if thread_id else None
    if not thread or thread["status"] != "open" or not db.direct_thread_has_user(thread, message.from_user.id):
        await state.clear()
        await message.answer("ℹ️ Диалог закрыт или недоступен.")
        return
    text = (message.text or "").strip()
    if not text:
        return
    peer_id = db.direct_thread_peer(thread, message.from_user.id)
    peer = db.get_user(peer_id) if peer_id else None
    if not peer:
        await message.answer("⚠️ Собеседник больше не доступен.")
        return
    db.add_direct_message(thread_id, message.from_user.id, text)
    await safe_call(lambda: message.bot.send_message(
        user_id=peer_id,
        text=f"💬 <b>Сообщение от {esc(message.from_user.full_name)}</b>\n\n{esc(text)}",
        fmt="html",
        attachments=direct_chat_keyboard(thread_id),
    ))
    await message.answer("✅ Сообщение доставлено.", reply_markup=direct_chat_keyboard(thread_id))


@router.callback_query(F.data.startswith("dchat_close:"))
async def direct_chat_close(callback: Callback, db: Database, state: FSMContext) -> None:
    thread_id = int(callback.data.split(":", 1)[1])
    thread = db.get_direct_thread(thread_id)
    if not thread or not db.direct_thread_has_user(thread, callback.from_user.id):
        await callback.answer("❌ Недоступно.")
        return
    peer_id = db.direct_thread_peer(thread, callback.from_user.id)
    db.close_direct_thread(thread_id, callback.from_user.id)
    await state.clear()
    await callback.message.edit_text("🔒 Диалог завершён.")
    if peer_id:
        await safe_call(lambda: callback.bot.send_message(
            user_id=peer_id, text="🔒 Собеседник завершил диалог."
        ))
    await callback.answer("Диалог закрыт")


@router.message(SupportStates.chatting, F.text)
async def support_user_message(message: Msg, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    ticket = db.get_ticket(ticket_id) if ticket_id else None

    if not ticket or ticket["status"] != "open":
        await state.clear()
        user = db.get_user(message.from_user.id)
        await message.answer(
            "ℹ️ Обращение закрыто. Чтобы задать новый вопрос, нажмите "
            "«👤 Связаться с администратором».",
            reply_markup=_menu_for((user or {}).get("role", "")),
        )
        return

    db.add_ticket_message(ticket_id, message.from_user.id, "user", message.text)

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}
    delivered = 0
    for manager_id in manager_ids(db):
        result = await safe_call(lambda mid=manager_id: message.bot.send_message(
            user_id=mid,
            text=(
                f"👤 <b>Обращение №{ticket_id}</b>\n"
                f"{role_label.get(ticket.get('user_role'), '👤 Пользователь')}: "
                f"{esc(ticket.get('user_name') or '—')}\n"
                f"📞 {esc(ticket.get('user_phone') or '—')}\n\n"
                f"💬 {esc(message.text)}"
            ),
            fmt="html",
            attachments=support_manager_keyboard(ticket_id),
        ))
        if result is not None:
            delivered += 1

    if delivered:
        await message.answer("✅ Отправлено администратору. Ожидайте ответа.")
    else:
        # Либо ADMIN_MAX_IDS пуст, либо никому не доставилось — честно
        # говорим об этом, а не создаём иллюзию, что вопрос принят.
        await message.answer(
            "⚠️ Сообщение сохранено, но администратору доставить не удалось.\n\n"
            "<i>Похоже, ни один менеджер ещё не вошёл в бота. "
            "Свяжитесь с центром по телефону.</i>"
        )


@router.callback_query(F.data.startswith("sup_close:"))
async def support_close(callback: Callback, db: Database, state: FSMContext) -> None:
    ticket_id = int(callback.data.split(":")[1])
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Обращение не найдено.")
        return

    is_manager_user = is_manager(db, callback.from_user.id)
    is_owner = callback.from_user.id == ticket["user_max_id"]
    if not (is_manager_user or is_owner):
        await callback.answer("❌ Недоступно.")
        return

    if not db.close_ticket(ticket_id, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Обращение №{ticket_id} уже закрыто.")
        await callback.answer()
        return

    # Состояние диалога снимаем только у самого пользователя: у менеджера
    # его нет, а чужое состояние из этого контекста не достать.
    if is_owner:
        await state.clear()

    await callback.message.edit_text(f"🔒 Обращение №{ticket_id} закрыто.")

    if is_manager_user and not is_owner:
        user = db.get_user(ticket["user_max_id"])
        await safe_call(lambda: callback.bot.send_message(
            user_id=ticket["user_max_id"],
            text=f"🔒 Обращение №{ticket_id} закрыто администратором.",
            attachments=_menu_for((user or {}).get("role", "")),
        ))
    elif is_owner:
        for manager_id in manager_ids(db):
            await safe_call(lambda mid=manager_id: callback.bot.send_message(
                user_id=mid,
                text=f"🔒 Обращение №{ticket_id} закрыто пользователем.",
            ))
        user = db.get_user(callback.from_user.id)
        await callback.message.answer(
            "Чем ещё помочь?", reply_markup=_menu_for((user or {}).get("role", ""))
        )

    await callback.answer("🔒 Закрыто")
