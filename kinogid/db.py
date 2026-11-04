"""SQLite: короткие транзакции, одна копия процесса бота на одну базу."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .content import ROLES


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


AUDIENCES = {
    "all": "все с включёнными сообщениями",
    "club": "участники киноклуба",
    "training": "интересуются обучением",
    **ROLES,
}


class Database:
    def __init__(self, path: Path, admin_ids: frozenset[int]):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.admin_ids = admin_ids
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                last_name TEXT,
                language_code TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                source TEXT,
                starts INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                clicks INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT,
                notifications INTEGER NOT NULL DEFAULT 1,
                blocked INTEGER NOT NULL DEFAULT 0,
                role TEXT,
                training_interest_at TEXT,
                onboarding_step INTEGER NOT NULL DEFAULT 0,
                posts_received INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                audience TEXT NOT NULL,
                created_at TEXT NOT NULL,
                send_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                PRIMARY KEY (job_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(state, send_at);
            CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(job_id, state);
        """)
        # Если участника назначили администратором, удаляем его из всей статистики.
        with self.connection:
            for admin_id in admin_ids:
                self.connection.execute("DELETE FROM users WHERE user_id=?", (admin_id,))
            self.connection.execute("UPDATE jobs SET state='pending' WHERE state='running'")

    def close(self) -> None:
        self.connection.close()

    def touch(self, user: Any, *, click: bool = False, start: bool = False, source: str = "") -> None:
        if user.id in self.admin_ids or user.is_bot:
            return
        now = stamp()
        with self.connection:
            self.connection.execute("""
                INSERT INTO users (
                    user_id, username, first_name, last_name, language_code,
                    first_seen, last_seen, source, starts, messages, clicks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    last_name=excluded.last_name, language_code=excluded.language_code,
                    last_seen=excluded.last_seen, blocked=0,
                    source=COALESCE(users.source, excluded.source),
                    starts=users.starts+excluded.starts,
                    messages=users.messages+excluded.messages,
                    clicks=users.clicks+excluded.clicks
            """, (
                user.id, user.username, user.first_name, user.last_name, user.language_code,
                now, now, source[:64] or None, int(start), int(not click), int(click),
            ))

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def join(self, user_id: int) -> None:
        self._update_user(user_id, "joined_at=COALESCE(joined_at, ?), notifications=1, blocked=0", (stamp(),))

    def set_notifications(self, user_id: int, enabled: bool) -> None:
        self._update_user(user_id, "notifications=?", (int(enabled),))

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self._update_user(user_id, "blocked=?", (int(blocked),))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError("Unknown role")
        self._update_user(user_id, "role=?", (role,))

    def set_interest(self, user_id: int, interested: bool) -> None:
        self._update_user(user_id, "training_interest_at=?", (stamp() if interested else None,))

    def set_onboarding_step(self, user_id: int, step: int) -> None:
        self._update_user(user_id, "onboarding_step=?", (step,))

    def _update_user(self, user_id: int, fields: str, values: tuple) -> None:
        if user_id in self.admin_ids:
            return
        # fields задаётся только кодом выше, пользовательский ввод — только параметры.
        with self.connection:
            self.connection.execute(f"UPDATE users SET {fields} WHERE user_id=?", (*values, user_id))

    def stats(self) -> dict:
        now = utcnow()
        row = self.connection.execute("""
            SELECT COUNT(*) total,
                COALESCE(SUM(first_seen >= ?), 0) new_today,
                COALESCE(SUM(first_seen >= ?), 0) new_week,
                COALESCE(SUM(last_seen >= ?), 0) active_week,
                COALESCE(SUM(joined_at IS NOT NULL), 0) joined,
                COALESCE(SUM(joined_at IS NOT NULL AND notifications=1 AND blocked=0), 0) club_active,
                COALESCE(SUM(notifications=0), 0) stopped,
                COALESCE(SUM(blocked=1), 0) blocked,
                COALESCE(SUM(training_interest_at IS NOT NULL), 0) training,
                COALESCE(SUM(posts_received), 0) posts_received,
                COALESCE(SUM(starts), 0) starts
            FROM users
        """, (stamp(now - timedelta(hours=24)), stamp(now - timedelta(days=7)),
              stamp(now - timedelta(days=7)))).fetchone()
        result = dict(row)
        result["roles"] = dict(self.connection.execute(
            "SELECT COALESCE(role, 'none'), COUNT(*) FROM users GROUP BY role"
        ).fetchall())
        return result

    def users(self, *, page: int = 1, page_size: int = 20) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM users ORDER BY first_seen DESC, user_id LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()

    def export(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM users ORDER BY user_id")]

    @staticmethod
    def _audience_sql(audience: str) -> tuple[str, tuple]:
        if audience not in AUDIENCES:
            raise ValueError("Unknown audience")
        base = "notifications=1 AND blocked=0"
        if audience == "club":
            return base + " AND joined_at IS NOT NULL", ()
        if audience == "training":
            return base + " AND training_interest_at IS NOT NULL", ()
        if audience in ROLES:
            return base + " AND role=?", (audience,)
        return base, ()

    def audience_count(self, audience: str) -> int:
        where, params = self._audience_sql(audience)
        return self.connection.execute(f"SELECT COUNT(*) FROM users WHERE {where}", params).fetchone()[0]

    def create_job(self, admin_id: int, source_chat_id: int, source_message_id: int,
                   audience: str, send_at: datetime) -> int:
        if admin_id not in self.admin_ids:
            raise ValueError("Admin required")
        self._audience_sql(audience)
        with self.connection:
            cursor = self.connection.execute("""
                INSERT INTO jobs (admin_id, source_chat_id, source_message_id, audience, created_at, send_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (admin_id, source_chat_id, source_message_id, audience, stamp(), stamp(send_at)))
            return cursor.lastrowid

    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def confirm_job(self, job_id: int) -> bool:
        with self.connection:
            job = self.job(job_id)
            if not job or job["state"] != "draft":
                return False
            where, params = self._audience_sql(job["audience"])
            self.connection.execute(f"""
                INSERT INTO deliveries (job_id, user_id)
                SELECT ?, user_id FROM users WHERE {where}
            """, (job_id, *params))
            self.connection.execute("UPDATE jobs SET state='pending' WHERE id=?", (job_id,))
        return True

    def cancel_job(self, job_id: int) -> bool:
        with self.connection:
            cursor = self.connection.execute("""
                UPDATE jobs SET state='cancelled'
                WHERE id=? AND state IN ('draft', 'pending', 'running')
            """, (job_id,))
            return bool(cursor.rowcount)

    def next_job(self) -> sqlite3.Row | None:
        with self.connection:
            job = self.connection.execute("""
                SELECT * FROM jobs WHERE state='pending' AND send_at<=?
                ORDER BY send_at, id LIMIT 1
            """, (stamp(),)).fetchone()
            if job:
                self.connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            return job

    def next_recipient(self, job_id: int) -> int | None:
        row = self.connection.execute("""
            SELECT user_id FROM deliveries WHERE job_id=? AND state='pending'
            ORDER BY user_id LIMIT 1
        """, (job_id,)).fetchone()
        return row[0] if row else None

    def eligible(self, user_id: int, audience: str) -> bool:
        if user_id in self.admin_ids:
            return False
        where, params = self._audience_sql(audience)
        return self.connection.execute(
            f"SELECT 1 FROM users WHERE user_id=? AND {where}", (user_id, *params)
        ).fetchone() is not None

    def attempt(self, job_id: int, user_id: int) -> int:
        with self.connection:
            self.connection.execute("""
                UPDATE deliveries SET attempts=attempts+1 WHERE job_id=? AND user_id=?
            """, (job_id, user_id))
            return self.connection.execute(
                "SELECT attempts FROM deliveries WHERE job_id=? AND user_id=?", (job_id, user_id)
            ).fetchone()[0]

    def delivery_result(self, job_id: int, user_id: int, state: str, error: str | None = None) -> None:
        with self.connection:
            cursor = self.connection.execute("""
                UPDATE deliveries SET state=?, error=?
                WHERE job_id=? AND user_id=? AND state='pending'
            """, (state, error, job_id, user_id))
            if state == "sent" and cursor.rowcount:
                self.connection.execute(
                    "UPDATE users SET posts_received=posts_received+1 WHERE user_id=?", (user_id,)
                )

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute("""
                UPDATE jobs SET state=?, error=? WHERE id=? AND state='running'
            """, ("failed" if error else "done", error, job_id))

    def recent_jobs(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10").fetchall()

    def job_counts(self, job_id: int) -> dict:
        counts = {"pending": 0, "sent": 0, "skipped": 0, "failed": 0}
        counts.update(dict(self.connection.execute(
            "SELECT state, COUNT(*) FROM deliveries WHERE job_id=? GROUP BY state", (job_id,)
        )))
        return counts
