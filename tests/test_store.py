from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from relaybox.store import Conflict, NotFound, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "queue.db"
        self.store = Store(self.path, clock=lambda: self.now)

    def add(self, **kwargs):
        return self.store.enqueue("mail", {"to": "demo@example.test"}, **kwargs)[0]

    def test_persistence_and_idempotency(self):
        job = self.add(key="send-1")
        restarted = Store(self.path, clock=lambda: self.now)
        same, created = restarted.enqueue("mail", {"to": "demo@example.test"}, key="send-1")
        self.assertFalse(created)
        self.assertEqual(job["id"], same["id"])
        with self.assertRaises(Conflict):
            restarted.enqueue("mail", {"to": "other@example.test"}, key="send-1")
        other, created = restarted.enqueue("other", {"to": "different"}, key="send-1")
        self.assertTrue(created)
        self.assertNotEqual(job["id"], other["id"])

    def test_canonical_json_and_options_are_part_of_idempotency(self):
        self.store.enqueue("mail", {"b": 2, "a": 1}, key="x")
        _, created = self.store.enqueue("mail", {"a": 1, "b": 2}, key="x")
        self.assertFalse(created)
        with self.assertRaises(Conflict):
            self.store.enqueue("mail", {"a": 1, "b": 2}, key="x", delay=1)

    def test_concurrent_claims_are_exclusive(self):
        ids = {self.add()["id"] for _ in range(20)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(lambda _: self.store.claim("mail"), range(30)))
        actual = [job["id"] for job in claimed if job]
        self.assertEqual(len(actual), 20)
        self.assertEqual(set(actual), ids)

    def test_concurrent_idempotent_enqueue_creates_one_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: self.store.enqueue("mail", {}, key="same"), range(20)))
        self.assertEqual(sum(created for _, created in result), 1)
        self.assertEqual(len({job["id"] for job, _ in result}), 1)

    def test_delay_queue_isolation_and_ack(self):
        self.add(delay=10)
        self.assertIsNone(self.store.claim("mail"))
        self.assertIsNone(self.store.claim("other"))
        self.now += 10
        job = self.store.claim("mail")
        self.store.ack(job["id"], job["lease_token"])
        self.assertIsNone(self.store.claim("mail"))
        self.assertEqual(self.store.snapshot("mail")["counts"]["done"], 1)
        self.assertNotIn("lease_token", self.store.snapshot("mail")["jobs"][0])

    def test_stale_token_cannot_ack_reclaimed_job(self):
        self.add()
        old = self.store.claim("mail", lease_seconds=5)
        self.now += 5
        with self.assertRaises(Conflict):
            self.store.ack(old["id"], old["lease_token"])
        new = self.store.claim("mail")
        self.assertEqual(new["attempts"], 2)
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(Conflict):
            self.store.ack(new["id"], old["lease_token"])
        self.store.ack(new["id"], new["lease_token"])

    def test_lease_deadline_is_checked_after_acquiring_write_lock(self):
        self.add()
        job = self.store.claim("mail", lease_seconds=30)
        original_transaction = self.store.transaction

        @contextmanager
        def delayed_transaction():
            with original_transaction() as db:
                # Model time spent waiting for SQLite's write lock, without a sleep.
                self.now += 31
                yield db

        self.store.transaction = delayed_transaction
        with self.assertRaises(Conflict):
            self.store.ack(job["id"], job["lease_token"])
        # Claim must also start its new lease after lock acquisition.
        reclaimed = self.store.claim("mail", lease_seconds=30)
        self.assertEqual(reclaimed["lease_until"], self.now + 30)

    def test_retry_backoff_dead_letter_and_manual_replay(self):
        self.add(max_attempts=2)
        first = self.store.claim("mail")
        self.store.fail(first["id"], first["lease_token"], "temporary")
        self.assertIsNone(self.store.claim("mail"))
        self.now += 2
        second = self.store.claim("mail")
        self.store.fail(second["id"], second["lease_token"], "permanent")
        self.assertEqual(self.store.snapshot("mail")["counts"]["dead"], 1)
        self.store.retry(second["id"])
        self.assertEqual(self.store.claim("mail")["attempts"], 1)

    def test_crash_on_last_attempt_goes_to_dead_letter(self):
        self.add(max_attempts=1)
        self.store.claim("mail", lease_seconds=1)
        self.now += 1
        self.assertIsNone(self.store.claim("mail"))
        self.assertEqual(self.store.snapshot("mail")["counts"]["dead"], 1)

    def test_missing_job_and_invalid_retry(self):
        with self.assertRaises(NotFound):
            self.store.retry("missing")
        job = self.add()
        with self.assertRaises(Conflict):
            self.store.retry(job["id"])

    def test_validation(self):
        for value in (True, 0, -1, 1.5, "3", 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.add(max_attempts=value)
        for queue in ("", "bad/name", "é", "a" * 65, None):
            with self.subTest(queue=queue), self.assertRaises(ValueError):
                self.store.claim(queue)
        for payload in (float("nan"), {"huge": "a" * 65537}, object()):
            with self.assertRaises(ValueError):
                self.store.enqueue("mail", payload)


if __name__ == "__main__":
    unittest.main()
