"""Teacher child selection and direct-message delivery regressions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.fsm import FSMContext
from bot.handlers import support
from bot.states import DirectChatStates
from database import Database
from max_api.keyboards import direct_children_keyboard


class DirectChatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "bot.db"))
        self.db.link_user(10, 100, "teacher", "", "Педагог")
        self.db.link_user(20, 200, "parent", "", "Первый родитель")
        self.db.link_user(21, 200, "parent", "", "Второй родитель")
        self.state = FSMContext(10)
        await self.state.clear()
        self.addAsyncCleanup(self.state.clear)
        self.callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10, full_name="Педагог"),
            message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock(),
            data="menu:support",
        )
        self.lessons = patch.object(support, "fetch_lessons", AsyncMock(return_value=[
            {"customer_ids": [200, "201", 200], "teacher_ids": [100]},
        ]))
        self.fetch = self.lessons.start()
        self.addCleanup(self.lessons.stop)
        names = patch.object(support, "load_customer_map", AsyncMock(return_value={
            200: "Анна", 201: "Борис",
        }))
        names.start()
        self.addCleanup(names.stop)

    def buttons(self):
        markup = self.callback.message.answer.call_args.kwargs["reply_markup"]
        return [button for row in markup[0]["payload"]["buttons"] for button in row]

    async def test_children_visible_without_registered_parent(self):
        await self.state.set_state(DirectChatStates.chatting)
        await support.support_start(self.callback, self.db, self.state, object(), None)
        children = [b for b in self.buttons() if b["payload"].startswith("dchat_child:")]
        self.assertEqual([b["text"] for b in children], ["👦 Анна", "👦 Борис"])
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.fetch.call_args.kwargs["teacher_id"], 100)

    async def test_child_lists_both_parents_and_opens_chat(self):
        self.callback.data = "dchat_child:200"
        await support.direct_child_parents(self.callback, self.db, self.state, object(), None)
        payloads = [b["payload"] for b in self.buttons()]
        self.assertIn("dchat_to:20", payloads)
        self.assertIn("dchat_to:21", payloads)
        self.callback.data = "dchat_to:21"
        await support.direct_chat_start(self.callback, self.db, self.state, object(), None)
        thread = self.db.get_direct_thread((await self.state.get_data())["direct_thread_id"])
        self.assertEqual(self.db.direct_thread_peer(thread, 10), 21)
        self.assertEqual(await self.state.get_state(), DirectChatStates.chatting)

    async def test_unregistered_parent_explained(self):
        self.callback.data = "dchat_child:201"
        await support.direct_child_parents(self.callback, self.db, self.state, object(), None)
        self.assertIn("Родитель ещё не вошёл", self.callback.message.answer.call_args.args[0])
        self.assertFalse(any(b["payload"].startswith("dchat_to:") for b in self.buttons()))

    async def test_unrelated_child_rejected(self):
        self.callback.data = "dchat_child:999"
        await support.direct_child_parents(self.callback, self.db, self.state, object(), None)
        self.callback.message.answer.assert_not_awaited()
        self.assertIn("не связан", self.callback.answer.call_args.args[0])

    async def test_inactive_parent_excluded(self):
        self.db.deactivate_user(20)
        parents = await support._users_by_crm_ids(self.db, {"200"}, "parent")
        self.assertEqual([p["max_user_id"] for p in parents], [21])

    async def test_failed_delivery_is_not_reported_as_success(self):
        thread_id = self.db.create_direct_thread(10, "teacher", 20, "parent")
        await self.state.update_data(direct_thread_id=thread_id)
        message = SimpleNamespace(
            from_user=self.callback.from_user, text="Здравствуйте", answer=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(support, "safe_call", AsyncMock(return_value=None)):
            await support.direct_chat_message(message, self.db, self.state)
        self.assertIn("доставить его не удалось", message.answer.call_args.args[0])

    def test_pagination_preserves_all_children_within_max_limits(self):
        children = [(i, f"Ребёнок {i}") for i in range(65)]
        seen = []
        for page in range(4):
            rows = direct_children_keyboard(children, page)[0]["payload"]["buttons"]
            self.assertLessEqual(len(rows), 30)
            seen.extend(b["payload"] for row in rows for b in row if b["payload"].startswith("dchat_child:"))
        self.assertEqual(seen, [f"dchat_child:{i}" for i in range(65)])


if __name__ == "__main__":
    unittest.main()
