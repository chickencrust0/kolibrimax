"""Regression checks for opt-in notification categories."""

import tempfile
import unittest
from pathlib import Path

from database import Database
from max_api.keyboards import (
    manager_menu_keyboard,
    notification_settings_keyboard,
    parent_menu_keyboard,
    teacher_menu_keyboard,
)


class NotificationPreferencesTest(unittest.TestCase):
    def test_preferences_persist_and_default_to_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bot.db")
            db = Database(path)
            self.assertTrue(all(db.get_notification_preferences(123).values()))
            self.assertFalse(db.toggle_notification(123, "lessons")["lessons"])
            self.assertFalse(Database(path).notification_enabled(123, "lessons"))
            self.assertTrue(db.notification_enabled(456, "lessons"))
            self.assertTrue(db.toggle_notification(123, "lessons")["lessons"])
            with self.assertRaises(ValueError):
                db.toggle_notification(123, "invalid")

    def test_all_menus_contain_settings(self):
        for menu in (parent_menu_keyboard, teacher_menu_keyboard, manager_menu_keyboard):
            buttons = menu()[0]["payload"]["buttons"]
            self.assertIn("menu:notifications", [row[0]["payload"] for row in buttons])

    def test_settings_show_relevant_categories(self):
        off = {key: False for key in Database.NOTIFICATION_CATEGORIES}
        parent = notification_settings_keyboard(off, "parent")[0]["payload"]["buttons"]
        manager = notification_settings_keyboard(off, "manager")[0]["payload"]["buttons"]
        self.assertIn("notifications:lessons", [row[0]["payload"] for row in parent])
        self.assertNotIn("notifications:summaries", [row[0]["payload"] for row in parent])
        self.assertIn("notifications:summaries", [row[0]["payload"] for row in manager])
        self.assertTrue(all(row[0]["text"].startswith("🔕") for row in parent[:-1]))


if __name__ == "__main__":
    unittest.main()
