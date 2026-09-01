"""
bot/dispatcher.py — минимальный Router/Dispatcher вместо aiogram.

Поддерживает ровно то подмножество возможностей aiogram, которое
использовалось в исходном боте: фильтры по тексту (regexp/наличие),
по наличию contact/document/photo, по callback-данным (== / startswith),
привязку хендлера к состоянию FSM и последующему тексту, плюс
инъекцию зависимостей в хендлер по именам параметров.

Меню бота в MAX реализованы через inline-кнопки (см. max_api/keyboards.py
и докстринг там же), поэтому в отличие от исходного бота на Telegram
здесь нет фильтров вида F.text == "<подпись кнопки>" — вместо них
хендлеры меню триггерятся через F.data == "menu:...".
"""

import inspect
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Union

from max_api.client import MaxBot
from max_api.context import Callback, Msg
from bot.fsm import FSMContext

logger = logging.getLogger(__name__)

CondFn = Callable[[Any], bool]


class Cond:
    """Обёртка над предикатом ctx -> bool с поддержкой | и &, как у aiogram F."""

    def __init__(self, fn: CondFn):
        self.fn = fn

    def __call__(self, ctx: Any) -> bool:
        try:
            return bool(self.fn(ctx))
        except Exception:
            return False

    def __or__(self, other: "Cond") -> "Cond":
        return Cond(lambda ctx: self.fn(ctx) or other.fn(ctx))

    def __and__(self, other: "Cond") -> "Cond":
        return Cond(lambda ctx: self.fn(ctx) and other.fn(ctx))


class _TextField:
    """
    F.text используется двумя способами: как готовый фильтр «в сообщении
    есть текст» и как F.text.regexp(...). Поэтому объект должен быть и
    вызываемым (Cond-совместимым), и иметь метод regexp.
    """

    def __call__(self, ctx: Any) -> bool:
        return bool(getattr(ctx, "text", "") or "")

    def regexp(self, pattern: str) -> Cond:
        compiled = re.compile(pattern)
        return Cond(lambda ctx: bool(compiled.match(getattr(ctx, "text", "") or "")))

    def __or__(self, other) -> Cond:
        return Cond(lambda ctx: self(ctx) or other(ctx))

    def __and__(self, other) -> Cond:
        return Cond(lambda ctx: self(ctx) and other(ctx))


class _DataField:
    def __eq__(self, other) -> Cond:  # type: ignore[override]
        return Cond(lambda ctx: getattr(ctx, "data", "") == other)

    def startswith(self, prefix: str) -> Cond:
        return Cond(lambda ctx: (getattr(ctx, "data", "") or "").startswith(prefix))


class _F:
    text = _TextField()
    data = _DataField()
    contact = Cond(lambda ctx: getattr(ctx, "contact", None) is not None)
    document = Cond(lambda ctx: getattr(ctx, "document", None) is not None)
    photo = Cond(lambda ctx: getattr(ctx, "photo", None) is not None)


F = _F()


def CommandStart() -> Cond:
    return Cond(
        lambda ctx: getattr(ctx, "update_type", "") == "bot_started"
        or (getattr(ctx, "text", "") or "").strip().lower() == "/start"
    )


Filter = Union[str, Cond, Any]


class _Entry:
    __slots__ = ("filters", "func")

    def __init__(self, filters: List[Filter], func: Callable):
        self.filters = filters
        self.func = func


class Router:
    def __init__(self, name: str = ""):
        self.name = name
        self._message_entries: List[_Entry] = []
        self._callback_entries: List[_Entry] = []

    def message(self, *filters: Filter):
        def deco(func: Callable) -> Callable:
            self._message_entries.append(_Entry(list(filters), func))
            return func
        return deco

    def callback_query(self, *filters: Filter):
        def deco(func: Callable) -> Callable:
            self._callback_entries.append(_Entry(list(filters), func))
            return func
        return deco


def _matches(entry: _Entry, ctx: Any, current_state: Optional[str]) -> bool:
    for f in entry.filters:
        if isinstance(f, str):
            if current_state != f:
                return False
        else:
            # f может быть Cond или другим вызываемым фильтром (например,
            # F.text, используемый напрямую как «есть текст»).
            try:
                if not f(ctx):
                    return False
            except Exception:
                return False
    return True


class Dispatcher:
    def __init__(self, bot: MaxBot, **workflow_data: Any):
        self.bot = bot
        self.workflow_data = workflow_data
        self._routers: List[Router] = []

    def include_router(self, router: Router) -> None:
        self._routers.append(router)

    async def _call(self, func: Callable, ctx: Any, ctx_kind: str, user_id: int) -> None:
        sig = inspect.signature(func)
        available: Dict[str, Any] = dict(self.workflow_data)
        available["state"] = FSMContext(user_id)
        available[ctx_kind] = ctx  # "message" или "callback"
        kwargs = {name: available[name] for name in sig.parameters if name in available}
        try:
            await func(**kwargs)
        except Exception:
            logger.exception(f"Ошибка в хендлере {func.__module__}.{func.__name__}")

    async def feed_update(self, update: Dict[str, Any]) -> None:
        update_type = update.get("update_type", "")
        try:
            if update_type in ("message_created", "bot_started"):
                ctx = Msg(self.bot, update)
                if not ctx.from_user.id:
                    return
                state = FSMContext(ctx.from_user.id)
                current_state = await state.get_state()
                for router in self._routers:
                    for entry in router._message_entries:
                        if _matches(entry, ctx, current_state):
                            await self._call(entry.func, ctx, "message", ctx.from_user.id)
                            return
            elif update_type == "message_callback":
                ctx = Callback(self.bot, update)
                if not ctx.from_user.id:
                    return
                state = FSMContext(ctx.from_user.id)
                current_state = await state.get_state()
                for router in self._routers:
                    for entry in router._callback_entries:
                        if _matches(entry, ctx, current_state):
                            await self._call(entry.func, ctx, "callback", ctx.from_user.id)
                            return
            # Прочие типы апдейтов (bot_added, chat_title_changed и т.п.) этому
            # боту не нужны — он работает только в личных диалогах 1-на-1.
        except Exception:
            logger.exception(f"Необработанная ошибка при обработке апдейта {update_type}")
