"""Transactional rolling quotas and idempotency for a single application process.

Only opaque identifiers, hashes and timestamps are durable here. In particular,
temporary media URLs/titles never enter the installation's quota database.
"""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager

from app.config import settings
from app.errors import AppError
from app.identity import Principal

class Admission:
    def __init__(self):
        self.inspections = 0

    @property
    def db(self):
        return settings.state_root / "admission.db"

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self):
        settings.state_root.mkdir(parents=True, exist_ok=True)
        self.inspections = 0
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT,
                    fingerprint TEXT NOT NULL, created REAL NOT NULL,
                    state TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
                    charged INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner, key)
                );
                CREATE INDEX IF NOT EXISTS reservation_owner_time ON reservations(owner, created);
                PRAGMA user_version=1;
            """)
            # Only work that actually started remains charged after a crash.
            conn.execute("UPDATE reservations SET state='released' WHERE state IN ('inspecting','queued')")
            conn.execute("UPDATE reservations SET state='consumed' WHERE state='running'")
        self.prune()

    def prune(self):
        with self.connect() as conn:
            conn.execute("DELETE FROM reservations WHERE state NOT IN ('inspecting','queued','running') AND created < ?",
                         (time.time() - settings.anon_download_window_seconds,))

    @contextmanager
    def inspection(self):
        if self.inspections >= settings.max_metadata_requests:
            raise AppError("capacity.exceeded", "Media inspection is busy. Try again shortly.", 503, headers={"Retry-After": "5"})
        self.inspections += 1
        try:
            yield
        finally:
            self.inspections -= 1

    def quota(self, actor: Principal, *, conn=None, now=None) -> dict:
        if conn is None:
            with self.connect() as connection:
                return self.quota(actor, conn=connection, now=now)
        now = time.time() if now is None else now
        limit = 0 if actor.admin else settings.anon_download_limit
        if not limit:
            return {"limit": 0, "remaining": None, "window_seconds": settings.anon_download_window_seconds, "next_available_at": None}
        count, oldest = conn.execute("""SELECT COUNT(*), MIN(created) FROM reservations WHERE owner=? AND charged=1
            AND (state IN ('inspecting','queued','running') OR (state='consumed' AND created > ?))
            """, (actor.owner, now - settings.anon_download_window_seconds)).fetchone()
        return {"limit": limit, "remaining": max(0, limit - count),
                "window_seconds": settings.anon_download_window_seconds,
                "next_available_at": max(now + 1, oldest + settings.anon_download_window_seconds) if count >= limit else None}

    def reserve(self, actor: Principal, key: str | None, payload: dict, *, charge=True) -> tuple[str, bool]:
        if key is not None and not 1 <= len(key) <= 128:
            raise AppError("url.invalid", "Idempotency-Key must contain 1–128 characters.", 422)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if key_hash:
                row = conn.execute("SELECT * FROM reservations WHERE owner=? AND key=?", (actor.owner, key_hash)).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise AppError("idempotency.conflict", "This idempotency key was used for a different request.", 409)
                    if row["admitted"]:
                        return row["id"], True
                    raise AppError("admission.pending", "This request is already being inspected. Retry with the same key.", 409, headers={"Retry-After": "1"})
            quota = self.quota(actor, conn=conn, now=now)
            if charge and quota["remaining"] == 0:
                retry = max(1, math.ceil(quota["next_available_at"] - now))
                raise AppError("quota.exceeded", "Your anonymous download quota is exhausted.", 429,
                               context=quota, headers={"Retry-After": str(retry)})
            count, own = conn.execute("""SELECT COUNT(*), COALESCE(SUM(owner=?),0) FROM reservations
                WHERE state IN ('inspecting','queued','running')""", (actor.owner,)).fetchone()
            if count >= settings.max_pending_jobs + settings.max_concurrent_jobs or own >= settings.max_jobs_per_owner:
                raise AppError("capacity.exceeded", "The processing queue is full. Try again later.", 503, headers={"Retry-After": "5"})
            job_id = uuid.uuid4().hex
            conn.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?, 'inspecting', 0, ?)",
                         (job_id, actor.owner, key_hash, fingerprint, now, int(charge and not actor.admin)))
            return job_id, False

    def accepted(self, job_id: str):
        with self.connect() as conn:
            conn.execute("UPDATE reservations SET state='queued', admitted=1 WHERE id=?", (job_id,))

    def started(self, job_id: str):
        with self.connect() as conn:
            conn.execute("UPDATE reservations SET state='running' WHERE id=?", (job_id,))

    def finish(self, job_id: str, started: bool):
        with self.connect() as conn:
            conn.execute("UPDATE reservations SET state=? WHERE id=?", ("consumed" if started else "released", job_id))

    def release(self, job_id: str):
        """Pre-admission failure/selector releases its reservation exactly once."""
        with self.connect() as conn:
            conn.execute("DELETE FROM reservations WHERE id=? AND admitted=0", (job_id,))


admission = Admission()
