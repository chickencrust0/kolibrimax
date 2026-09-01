"""
database.py — локальное хранилище (SQLite): пользователи, файлы ДЗ,
заявки на перенос, лог напоминаний.

Портировано из alfacrm-bot без структурных изменений — только
telegram_id переименован в max_user_id, т.к. идентификатор теперь
приходит от MAX. Комментарии о причинах решений (WAL, чанки IN(...),
дедупликация напоминаний, миграция author_role) сохранены из
оригинала — это по-прежнему актуальные грабли, а не байки.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# SQLite ограничивает число параметров в запросе (обычно 999).
_SQL_VAR_CHUNK = 500


class Database:
    def __init__(self, db_path: str = "bot.db"):
        self.db_path = db_path
        self._init_db()

    # ==================== СОЕДИНЕНИЯ ====================

    @contextmanager
    def _conn(self):
        """
        `with sqlite3.connect(...)` фиксирует транзакцию, но НЕ закрывает
        соединение — незакрытое соединение течёт дескрипторами на каждом
        запросе, поэтому закрываем явно в finally.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    max_user_id INTEGER PRIMARY KEY,
                    crm_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher', 'parent', 'manager')),
                    phone TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS homework_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    file_type TEXT DEFAULT 'document',
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transfer_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_max_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    comment TEXT,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    FOREIGN KEY (teacher_max_id) REFERENCES users(max_user_id)
                );

                CREATE TABLE IF NOT EXISTS reminder_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    target_max_id INTEGER,
                    status TEXT DEFAULT 'sent'
                );

                CREATE INDEX IF NOT EXISTS idx_homework_lesson ON homework_files(lesson_id);
                CREATE INDEX IF NOT EXISTS idx_transfer_status ON transfer_requests(status);
                CREATE INDEX IF NOT EXISTS idx_reminder_lookup
                    ON reminder_log(lesson_id, reminder_type, target_max_id, sent_at);

                -- Неявки. В impulseCRM нет статуса «не пришёл» до момента
                -- списания занятия, поэтому отметка преподавателя живёт
                -- здесь, а в CRM уходит только решение менеджера (burn_one).
                -- lesson_id — TEXT, потому что у повторяющихся занятий id
                -- синтетический вида "116:2026-09-01".
                CREATE TABLE IF NOT EXISTS absences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id TEXT NOT NULL,
                    client_id INTEGER NOT NULL,
                    client_name TEXT,
                    lesson_date TEXT NOT NULL,
                    teacher_max_id INTEGER,
                    status TEXT DEFAULT 'pending'
                        CHECK(status IN ('pending', 'burned', 'excused')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    UNIQUE(lesson_id, client_id)
                );

                CREATE INDEX IF NOT EXISTS idx_absences_date
                    ON absences(lesson_date, status);

                -- Обращения в поддержку: диалог «клиент <-> менеджер».
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_max_id INTEGER NOT NULL,
                    user_name TEXT,
                    user_role TEXT,
                    user_phone TEXT,
                    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    closed_by INTEGER
                );

                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_max_id INTEGER,
                    sender_side TEXT CHECK(sender_side IN ('user', 'manager')),
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
                );

                CREATE INDEX IF NOT EXISTS idx_tickets_status
                    ON support_tickets(status, user_max_id);
                CREATE INDEX IF NOT EXISTS idx_ticket_messages
                    ON support_messages(ticket_id, created_at);
            """)

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(transfer_requests)")}
            if "author_role" not in columns:
                conn.execute(
                    "ALTER TABLE transfer_requests ADD COLUMN author_role TEXT DEFAULT 'teacher'"
                )
                logger.info("🛠 transfer_requests: добавлена колонка author_role")

    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    def deactivate_user(self, max_user_id: int):
        with self._conn() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE max_user_id = ?", (max_user_id,))

    def link_user(self, max_user_id: int, crm_id: int, role: str, phone: str, full_name: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO users (max_user_id, crm_id, role, phone, full_name, is_active)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(max_user_id) DO UPDATE SET
                       crm_id=excluded.crm_id,
                       role=excluded.role,
                       phone=excluded.phone,
                       full_name=excluded.full_name,
                       is_active=1""",
                (max_user_id, crm_id, role, phone, full_name),
            )

    def get_user(self, max_user_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE max_user_id = ? AND is_active = 1", (max_user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_crm_id(self, crm_id: int, role: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE crm_id = ? AND role = ? AND is_active = 1",
                (crm_id, role),
            ).fetchone()
            return dict(row) if row else None

    def get_all_users_by_role(self, role: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = ? AND is_active = 1", (role,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ==================== ДОМАШНИЕ ЗАДАНИЯ ====================

    def add_homework_file(self, lesson_id: int, file_id: str, file_name: str, file_type: str = "document"):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO homework_files (lesson_id, file_id, file_name, file_type) VALUES (?,?,?,?)",
                (lesson_id, file_id, file_name, file_type),
            )

    def get_homework_files(self, lesson_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM homework_files WHERE lesson_id = ? ORDER BY uploaded_at DESC",
                (lesson_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_homework_file_counts(
        self, lesson_ids: Optional[Sequence[int]] = None
    ) -> Dict[int, int]:
        """{lesson_id: количество файлов ДЗ} одним запросом."""
        counts: Dict[int, int] = {}
        with self._conn() as conn:
            if lesson_ids is None:
                rows = conn.execute(
                    "SELECT lesson_id, COUNT(*) AS cnt FROM homework_files GROUP BY lesson_id"
                ).fetchall()
                return {row["lesson_id"]: row["cnt"] for row in rows}

            ids = [int(i) for i in lesson_ids if i is not None]
            for start in range(0, len(ids), _SQL_VAR_CHUNK):
                chunk = ids[start:start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT lesson_id, COUNT(*) AS cnt FROM homework_files "
                    f"WHERE lesson_id IN ({placeholders}) GROUP BY lesson_id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    counts[row["lesson_id"]] = row["cnt"]
        return counts

    # ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

    def create_transfer_request(
        self,
        max_user_id: int,
        lesson_id: Optional[int],
        comment: str,
        author_role: str = "teacher",
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO transfer_requests
                       (teacher_max_id, lesson_id, comment, author_role)
                   VALUES (?,?,?,?)""",
                (max_user_id, int(lesson_id or 0), comment, author_role),
            )
            return cursor.lastrowid

    def get_transfer_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def resolve_transfer_request(self, request_id: int, status: str, resolved_by: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE transfer_requests
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='pending'""",
                (status, resolved_by, request_id),
            )
            # False — заявку уже обработал другой менеджер.
            return cursor.rowcount > 0

    def get_pending_transfer_requests(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT tr.*,
                          u.full_name AS author_name,
                          u.phone     AS author_phone
                   FROM transfer_requests tr
                   LEFT JOIN users u ON tr.teacher_max_id = u.max_user_id
                   WHERE tr.status = 'pending'
                   ORDER BY tr.created_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    # ==================== НЕЯВКИ ====================

    def mark_absent(
        self,
        lesson_id: Any,
        client_id: int,
        client_name: str,
        lesson_date: str,
        teacher_max_id: Optional[int] = None,
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO absences
                       (lesson_id, client_id, client_name, lesson_date, teacher_max_id)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(lesson_id, client_id) DO UPDATE SET
                       client_name=excluded.client_name,
                       lesson_date=excluded.lesson_date,
                       teacher_max_id=excluded.teacher_max_id,
                       status='pending',
                       resolved_at=NULL,
                       resolved_by=NULL""",
                (str(lesson_id), int(client_id), client_name, lesson_date, teacher_max_id),
            )
            return cursor.lastrowid

    def unmark_absent(self, lesson_id: Any, client_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM absences WHERE lesson_id=? AND client_id=?",
                (str(lesson_id), int(client_id)),
            )

    def get_absent_client_ids(self, lesson_id: Any) -> List[int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT client_id FROM absences WHERE lesson_id=?", (str(lesson_id),)
            ).fetchall()
            return [row["client_id"] for row in rows]

    def get_absence(self, absence_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM absences WHERE id=?", (absence_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_absences_for_date(
        self, lesson_date: str, status: Optional[str] = "pending"
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM absences WHERE lesson_date=?"
        params: List[Any] = [lesson_date]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at"
        with self._conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def resolve_absence(self, absence_id: int, status: str, resolved_by: int) -> bool:
        """False — неявку уже обработал другой менеджер."""
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE absences
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='pending'""",
                (status, resolved_by, absence_id),
            )
            return cursor.rowcount > 0

    # ==================== ПОДДЕРЖКА ====================

    def get_open_ticket_for_user(self, user_max_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_tickets WHERE user_max_id=? AND status='open' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_max_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_ticket(
        self, user_max_id: int, user_name: str, user_role: str, user_phone: str
    ) -> int:
        existing = self.get_open_ticket_for_user(user_max_id)
        if existing:
            return existing["id"]
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO support_tickets (user_max_id, user_name, user_role, user_phone)
                   VALUES (?,?,?,?)""",
                (user_max_id, user_name, user_role, user_phone),
            )
            return cursor.lastrowid

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
            ).fetchone()
            return dict(row) if row else None

    def add_ticket_message(
        self, ticket_id: int, sender_max_id: int, sender_side: str, text: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO support_messages (ticket_id, sender_max_id, sender_side, text)
                   VALUES (?,?,?,?)""",
                (ticket_id, sender_max_id, sender_side, text),
            )

    def get_ticket_messages(self, ticket_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM support_messages WHERE ticket_id=? "
                "ORDER BY created_at LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_open_tickets(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM support_tickets WHERE status='open' ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def close_ticket(self, ticket_id: int, closed_by: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE support_tickets
                   SET status='closed', closed_at=CURRENT_TIMESTAMP, closed_by=?
                   WHERE id=? AND status='open'""",
                (closed_by, ticket_id),
            )
            return cursor.rowcount > 0

    # ==================== ЛОГ НАПОМИНАНИЙ ====================

    def mark_reminder_sent(
        self,
        lesson_id: int,
        reminder_type: str,
        target_max_id: Optional[int] = None,
    ):
        """
        Тип напоминания должен совпадать в записи и в проверке
        was_reminder_sent — иначе дедупликация никогда не сработает и
        напоминания задублируются на каждом тике планировщика.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reminder_log (lesson_id, reminder_type, target_max_id) VALUES (?,?,?)",
                (lesson_id, reminder_type, target_max_id),
            )

    def was_reminder_sent(
        self,
        lesson_id: int,
        reminder_type: str,
        target_max_id: Optional[int] = None,
        hours: int = 24,
    ) -> bool:
        query = """SELECT COUNT(*) AS count FROM reminder_log
                   WHERE lesson_id=? AND reminder_type=?
                     AND sent_at > datetime('now', ? || ' hours')"""
        params: List[Any] = [lesson_id, reminder_type, f"-{hours}"]
        if target_max_id is not None:
            query += " AND target_max_id=?"
            params.append(target_max_id)

        with self._conn() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return bool(row["count"])

    def cleanup_reminder_log(self, days: int = 30) -> int:
        """Лог напоминаний растёт вечно — чистим старое."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM reminder_log WHERE sent_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            return cursor.rowcount
