"""
main.py — точка входа.

Портировано из alfacrm-bot: тот же порядок запуска (клиент CRM -> кеш ->
БД -> планировщик -> диспетчер апдейтов), только источник апдейтов —
MAX (long polling по умолчанию, вебхук — через MAX_MODE=webhook).

Long Polling подходит для разработки/тестирования, но не для
продакшена (см. dev.max.ru/docs-api, «Рекомендации по работе с API») —
переключитесь на MAX_MODE=webhook и настройте HTTPS с доверенным
сертификатом (в т.ч. Минцифры) перед боевым запуском.
"""

import asyncio
import logging
import signal

from aiohttp import web

import settings
from cache import LessonCache
from database import Database
from impulse_client import ImpulseCRMClient
from max_api.client import MaxBot
from scheduler import ReminderScheduler
from bot.dispatcher import Dispatcher
from bot.handlers import common as _common  # noqa: F401  (регистрирует реэкспорт)
from bot.handlers import manager, parent, start, support, teacher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

UPDATE_TYPES = [
    "message_created",
    "message_callback",
    "bot_started",
]


def _check_config() -> None:
    problems = []
    if not settings.MAX_BOT_TOKEN:
        problems.append("MAX_BOT_TOKEN не задан")
    if not settings.IMPULSE_DOMAIN:
        problems.append("IMPULSE_DOMAIN не задан")
    if not settings.IMPULSE_LOGIN or not settings.IMPULSE_API_KEY:
        problems.append("IMPULSE_LOGIN/IMPULSE_API_KEY не заданы")
    if not settings.MANAGER_IDS:
        problems.append("ADMIN_MAX_IDS пуст — некому будет прийти сводкам и заявкам")
    if settings.TZ_ERROR:
        problems.append(f"часовой пояс: {settings.TZ_ERROR}")

    for p in problems:
        logger.warning(f"⚠️ {p}")


def build_dispatcher(bot: MaxBot, db: Database, impulse: ImpulseCRMClient, cache: LessonCache) -> Dispatcher:
    dp = Dispatcher(bot, db=db, impulse=impulse, cache=cache)
    # Порядок важен: start обрабатывает /start и логин раньше остальных
    # текстовых фильтров (например, F.text.regexp с номером телефона).
    dp.include_router(start.router)
    # support — раньше остальных: пока пользователь в диалоге с менеджером,
    # его текст должен уходить в обращение, а не в другие сценарии.
    dp.include_router(support.router)
    dp.include_router(teacher.router)
    dp.include_router(parent.router)
    dp.include_router(manager.router)
    return dp


async def run_polling(bot: MaxBot, dp: Dispatcher) -> None:
    logger.info("▶️ Запуск в режиме long polling")
    marker = None
    while True:
        try:
            result = await bot.get_updates(
                marker=marker, limit=100, timeout=settings.MAX_LONGPOLL_TIMEOUT, types=UPDATE_TYPES
            )
        except Exception as e:
            logger.error(f"Ошибка long polling: {e}")
            await asyncio.sleep(2)
            continue

        updates = (result or {}).get("updates") or []
        for update in updates:
            asyncio.create_task(dp.feed_update(update))

        new_marker = (result or {}).get("marker")
        if new_marker is not None:
            marker = new_marker


async def run_webhook(bot: MaxBot, dp: Dispatcher) -> None:
    logger.info(f"▶️ Запуск в режиме webhook на {settings.MAX_WEBHOOK_HOST}:{settings.MAX_WEBHOOK_PORT}")

    async def handle(request: web.Request) -> web.Response:
        if settings.MAX_WEBHOOK_SECRET:
            if request.headers.get("X-Max-Bot-Api-Secret") != settings.MAX_WEBHOOK_SECRET:
                return web.Response(status=401)
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400)
        asyncio.create_task(dp.feed_update(update))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post(settings.MAX_WEBHOOK_PATH, handle)

    if settings.MAX_WEBHOOK_URL:
        try:
            await bot.subscribe(
                settings.MAX_WEBHOOK_URL, UPDATE_TYPES, settings.MAX_WEBHOOK_SECRET or None
            )
            logger.info(f"✅ Подписка на вебхук оформлена: {settings.MAX_WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"❌ Не удалось оформить подписку на вебхук: {e}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.MAX_WEBHOOK_HOST, settings.MAX_WEBHOOK_PORT)
    await site.start()

    stop_event = asyncio.Event()
    await stop_event.wait()


async def main() -> None:
    _check_config()

    bot = MaxBot(settings.MAX_BOT_TOKEN)
    try:
        me = await bot.get_me()
        logger.info(f"🤖 Бот подключён: {me.get('name') or me.get('username') or me}")
    except Exception as e:
        logger.error(f"❌ Не удалось подключиться к MAX: {e}")

    impulse = ImpulseCRMClient(
        domain=settings.IMPULSE_DOMAIN,
        login=settings.IMPULSE_LOGIN,
        api_key=settings.IMPULSE_API_KEY,
    )
    db = Database(settings.DB_PATH)
    cache = LessonCache(impulse)
    cache.start()

    reminder_scheduler = ReminderScheduler(db, impulse, bot, cache)
    reminder_scheduler.start()

    dp = build_dispatcher(bot, db, impulse, cache)

    stop = asyncio.Event()

    def _on_signal():
        logger.info("⏹ Получен сигнал остановки")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    runner_task = asyncio.create_task(
        run_webhook(bot, dp) if settings.MAX_MODE == "webhook" else run_polling(bot, dp)
    )

    # На Windows loop.add_signal_handler недоступен (NotImplementedError
    # выше), поэтому Ctrl+C приходит в виде KeyboardInterrupt прямо в
    # asyncio.run() и отменяет текущую задачу — раньше это пропускало
    # блок очистки ниже целиком (cache.stop()/scheduler.stop()/close()),
    # отсюда "Unclosed client session" при выходе. try/finally гарантирует
    # очистку при любом способе остановки.
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        runner_task.cancel()
        try:
            await runner_task
        except (asyncio.CancelledError, Exception):
            pass
        cache.stop()
        reminder_scheduler.stop()
        await impulse.close()
        await bot.close()
        logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
