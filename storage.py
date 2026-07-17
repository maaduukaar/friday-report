"""SQLite storage for the multi-user Friday Report bot.

The bot is intentionally kept single-process for now.  SQLite is a good fit for
the expected scale (up to a few hundred users) and lets us make the quota check
and job reservation one transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ACTIVE_ROLES = ("admin", "user")
QUOTA_STATUSES = ("queued", "running", "submitted", "unknown")
FINAL_RUN_STATUSES = {"submitted", "failed", "unknown", "dry_run"}


class _ClosingConnection(sqlite3.Connection):
    """Make ``with connection`` close the handle as well as committing/rolling back.

    sqlite3's stock context manager deliberately leaves connections open.  That
    is easy to miss and keeps WAL handles open on Windows, where it also blocks
    database cleanup during tests and deployment maintenance.
    """

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class StorageError(RuntimeError):
    """Base class for storage-related domain errors."""


class UserLimitReached(StorageError):
    """Raised when approving a user would exceed the configured capacity."""


class WeeklyLimitReached(StorageError):
    """Raised when a user has no remaining report submissions this week."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Weekly report limit ({limit}) has been reached")
        self.limit = limit


class ReportInProgress(StorageError):
    """Raised when the same user already has a queued or running report."""


class InvalidSetting(StorageError):
    """Raised when a bot setting is outside its supported range."""


class ProfileIncomplete(StorageError):
    """Raised when an administrator tries to activate an incomplete profile."""


@dataclass(frozen=True)
class User:
    telegram_user_id: int
    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: str
    employee_name: str | None
    department: str | None
    created_at: str
    updated_at: str

    @property
    def is_active(self) -> bool:
        return self.role in ACTIVE_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def profile_complete(self) -> bool:
        return bool(self.employee_name and self.employee_name.strip() and self.department and self.department.strip())


@dataclass(frozen=True)
class RunReservation:
    run_id: str
    week_start: str
    remaining: int


