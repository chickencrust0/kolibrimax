"""
bot/handlers/start.py — вход по номеру телефона, меню роли, выход.

Портировано из alfacrm-bot. Главное отличие: в MAX нет постоянной
reply-клавиатуры (см. max_api/keyboards.py), поэтому вместо кнопки
«🚪 Выйти из профиля» как текста меню используется callback
"menu:logout" — сама раскладка/подписи кнопок не изменились.
"""

import logging

import settings
from impulse_client import ImpulseCRMClient, ImpulseCRMError
from database import Database
from bot.formatting import esc
from bot.dispatcher import CommandStart, F, Router
from bot.fsm import FSMContext
from max_api.context import Callback, Msg
from max_api.keyboards import (
    confirm_logout_keyboard,
    manager_menu_keyboard,
    parent_menu_keyboard,
    request_phone_keyboard,
    teacher_menu_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="start")

ROLE_LABELS = {
    "teacher": "👨‍🏫 Преподаватель",
    "parent": "👨‍👩‍👧 Родитель",
    "manager": "👑 Менеджер",
}


def get_menu_by_role(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "parent":
        return parent_menu_keyboard()
    return manager_menu_keyboard()


def _login_manager(db: Database, message: Msg, phone: str = "") -> None:
    db.link_user(
        max_user_id=message.from_user.id,
        crm_id=0,
        role="manager",
        phone=phone,
        full_name=message.from_user.full_name or "Менеджер",
    )


@router.message(CommandStart())
async def cmd_start(message: Msg, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)

    if not user and message.from_user.id in settings.MANAGER_IDS:
        # Менеджеру телефон не нужен — он опознаётся по ID в MAX.
        _login_manager(db, message)
        user = db.get_user(message.from_user.id)

    if user:
        role = user["role"]
        crm_line = (
            f"🆔 CRM ID: <code>{user['crm_id']}</code>\n" if user["crm_id"] else ""
        )
        await message.answer(
            f"👋 С возвращением, <b>{esc(user['full_name'])}</b>!\n\n"
            f"📊 Роль: {ROLE_LABELS.get(role, esc(role))}\n"
            f"{crm_line}\n"
            f"👇 Выберите раздел:",
            reply_markup=get_menu_by_role(role),
            parse_mode="HTML",
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для входа поделитесь номером телефона.\n\n"
        "📱 Нажмите кнопку ниже или напишите номер вручную\n"
        "<i>Формат: +7(XXX)XXX-XX-XX или 8XXXXXXXXXX</i>",
        reply_markup=request_phone_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.contact)
async def handle_contact(
    message: Msg, db: Database, impulse: ImpulseCRMClient, state: FSMContext
) -> None:
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        # Чужой контакт: иначе можно войти под любым номером из адресной книги.
        await message.answer(
            "❌ Поделитесь, пожалуйста, <b>своим</b> контактом.",
            parse_mode="HTML",
            reply_markup=request_phone_keyboard(),
        )
        return
    await state.clear()
    await process_phone_login(message, db, impulse, message.contact.phone_number)


@router.message(F.text.regexp(r"^\+?\d[\d\-\(\)\s]{5,}$"))
async def handle_manual_phone(
    message: Msg, db: Database, impulse: ImpulseCRMClient, state: FSMContext
) -> None:
    if db.get_user(message.from_user.id):
        # Уже авторизован — пропускаем, пусть обработают другие роутеры.
        return
    await state.clear()
    await process_phone_login(message, db, impulse, message.text.strip())


async def process_phone_login(
    message: Msg, db: Database, impulse: ImpulseCRMClient, phone: str
) -> None:
    max_user_id = message.from_user.id
    logger.info(f"Вход по телефону для {max_user_id}")

    if max_user_id in settings.MANAGER_IDS:
        _login_manager(db, message, phone)
        await message.answer("✅ Вы вошли как менеджер.", reply_markup=manager_menu_keyboard())
        return

    try:
        teacher = await impulse.find_teacher_by_phone(phone)
    except ImpulseCRMError as e:
        logger.warning(f"Поиск преподавателя не удался: {e}")
        teacher = None

    if teacher:
        name = impulse.extract_user_name(teacher)
        db.link_user(
            max_user_id=max_user_id, crm_id=teacher["id"], role="teacher",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>! (Преподаватель)",
            parse_mode="HTML",
            reply_markup=teacher_menu_keyboard(),
        )
        return

    try:
        customer = await impulse.find_customer_by_phone(phone)
    except ImpulseCRMError as e:
        logger.warning(f"Поиск клиента не удался: {e}")
        customer = None

    if customer:
        name = impulse.extract_user_name(customer)
        db.link_user(
            max_user_id=max_user_id, crm_id=customer["id"], role="parent",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>!",
            parse_mode="HTML",
            reply_markup=parent_menu_keyboard(),
        )
        return

    await message.answer(
        "Номер не найден. Попробуйте ещё раз или обратитесь к менеджеру.",
        reply_markup=request_phone_keyboard(),
    )


@router.callback_query(F.data == "menu:logout")
async def logout_start(callback: Callback) -> None:
    await callback.message.answer(
        "⚠️ Вы уверены, что хотите выйти?", reply_markup=confirm_logout_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("logout:"))
async def logout_process(callback: Callback, db: Database, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "confirm":
        await state.clear()
        db.deactivate_user(callback.from_user.id)
        await callback.message.edit_text("👋 Вы вышли.")
        await callback.message.answer(
            "Для входа поделитесь номером телефона:", reply_markup=request_phone_keyboard()
        )
    else:
        await callback.message.edit_text("❌ Отменено.")
    await callback.answer()
