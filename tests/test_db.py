import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from kinogid.db import Database, utcnow

ADMIN = 759470806


def person(user_id=100, name="Даша"):
    return SimpleNamespace(id=user_id, username="viewer", first_name=name,
                           last_name=None, language_code="ru", is_bot=False)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path, frozenset({ADMIN}))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def job(self, audience="club", when=None):
        return self.db.create_job(ADMIN, ADMIN, 10, audience, when or utcnow())

    def test_admin_never_enters_users_or_stats(self):
        self.db.touch(person(ADMIN), start=True)
        self.db.join(ADMIN)
        self.db.set_role(ADMIN, "editor")
        self.db.set_interest(ADMIN, True)
        self.assertIsNone(self.db.user(ADMIN))
        self.assertEqual(self.db.stats()["total"], 0)
        self.assertEqual(self.db.audience_count("all"), 0)

    def test_role_and_training_are_independent_and_repeated_start_does_not_duplicate(self):
        self.db.touch(person(), start=True, source="instagram")
        self.db.join(100)
        self.db.set_role(100, "editor")
        self.db.set_interest(100, True)
        self.db.touch(person(), start=True, source="other")
        self.assertEqual(self.db.stats()["total"], 1)
        row = self.db.user(100)
        self.assertEqual(row["starts"], 2)
        self.assertEqual(row["source"], "instagram")
        self.assertEqual(row["role"], "editor")
        self.assertIsNotNone(row["training_interest_at"])
        self.db.set_interest(100, False)
        self.assertEqual(self.db.user(100)["role"], "editor")

    def test_confirmation_is_idempotent_and_audience_is_snapshot(self):
        self.db.touch(person())
        self.db.join(100)
        job_id = self.job()
        self.assertTrue(self.db.confirm_job(job_id))
        self.assertFalse(self.db.confirm_job(job_id))
        self.db.touch(person(101))
        self.db.join(101)
        self.assertEqual(self.db.job_counts(job_id)["pending"], 1)
        self.assertEqual(self.db.next_recipient(job_id), 100)

    def test_stop_and_role_change_are_checked_again_before_delivery(self):
        self.db.touch(person())
        self.db.set_role(100, "editor")
        self.assertTrue(self.db.eligible(100, "editor"))
        self.db.set_role(100, "cinema")
        self.assertFalse(self.db.eligible(100, "editor"))
        self.db.set_notifications(100, False)
        self.assertFalse(self.db.eligible(100, "all"))
        self.db.touch(person(), start=True)
        self.assertFalse(self.db.eligible(100, "all"))
        self.db.join(100)
        self.assertTrue(self.db.eligible(100, "club"))

    def test_queue_and_profiles_survive_restart(self):
        self.db.touch(person())
        self.db.join(100)
        self.db.set_onboarding_step(100, 1)
        job_id = self.job()
        self.db.confirm_job(job_id)
        self.db.next_job()
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.assertEqual(self.db.job(job_id)["state"], "pending")
        self.assertEqual(self.db.user(100)["onboarding_step"], 1)
        self.assertEqual(self.db.next_recipient(job_id), 100)

    def test_new_admin_is_purged_from_saved_users_and_delivery_queue(self):
        self.db.touch(person())
        self.db.join(100)
        job_id = self.job()
        self.db.confirm_job(job_id)
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN, 100}))
        self.assertIsNone(self.db.user(100))
        self.assertEqual(self.db.job_counts(job_id)["pending"], 0)

    def test_future_and_cancelled_jobs_are_not_started(self):
        job_id = self.job(when=utcnow() + timedelta(days=1))
        self.db.confirm_job(job_id)
        self.assertIsNone(self.db.next_job())
        self.assertTrue(self.db.cancel_job(job_id))
        self.assertFalse(self.db.cancel_job(job_id))
        self.assertIsNone(self.db.next_job())

    def test_delivery_count_cannot_be_incremented_twice(self):
        self.db.touch(person())
        job_id = self.job("all")
        self.db.confirm_job(job_id)
        self.db.delivery_result(job_id, 100, "sent")
        self.db.delivery_result(job_id, 100, "sent")
        self.assertEqual(self.db.user(100)["posts_received"], 1)
