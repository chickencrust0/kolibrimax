import unittest
from unittest.mock import AsyncMock, patch

from impulse_client import ImpulseCRMClient
from max_api.keyboards import lesson_attendance_keyboard


class AttendanceTests(unittest.IsolatedAsyncioTestCase):
    def test_names_stay_in_first_column_and_freeze_has_no_actions(self):
        students = [(1, "Иванов Иван"), (2, "Петров Пётр")]
        for marked, absent in [(set(), set()), ({1}, set()), (set(), {1})]:
            rows = lesson_attendance_keyboard("12:2026-09-18", students, marked, absent)[0]["payload"]["buttons"]
            self.assertEqual(rows[0][0]["text"], students[0][1])
            self.assertEqual(len(rows[0]), 3)
        rows = lesson_attendance_keyboard("12:2026-09-18", students, frozen={"1"})[0]["payload"]["buttons"]
        self.assertEqual(rows[0][1]["text"], "❄️ Заморожено")
        self.assertTrue(all(b["payload"].startswith("journal:") for b in rows[0]))

    async def test_account_only_sent_after_explicit_selection(self):
        client = object.__new__(ImpulseCRMClient)
        client._internal_request = AsyncMock(return_value={})
        account = {"id": 8, "entity": "groupAccount"}
        target = {"id": 4, "entity": "group", "minutesBegin": 900}
        with patch("settings.IMPULSE_CHECK_VISITS_ENABLED", True), patch.object(client, "_assert_ok", return_value={}):
            await client.check_visit(1, account, target, 123)
            self.assertNotIn("account", client._internal_request.call_args.kwargs["json_body"])
            await client.check_visit(1, account, target, 123, explicit_account=True)
            payload = client._internal_request.call_args.kwargs["json_body"]
            self.assertEqual(payload["account"], account)
            self.assertEqual(payload["target"], target)
            self.assertNotIn("force", payload)
