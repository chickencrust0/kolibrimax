"""
scheduler.py — фоновые задания: утренняя рассылка, напоминания о скором
уроке, ежедневная сводка менеджеру, проверка баланса, чистка лога.

Портировано из alfacrm-bot без изменений в логике задач — только вызовы
alfacrm -> impulse и bot.send_message(chat_id, text, ...) -> MaxBot API
(user_id=, text=, attachments=).
"""

import asyncio
import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import settings
from impulse_client import ImpulseCRMClient, ImpulseCRMError
from database import Database
from bot.formatting import (
    STATUS_PLANNED,
    build_schedule,
    esc,
    fmt_date_long,
    format_reminder,
    parse_lesson_datetime,
    safe_call,
    send_blocks,
    split_messages,
)
from bot.handlers.common import fetch_lessons, get_lesson_summary, load_customer_map
from max_api.keyboards import absence_decision_keyboard, lesson_action_keyboard

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, db: Database, impulse: ImpulseCRMClient, bot, cache=None):
        self.db = db
        self.impulse = impulse
        self.bot = bot
        self.cache = cache
        # Все задания живут в часовом поясе филиала, а не сервера.
        self.scheduler = AsyncIOScheduler(timezone=settings.TZ)
        self.manager_ids = settings.MANAGER_IDS

    def start(self):
        common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
        self.scheduler.add_job(
            self.send_daily_schedule, CronTrigger(hour=8, minute=0, timezone=settings.TZ),
            id="daily", **common,
        )
        self.scheduler.add_job(
            self.check_upcoming_lessons, IntervalTrigger(minutes=5), id="upcoming", **common,
        )
        self.scheduler.add_job(
            self.check_low_balance, CronTrigger(hour=10, minute=0, timezone=settings.TZ),
            id="balance", **common,
        )
        self.scheduler.add_job(
            self.send_daily_summary, CronTrigger(hour=0, minute=1, timezone=settings.TZ),
            id="daily_summary", **common,
        )
        self.scheduler.add_job(
            self.send_absence_digest,
            CronTrigger(
                hour=settings.ABSENCE_DIGEST_HOUR,
                minute=settings.ABSENCE_DIGEST_MINUTE,
                timezone=settings.TZ,
            ),
            id="absence_digest", **common,
        )
        self.scheduler.add_job(
            self.cleanup, CronTrigger(hour=3, minute=30, timezone=settings.TZ),
            id="cleanup", **common,
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен (TZ={settings.TIMEZONE})")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("⏹ Планировщик остановлен")

    # ==================== УТРЕННЯЯ РАССЫЛКА ====================

    async def send_daily_schedule(self):
        today = settings.today()
        today_iso = today.isoformat()

        try:
            customers = await load_customer_map(self.impulse)
        except Exception as e:
            logger.error(f"Не удалось загрузить карту учеников: {e}")
            customers = {}

        for role in ("teacher", "parent"):
            for user in self.db.get_all_users_by_role(role):
                try:
                    kwargs = (
                        {"teacher_id": user["crm_id"]} if role == "teacher"
                        else {"customer_id": user["crm_id"]}
                    )
                    lessons = await fetch_lessons(
                        self.impulse, self.cache,
                        status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
                        **kwargs,
                    )
                    if not lessons:
                        continue

                    blocks = build_schedule(
                        lessons,
                        role=role,
                        title=f"Расписание на сегодня, {fmt_date_long(today)}",
                        customers=customers,
                        today=today,
                    )
                    await send_blocks(self.bot, user["max_user_id"], blocks)
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['max_user_id']}: {e}")

    # ==================== НАПОМИНАНИЯ ====================

    async def check_upcoming_lessons(self):
        now = settings.now()
        today_iso = now.date().isoformat()

        try:
            lessons = await fetch_lessons(
                self.impulse, self.cache,
                status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
            )
        except ImpulseCRMError as e:
            logger.error(f"Ошибка проверки уроков: {e}")
            return

        for lesson in lessons:
            lesson_time = parse_lesson_datetime(lesson)
            if not lesson_time:
                continue

            minutes_left = (lesson_time - now).total_seconds() / 60
            if 55 <= minutes_left <= 65:
                await self._notify_lesson(lesson, "1 час")
            elif 12 <= minutes_left <= 18:
                await self._notify_lesson(lesson, "15 минут")

    async def _notify_lesson(self, lesson, when: str):
        lesson_id = lesson.get("id")
        if not lesson_id:
            return

        # Тип напоминания один и тот же при записи и при проверке —
        # если эти строки разойдутся, дедупликация перестанет работать
        # и напоминания задублируются на каждом тике планировщика.
        reminder_type = f"upcoming_{when}"

        try:
            customers = await load_customer_map(self.impulse)
        except Exception:
            customers = {}

        for teacher_id in lesson.get("teacher_ids") or []:
            teacher = self.db.get_user_by_crm_id(teacher_id, "teacher")
            if not teacher:
                continue
            if self.db.was_reminder_sent(lesson_id, reminder_type, teacher["max_user_id"], hours=6):
                continue
            try:
                await safe_call(lambda t=teacher: self.bot.send_message(
                    user_id=t["max_user_id"],
                    text=format_reminder(lesson, when=when, role="teacher", customers=customers),
                    fmt="html",
                    attachments=lesson_action_keyboard(lesson_id),
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, teacher["max_user_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить преподавателю {teacher['max_user_id']}: {e}")

        for customer_id in lesson.get("customer_ids") or []:
            parent = self.db.get_user_by_crm_id(customer_id, "parent")
            if not parent:
                continue
            if self.db.was_reminder_sent(lesson_id, reminder_type, parent["max_user_id"], hours=6):
                continue
            try:
                await safe_call(lambda p=parent: self.bot.send_message(
                    user_id=p["max_user_id"],
                    text=format_reminder(lesson, when=when, role="parent"),
                    fmt="html",
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, parent["max_user_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить родителю {parent['max_user_id']}: {e}")

    # ==================== ЕЖЕДНЕВНАЯ СВОДКА ====================

    async def send_daily_summary(self):
        yesterday = settings.today() - timedelta(days=1)
        yesterday_iso = yesterday.isoformat()
        period_label = f"за {fmt_date_long(yesterday)}"

        try:
            lessons = await fetch_lessons(
                self.impulse, self.cache, date_from=yesterday_iso, date_to=yesterday_iso
            )
        except Exception as e:
            logger.error(f"❌ Ошибка получения уроков для сводки: {e}", exc_info=True)
            return

        try:
            blocks = await get_lesson_summary(lessons, self.db, self.impulse, period_label)
        except Exception as e:
            logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
            return

        for manager_id in self.manager_ids:
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")

    # ==================== БАЛАНС ====================

    @staticmethod
    def _remaining_lessons(customer) -> int:
        # paid_lesson_count — оплачено, paid_count — израсходовано.
        paid = int(customer.get("paid_lesson_count") or 0)
        used = int(customer.get("paid_count") or 0)
        return max(0, paid - used)

    async def check_low_balance(self):
        today = settings.today()
        try:
            # Один проход по клиентам + один по абонементам, а не
            # get_customer_info() в цикле (это было бы N лишних полных
            # перевыгрузок списка абонементов на каждого клиента).
            clients = await self.impulse.load_all_clients()
            balances = await self.impulse.get_balances_map()
            customers = []
            for c in clients:
                entry = balances.get(c.get("id"), {"paid": 0, "used": 0})
                merged = dict(c)
                merged["paid_lesson_count"] = entry["paid"]
                merged["paid_count"] = entry["used"]
                # Денежный баланс лежит прямо на client.deposit, а не в
                # сущности абонемента (подтверждено снятой схемой аккаунта).
                merged["balance"] = c.get(settings.IMPULSE_FIELD_CLIENT_BALANCE) or 0
                customers.append(merged)
        except ImpulseCRMError as e:
            logger.error(f"Ошибка проверки баланса: {e}")
            return

        low_balance = [
            c for c in customers
            if self._remaining_lessons(c) <= settings.LOW_BALANCE_THRESHOLD
        ]
        if not low_balance:
            return

        for customer in low_balance:
            parent = self.db.get_user_by_crm_id(customer.get("id"), "parent")
            if not parent:
                continue
            if self.db.was_reminder_sent(
                int(customer["id"]), "low_balance", parent["max_user_id"], hours=20
            ):
                continue
            try:
                await safe_call(lambda p=parent, c=customer: self.bot.send_message(
                    user_id=p["max_user_id"],
                    text=(
                        f"⚠️ <b>Заканчивается абонемент</b>\n\n"
                        f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}\n"
                        f"🎟 <b>Осталось занятий:</b> {self._remaining_lessons(c)}\n"
                        f"💰 <b>Баланс:</b> {esc(c.get('balance', '0'))} руб.\n"
                        f"➡️ <b>Следующее занятие:</b> {esc(c.get('next_lesson_date') or '—')}\n\n"
                        f"Свяжитесь с менеджером для продления."
                    ),
                    fmt="html",
                ))
                self.db.mark_reminder_sent(
                    int(customer["id"]), "low_balance", parent["max_user_id"]
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить родителя {parent['max_user_id']}: {e}")

        lines = [
            f"⚠️ <b>Заканчивается абонемент у {len(low_balance)} учеников</b>",
            f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}",
            "",
        ]
        for customer in low_balance:
            name = self.impulse.extract_user_name(customer)
            lines.append(f"👤 <b>{esc(name)}</b> (ID: {esc(customer.get('id'))})")
            lines.append(f"   🎟 Осталось: {self._remaining_lessons(customer)} занятий")
            lines.append(f"   💰 Баланс: {esc(customer.get('balance', '0'))} руб.")
            lines.append(f"   ➡️ След. занятие: {esc(customer.get('next_lesson_date') or '—')}")
            lines.append("")

        blocks = split_messages(["\n".join(lines)])
        for manager_id in self.manager_ids:
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось уведомить менеджера {manager_id}: {e}")

    # ==================== СВОДКА ПО НЕЯВКАМ ====================

    async def send_absence_digest(self):
        """
        Вечерняя сводка менеджеру: кто сегодня не пришёл. У каждой неявки
        кнопки «занятие сгорело» (списать через burn_one) и
        «уважительная» (не списывать).
        """
        today_iso = settings.today().isoformat()
        absences = self.db.get_absences_for_date(today_iso, status="pending")
        if not absences:
            logger.info(f"Неявок за {today_iso} нет — сводка не отправляется")
            return

        for manager_id in self.manager_ids:
            try:
                await safe_call(lambda mid=manager_id: self.bot.send_message(
                    user_id=mid,
                    text=(
                        f"❌ <b>Неявки за {esc(today_iso)}</b>\n"
                        f"Всего: {len(absences)}"
                    ),
                    fmt="html",
                ))
                for i, a in enumerate(absences):
                    if i:
                        await asyncio.sleep(settings.MAX_SEND_DELAY)
                    await safe_call(lambda mid=manager_id, r=a: self.bot.send_message(
                        user_id=mid,
                        text=(
                            f"👤 <b>{esc(r.get('client_name') or r['client_id'])}</b>\n"
                            f"📅 <b>Дата:</b> {esc(r['lesson_date'])}\n"
                            f"🆔 <b>Занятие:</b> {esc(r['lesson_id'])}"
                        ),
                        fmt="html",
                        attachments=absence_decision_keyboard(r["id"]),
                    ))
            except Exception as e:
                logger.error(f"Не удалось отправить сводку по неявкам {manager_id}: {e}")

    # ==================== ОБСЛУЖИВАНИЕ ====================

    async def cleanup(self):
        try:
            removed = self.db.cleanup_reminder_log(days=30)
            logger.info(f"🧹 Лог напоминаний: удалено {removed} записей")
        except Exception as e:
            logger.error(f"Не удалось почистить лог напоминаний: {e}")
