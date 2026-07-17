"""Unit tests for the SQLite-backed multi-user storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from storage import (
    InvalidSetting,
    ProfileIncomplete,
    ReportInProgress,
    Storage,
    StorageError,
    UserLimitReached,
    WeeklyLimitReached,
)


DEFAULT_WORKLOAD = {"SITES_DEVELOPMENT": 100}


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "bot.sqlite3"
        self.storage = Storage(self.database_path)
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_admin(self, user_id: int = 1):
        return self.storage.ensure_bootstrap_admin(
            telegram_user_id=user_id,
            chat_id=10_000 + user_id,
            username=f"admin_{user_id}",
            first_name="Admin",
            last_name="User",
            default_employee_name="Administrator",
            default_department="IT",
            default_workload=DEFAULT_WORKLOAD,
        )

    def register_and_approve_user(self, user_id: int, actor_user_id: int = 1):
        user, created = self.storage.register_pending_user(
            telegram_user_id=user_id,
            chat_id=10_000 + user_id,
            username=f"user_{user_id}",
            first_name="Regular",
            last_name="User",
        )
        self.assertTrue(created)
        self.assertEqual(user.role, "pending")
        self.storage.set_profile(user_id, f"Employee {user_id}", "IT")
        return self.storage.approve_user(actor_user_id, user_id, DEFAULT_WORKLOAD)

    def test_weekly_send_limit_defaults_to_one_and_persists_updates(self) -> None:
        self.create_admin()
        self.assertEqual(self.storage.get_weekly_send_limit(), 1)

        self.assertEqual(self.storage.set_weekly_send_limit(actor_user_id=1, value=3), 3)
        self.assertEqual(self.storage.get_weekly_send_limit(), 3)

        reloaded_storage = Storage(self.database_path)
        self.assertEqual(reloaded_storage.get_weekly_send_limit(), 3)

    def test_weekly_send_limit_rejects_invalid_values(self) -> None:
        self.create_admin()
        for value in (0, -1, 53, "2", 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSetting):
                    self.storage.set_weekly_send_limit(actor_user_id=1, value=value)  # type: ignore[arg-type]

        self.assertEqual(self.storage.get_weekly_send_limit(), 1)

    def test_pending_application_is_claimed_once_after_profile_completion(self) -> None:
        pending_user, created = self.storage.register_pending_user(
            telegram_user_id=2,
            chat_id=10_002,
            username="pending_2",
            first_name="Pending",
            last_name="User",
        )
        self.assertTrue(created)
        with self.assertRaises(ProfileIncomplete):
            self.storage.claim_pending_application_submission(pending_user.telegram_user_id)

        self.storage.set_profile(pending_user.telegram_user_id, "Employee 2", "IT")
        self.assertTrue(self.storage.claim_pending_application_submission(pending_user.telegram_user_id))
        self.assertFalse(self.storage.claim_pending_application_submission(pending_user.telegram_user_id))

    def test_restart_migrates_existing_completed_pending_profile(self) -> None:
        pending_user, _ = self.storage.register_pending_user(
            telegram_user_id=2,
            chat_id=10_002,
            username="pending_2",
            first_name="Pending",
            last_name="User",
        )
        self.storage.set_profile(pending_user.telegram_user_id, "Employee 2", "IT")

        restarted_storage = Storage(self.database_path)
        restarted_storage.initialize()
        self.assertFalse(restarted_storage.claim_pending_application_submission(pending_user.telegram_user_id))

    def test_quota_is_per_user_and_resets_at_iso_week_boundary(self) -> None:
        self.create_admin()
        first_user = self.register_and_approve_user(2)
        second_user = self.register_and_approve_user(3)
        thursday = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
        following_monday = datetime(2027, 1, 4, 12, tzinfo=timezone.utc)

        first_reservation = self.storage.reserve_report_run(
            first_user.telegram_user_id, {"source": "test"}, thursday, consume_quota=True
        )
        self.assertEqual(first_reservation.week_start, "2026-12-28")
        self.assertEqual(first_reservation.remaining, 0)

        with self.assertRaises(ReportInProgress):
            self.storage.reserve_report_run(
                first_user.telegram_user_id, {"source": "test"}, thursday, consume_quota=True
            )

        self.storage.finish_report_run(first_reservation.run_id, "submitted")
        with self.assertRaises(WeeklyLimitReached):
            self.storage.reserve_report_run(
                first_user.telegram_user_id, {"source": "test"}, thursday, consume_quota=True
            )

        second_reservation = self.storage.reserve_report_run(
            second_user.telegram_user_id, {"source": "test"}, thursday, consume_quota=True
        )
        self.assertEqual(second_reservation.remaining, 0)

        next_week_reservation = self.storage.reserve_report_run(
            first_user.telegram_user_id, {"source": "test"}, following_monday, consume_quota=True
        )
        self.assertEqual(next_week_reservation.week_start, "2027-01-04")

    def test_concurrent_reservations_admit_only_one_run_at_default_limit(self) -> None:
        self.create_admin()
        user = self.register_and_approve_user(2)
        now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
        workers = 8
        start = threading.Barrier(workers)

        def reserve(index: int) -> str:
            start.wait()
            try:
                self.storage.reserve_report_run(
                    user.telegram_user_id,
                    {"attempt": index},
                    now,
                    consume_quota=True,
                )
            except (WeeklyLimitReached, ReportInProgress):
                return "rejected"
            return "reserved"

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(reserve, range(workers)))

        self.assertEqual(results.count("reserved"), 1)
        self.assertEqual(results.count("rejected"), workers - 1)

    def test_restart_releases_queued_runs_but_preserves_running_run_quota(self) -> None:
        self.create_admin()
        user = self.register_and_approve_user(2)
        now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)

        queued = self.storage.reserve_report_run(user.telegram_user_id, {}, now, consume_quota=True)
        self.storage.recover_interrupted_runs()
        restarted = self.storage.reserve_report_run(user.telegram_user_id, {}, now, consume_quota=True)

        self.storage.mark_run_running(restarted.run_id)
        self.storage.recover_interrupted_runs()
        with self.assertRaises(WeeklyLimitReached):
            self.storage.reserve_report_run(user.telegram_user_id, {}, now, consume_quota=True)

        self.assertNotEqual(queued.run_id, restarted.run_id)

    def test_roles_and_active_user_cap_are_enforced(self) -> None:
        self.storage = Storage(self.database_path, max_active_users=2)
        self.storage.initialize()
        admin = self.create_admin()
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.is_active)

        pending_user, created = self.storage.register_pending_user(
            telegram_user_id=2,
            chat_id=10_002,
            username="pending_2",
            first_name="Pending",
            last_name="User",
        )
        self.assertTrue(created)
        self.assertEqual(pending_user.role, "pending")
        self.assertFalse(pending_user.is_active)

        with self.assertRaises(UserLimitReached):
            self.storage.register_pending_user(
                telegram_user_id=3,
                chat_id=10_003,
                username="pending_3",
                first_name="Another",
                last_name="User",
            )

        self.assertIsNone(self.storage.get_user(3))
        self.storage.set_profile(pending_user.telegram_user_id, "Employee 2", "IT")
        approved_user = self.storage.approve_user(
            admin.telegram_user_id, pending_user.telegram_user_id, DEFAULT_WORKLOAD
        )
        self.assertEqual(approved_user.role, "user")
        self.assertTrue(approved_user.is_active)
        self.assertFalse(approved_user.is_admin)
        with self.assertRaises(StorageError):
            self.storage.set_weekly_send_limit(approved_user.telegram_user_id, 2)

        self.assertEqual(self.storage.get_user(pending_user.telegram_user_id).role, "user")


if __name__ == "__main__":
    unittest.main()
