"""SQLite-backed leases. Each operation owns its connection and transaction."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid


class Conflict(Exception):
    """A request conflicts with an existing job or an expired lease."""


class NotFound(Exception):
    """No job has the supplied identifier."""


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def queue_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("queue must contain 1 to 64 characters")
    if any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in value):
        raise ValueError("queue may contain ASCII letters, digits, _ and -")
    return value


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(Path(path))
        self.clock = clock
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    queue TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    idempotency_key TEXT,
                    state TEXT NOT NULL CHECK(state IN ('ready','leased','done','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    lease_token TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    UNIQUE(queue, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_ready
                    ON jobs(queue, state, available_at, created_at, id);
                CREATE INDEX IF NOT EXISTS jobs_expired
                    ON jobs(state, lease_until);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def job(row, include_token=False):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result.pop("fingerprint")
        if not include_token:
            result.pop("lease_token")
        return result

    def enqueue(self, queue, payload, key=None, delay=0, max_attempts=3):
        queue_name(queue)
        integer(delay, "delay", 0, 86400)
        integer(max_attempts, "max_attempts", 1, 100)
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        try:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be finite JSON") from exc
        if len(body.encode("utf-8")) > 65536:
            raise ValueError("payload exceeds 64 KiB")
        fingerprint = hashlib.sha256(
            f"{body}\n{delay}\n{max_attempts}".encode("utf-8")
        ).hexdigest()
        with self.transaction() as db:
            now = self.clock()
            if key is not None:
                existing = db.execute(
                    "SELECT * FROM jobs WHERE queue=? AND idempotency_key=?", (queue, key)
                ).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise Conflict("idempotency key already used for a different request")
                    return self.job(existing), False
            job_id = str(uuid.uuid4())
            db.execute("""
                INSERT INTO jobs(id,queue,payload,fingerprint,idempotency_key,state,
                    max_attempts,available_at,created_at,updated_at)
                VALUES(?,?,?,?,?,'ready',?,?,?,?)
            """, (job_id, queue, body, fingerprint, key, max_attempts, now + delay, now, now))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self.job(row), True

    @staticmethod
    def recover(db, now):
        # Recovery is transactional with claiming, so two claimants cannot reclaim
        # the same expired lease. The next claim always gets a fresh token.
        db.execute("""
            UPDATE jobs SET state=CASE WHEN attempts>=max_attempts THEN 'dead' ELSE 'ready' END,
                lease_token=NULL, lease_until=NULL, available_at=?, updated_at=?,
                last_error='lease expired'
            WHERE state='leased' AND lease_until<=?
        """, (now, now, now))

    def claim(self, queue, lease_seconds=30):
        queue_name(queue)
        integer(lease_seconds, "lease_seconds", 1, 3600)
        with self.transaction() as db:
            now = self.clock()
            self.recover(db, now)
            row = db.execute("""
                SELECT * FROM jobs WHERE queue=? AND state='ready' AND available_at<=?
                ORDER BY available_at,created_at,id LIMIT 1
            """, (queue, now)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("""
                UPDATE jobs SET state='leased',attempts=attempts+1,lease_until=?,
                    lease_token=?,updated_at=? WHERE id=?
            """, (now + lease_seconds, token, now, row["id"]))
            return self.job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone(), True)

    @staticmethod
    def owned(db, job_id, token, now):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("job not found")
        if not isinstance(token, str) or not token:
            raise ValueError("lease_token is required")
        if row["state"] != "leased" or row["lease_until"] <= now:
            raise Conflict("lease is no longer active")
        if not secrets.compare_digest(row["lease_token"], token):
            raise Conflict("lease token does not match")
        return row

    def ack(self, job_id, token):
        with self.transaction() as db:
            now = self.clock()
            self.owned(db, job_id, token, now)
            db.execute("""
                UPDATE jobs SET state='done',lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE id=?
            """, (now, job_id))

    def fail(self, job_id, token, error="worker failed"):
        if not isinstance(error, str) or len(error) > 1000:
            raise ValueError("error must be a string of at most 1000 characters")
        with self.transaction() as db:
            now = self.clock()
            row = self.owned(db, job_id, token, now)
            dead = row["attempts"] >= row["max_attempts"]
            delay = min(300, 2 ** min(row["attempts"], 9))
            db.execute("""
                UPDATE jobs SET state=?,available_at=?,lease_token=NULL,lease_until=NULL,
                    updated_at=?,last_error=? WHERE id=?
            """, ("dead" if dead else "ready", now + delay, now, error, job_id))

    def retry(self, job_id):
        with self.transaction() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("job not found")
            if row["state"] != "dead":
                raise Conflict("only dead jobs can be retried")
            db.execute("""
                UPDATE jobs SET state='ready',attempts=0,available_at=?,updated_at=?,
                    last_error=NULL WHERE id=?
            """, (now, now, job_id))

    def snapshot(self, queue):
        queue_name(queue)
        # Reads do not mutate state. Expired leases are recovered on the next claim.
        with self.connection() as db:
            db.execute("BEGIN")
            counts = {state: 0 for state in ("ready", "leased", "done", "dead")}
            counts.update({r["state"]: r["n"] for r in db.execute(
                "SELECT state,COUNT(*) n FROM jobs WHERE queue=? GROUP BY state", (queue,)
            )})
            rows = db.execute("""
                SELECT * FROM jobs WHERE queue=? ORDER BY created_at DESC,id DESC LIMIT 100
            """, (queue,)).fetchall()
            return {"counts": counts, "jobs": [self.job(row) for row in rows]}