class Storage:
    """Small, transaction-safe repository around a single SQLite file."""

    def __init__(self, database_path: str | Path, max_active_users: int = 300) -> None:
        self.database_path = Path(database_path)
        self.max_active_users = max_active_users

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    role TEXT NOT NULL CHECK (role IN ('pending', 'user', 'admin', 'blocked')),
                    employee_name TEXT,
                    department TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_workloads (
                    user_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 100),
                    PRIMARY KEY (user_id, category)
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_runs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    week_start TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'submitted', 'failed', 'unknown', 'dry_run')),
                    quota_consumed INTEGER NOT NULL CHECK (quota_consumed IN (0, 1)),
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_report_runs_quota
                    ON report_runs(user_id, week_start, quota_consumed, status);

                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    user_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    reminder_date TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, reminder_date)
                );

                CREATE TABLE IF NOT EXISTS pending_application_submissions (
                    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    submitted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER REFERENCES users(telegram_user_id) ON DELETE SET NULL,
                    subject_user_id INTEGER REFERENCES users(telegram_user_id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO bot_settings(key, value) VALUES ('weekly_send_limit', '1')"
            )
            # Profiles completed before this table was introduced had already
            # triggered the legacy notification flow. Mark them as submitted
            # during startup migration so editing their profile cannot send a
            # third application to administrators.
            connection.execute(
                """
                INSERT OR IGNORE INTO pending_application_submissions(user_id, submitted_at)
                SELECT telegram_user_id, updated_at
                FROM users
                WHERE role = 'pending'
                  AND TRIM(COALESCE(employee_name, '')) != ''
                  AND TRIM(COALESCE(department, '')) != ''
                """
            )

    def has_admins(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        return row is not None

    def recover_interrupted_runs(self) -> None:
        """Recover jobs left behind by a bot restart before workers are started.

        A queued job has not started Playwright and can safely be released. A
        running job may have submitted the form before shutdown, so it becomes
        ``unknown`` and preserves its quota slot.
        """

        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE report_runs
                SET status = 'failed', finished_at = ?, error_message = 'Interrupted before worker start'
                WHERE status = 'queued'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE report_runs
                SET status = 'unknown', finished_at = ?, error_message = 'Bot restarted while report was running'
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.commit()

    def get_user(self, telegram_user_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
        return self._row_to_user(row)

    def register_pending_user(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> tuple[User, bool]:
        """Register an unknown private-chat user as pending, once."""

        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            if existing is None:
                registered_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role IN ('pending', 'admin', 'user')"
                ).fetchone()[0]
                if registered_count >= self.max_active_users:
                    connection.rollback()
                    raise UserLimitReached(f"The {self.max_active_users}-user limit has been reached")
                connection.execute(
                    """
                    INSERT INTO users(
                        telegram_user_id, chat_id, username, first_name, last_name,
                        role, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (telegram_user_id, chat_id, username, first_name, last_name, now, now),
                )
                self._audit(connection, None, telegram_user_id, "user.registration_requested")
                row = connection.execute(
                    "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
                ).fetchone()
                connection.commit()
                return self._row_to_user(row), True

            connection.execute(
                """
                UPDATE users
                SET chat_id = ?, username = ?, first_name = ?, last_name = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (chat_id, username, first_name, last_name, now, telegram_user_id),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            connection.commit()
        return self._row_to_user(row), False

    def ensure_bootstrap_admin(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        default_employee_name: str | None,
        default_department: str | None,
        default_workload: dict[str, int],
    ) -> User:
        """Activate a configured bootstrap administrator without trusting first contact."""

        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            if row is None:
                registered_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role IN ('pending', 'admin', 'user')"
                ).fetchone()[0]
                if registered_count >= self.max_active_users:
                    connection.rollback()
                    raise UserLimitReached(f"The {self.max_active_users}-user limit has been reached")
                connection.execute(
                    """
                    INSERT INTO users(
                        telegram_user_id, chat_id, username, first_name, last_name,
                        role, employee_name, department, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'admin', ?, ?, ?, ?)
                    """,
                    (
                        telegram_user_id,
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        self._clean_profile_value(default_employee_name),
                        self._clean_profile_value(default_department),
                        now,
                        now,
                    ),
                )
                self._seed_workload(connection, telegram_user_id, default_workload)
                self._audit(connection, telegram_user_id, telegram_user_id, "user.bootstrap_admin_created")
            else:
                connection.execute(
                    """
                    UPDATE users
                    SET chat_id = ?, username = ?, first_name = ?, last_name = ?, role = 'admin',
                        employee_name = COALESCE(NULLIF(employee_name, ''), ?),
                        department = COALESCE(NULLIF(department, ''), ?), updated_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        self._clean_profile_value(default_employee_name),
                        self._clean_profile_value(default_department),
                        now,
                        telegram_user_id,
                    ),
                )
                self._seed_workload(connection, telegram_user_id, default_workload)
                self._audit(connection, telegram_user_id, telegram_user_id, "user.bootstrap_admin_activated")
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            connection.commit()
        return self._row_to_user(row)

    def approve_user(
        self,
        actor_user_id: int,
        target_user_id: int,
        default_workload: dict[str, int],
    ) -> User:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_admin_actor(connection, actor_user_id)
            target = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (target_user_id,)
            ).fetchone()
            if target is None:
                connection.rollback()
                raise StorageError("User does not exist")
            if target["role"] == "blocked":
                connection.rollback()
                raise StorageError("Blocked user cannot be approved")
            if not self._clean_profile_value(target["employee_name"]) or not self._clean_profile_value(target["department"]):
                connection.rollback()
                raise ProfileIncomplete("The user must complete their employee name and department first")
            if target["role"] not in ACTIVE_ROLES:
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role IN ('admin', 'user')"
                ).fetchone()[0]
                if active_count >= self.max_active_users:
                    connection.rollback()
                    raise UserLimitReached(f"The {self.max_active_users}-user limit has been reached")
                connection.execute(
                    "UPDATE users SET role = 'user', updated_at = ? WHERE telegram_user_id = ?",
                    (now, target_user_id),
                )
                self._seed_workload(connection, target_user_id, default_workload)
                self._audit(connection, actor_user_id, target_user_id, "user.approved")
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (target_user_id,)
            ).fetchone()
            connection.commit()
        return self._row_to_user(row)

    def block_user(self, actor_user_id: int, target_user_id: int) -> None:
        if actor_user_id == target_user_id:
            raise StorageError("Administrators cannot block themselves")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_admin_actor(connection, actor_user_id)
            target = connection.execute(
                "SELECT role FROM users WHERE telegram_user_id = ?", (target_user_id,)
            ).fetchone()
            if target is None or target["role"] == "admin":
                connection.rollback()
                raise StorageError("Administrators cannot block this user")
            connection.execute(
                "UPDATE users SET role = 'blocked', updated_at = ? WHERE telegram_user_id = ?",
                (now, target_user_id),
            )
            self._audit(connection, actor_user_id, target_user_id, "user.blocked")
            connection.commit()

    def list_pending_users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE role = 'pending' ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def list_active_users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE role IN ('admin', 'user') ORDER BY telegram_user_id"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def claim_pending_application_submission(self, user_id: int) -> bool:
        """Mark a completed pending profile as submitted exactly once.

        The caller sends the administrator notification only when this method
        returns ``True``.  Keeping the marker in SQLite prevents duplicate
        notifications when a pending user reopens and saves their profile.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT role, employee_name, department FROM users WHERE telegram_user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None or user["role"] != "pending":
                connection.rollback()
                raise StorageError("Pending user was not found")
            if not self._clean_profile_value(user["employee_name"]) or not self._clean_profile_value(user["department"]):
                connection.rollback()
                raise ProfileIncomplete("The user must complete their profile before submitting an application")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO pending_application_submissions(user_id, submitted_at)
                VALUES (?, ?)
                """,
                (user_id, self._now()),
            )
            submitted = cursor.rowcount == 1
            if submitted:
                self._audit(connection, user_id, user_id, "user.application_submitted")
            connection.commit()
        return submitted

    def set_profile(self, actor_user_id: int, employee_name: str, department: str) -> User:
        employee_name = self._validate_profile_value(employee_name, "Employee name")
        department = self._validate_profile_value(department, "Department")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE users SET employee_name = ?, department = ?, updated_at = ?
                WHERE telegram_user_id = ? AND role != 'blocked'
                """,
                (employee_name, department, now, actor_user_id),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (actor_user_id,)
            ).fetchone()
            if row is None or row["role"] == "blocked":
                connection.rollback()
                raise StorageError("User was not found")
            self._audit(connection, actor_user_id, actor_user_id, "profile.updated")
            connection.commit()
        return self._row_to_user(row)

    def set_profile_for_user(
        self,
        actor_user_id: int,
        target_user_id: int,
        employee_name: str,
        department: str,
    ) -> User:
        """Let an administrator correct or bind a user's form identity."""

        employee_name = self._validate_profile_value(employee_name, "Employee name")
        department = self._validate_profile_value(department, "Department")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_admin_actor(connection, actor_user_id)
            connection.execute(
                """
                UPDATE users SET employee_name = ?, department = ?, updated_at = ?
                WHERE telegram_user_id = ? AND role != 'blocked'
                """,
                (employee_name, department, now, target_user_id),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (target_user_id,)
            ).fetchone()
            if row is None or row["role"] == "blocked":
                connection.rollback()
                raise StorageError("User was not found")
            self._audit(connection, actor_user_id, target_user_id, "profile.admin_updated")
            connection.commit()
        return self._row_to_user(row)

    def get_workload(self, user_id: int) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT category, value FROM user_workloads WHERE user_id = ? ORDER BY category", (user_id,)
            ).fetchall()
        return {row["category"]: row["value"] for row in rows}

    def set_workload_value(self, user_id: int, category: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError("Workload value must be an integer from 0 to 100")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM users WHERE telegram_user_id = ? AND role IN ('admin', 'user')",
                (user_id,),
            ).fetchone()
            if not exists:
                connection.rollback()
                raise StorageError("Active user was not found")
            connection.execute(
                """
                INSERT INTO user_workloads(user_id, category, value) VALUES (?, ?, ?)
                ON CONFLICT(user_id, category) DO UPDATE SET value = excluded.value
                """,
                (user_id, category, value),
            )
            self._audit(connection, user_id, user_id, "workload.updated", {"category": category})
            connection.commit()

    def replace_workload(self, user_id: int, workload: dict[str, int]) -> None:
        self._validate_workload(workload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM users WHERE telegram_user_id = ? AND role IN ('admin', 'user')",
                (user_id,),
            ).fetchone()
            if not exists:
                connection.rollback()
                raise StorageError("Active user was not found")
            connection.execute("DELETE FROM user_workloads WHERE user_id = ?", (user_id,))
            self._seed_workload(connection, user_id, workload)
            self._audit(connection, user_id, user_id, "workload.reset")
            connection.commit()

    def get_weekly_send_limit(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = 'weekly_send_limit'"
            ).fetchone()
        if row is None:
            return 1
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 1

    def set_weekly_send_limit(self, actor_user_id: int, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 52:
            raise InvalidSetting("Weekly send limit must be an integer from 1 to 52")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_admin_actor(connection, actor_user_id)
            connection.execute(
                """
                INSERT INTO bot_settings(key, value) VALUES ('weekly_send_limit', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(value),),
            )
            self._audit(connection, actor_user_id, None, "settings.weekly_send_limit_updated", {"value": value})
            connection.commit()
        return value

    def reserve_report_run(
        self,
        user_id: int,
        snapshot: dict[str, Any],
        now: datetime,
        consume_quota: bool,
    ) -> RunReservation:
        """Atomically reserve a per-user ISO-week submission slot."""

        if now.tzinfo is None:
            raise ValueError("A timezone-aware datetime is required")
        week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
        created_at = now.astimezone(timezone.utc).isoformat()
        run_id = uuid.uuid4().hex
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT role FROM users WHERE telegram_user_id = ?", (user_id,)
            ).fetchone()
            if user is None or user["role"] not in ACTIVE_ROLES:
                connection.rollback()
                raise StorageError("Active user was not found")

            limit_row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = 'weekly_send_limit'"
            ).fetchone()
            limit = int(limit_row["value"]) if limit_row else 1
            active_run = connection.execute(
                """
                SELECT 1 FROM report_runs
                WHERE user_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if active_run is not None:
                connection.rollback()
                raise ReportInProgress("A report is already queued or running for this user")
            used = 0
            if consume_quota:
                placeholders = ",".join("?" for _ in QUOTA_STATUSES)
                used = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM report_runs
                    WHERE user_id = ? AND week_start = ? AND quota_consumed = 1
                      AND status IN ({placeholders})
                    """,
                    (user_id, week_start, *QUOTA_STATUSES),
                ).fetchone()[0]
                if used >= limit:
                    connection.rollback()
                    raise WeeklyLimitReached(limit)

            connection.execute(
                """
                INSERT INTO report_runs(
                    id, user_id, week_start, status, quota_consumed, snapshot_json, created_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (run_id, user_id, week_start, int(consume_quota), snapshot_json, created_at),
            )
            self._audit(connection, user_id, user_id, "report.queued", {"run_id": run_id})
            connection.commit()
        return RunReservation(run_id=run_id, week_start=week_start, remaining=max(0, limit - used - int(consume_quota)))

    def mark_run_running(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE report_runs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (self._now(), run_id),
            )

    def finish_report_run(self, run_id: str, status: str, error_message: str | None = None) -> None:
        if status not in FINAL_RUN_STATUSES:
            raise ValueError(f"Unsupported final run status: {status}")
        safe_error = (error_message or "").strip()[:500] or None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT user_id FROM report_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StorageError("Report run was not found")
            connection.execute(
                """
                UPDATE report_runs
                SET status = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (status, self._now(), safe_error, run_id),
            )
            self._audit(connection, row["user_id"], row["user_id"], f"report.{status}", {"run_id": run_id})
            connection.commit()

    def claim_reminder_delivery(self, user_id: int, reminder_date: str) -> bool:
        """Persist idempotency before sending a weekly reminder."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO reminder_deliveries(user_id, reminder_date, claimed_at) VALUES (?, ?, ?)",
                    (user_id, reminder_date, self._now()),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
        return True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _row_to_user(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            telegram_user_id=row["telegram_user_id"],
            chat_id=row["chat_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            role=row["role"],
            employee_name=row["employee_name"],
            department=row["department"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean_profile_value(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def _validate_profile_value(self, value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be text")
        value = value.strip()
        if not 1 <= len(value) <= 120:
            raise ValueError(f"{field_name} must contain from 1 to 120 characters")
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"{field_name} contains unsupported control characters")
        return value

    @staticmethod
    def _validate_workload(workload: dict[str, int]) -> None:
        for category, value in workload.items():
            if not isinstance(category, str) or not category:
                raise ValueError("Workload category must be a non-empty string")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError("Workload values must be integers from 0 to 100")

    def _seed_workload(
        self, connection: sqlite3.Connection, user_id: int, workload: dict[str, int]
    ) -> None:
        self._validate_workload(workload)
        connection.executemany(
            "INSERT OR IGNORE INTO user_workloads(user_id, category, value) VALUES (?, ?, ?)",
            [(user_id, category, value) for category, value in workload.items()],
        )

    @staticmethod
    def _require_admin_actor(connection: sqlite3.Connection, actor_user_id: int) -> None:
        actor = connection.execute(
            "SELECT role FROM users WHERE telegram_user_id = ?", (actor_user_id,)
        ).fetchone()
        if actor is None or actor["role"] != "admin":
            raise StorageError("Administrator role is required")

    def _audit(
        self,
        connection: sqlite3.Connection,
        actor_user_id: int | None,
        subject_user_id: int | None,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log(actor_user_id, subject_user_id, event_type, created_at, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                subject_user_id,
                event_type,
                self._now(),
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )
