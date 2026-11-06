"""Bounded in-process workers, paginated disk history and restart recovery."""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.admission import admission
from app.blocking import run_sync
from app.config import settings
from app.errors import AppError, error_body
from app.storage import storage, timestamp


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: str
    url: str = ""
    status: str = "queued"
    stage: str = "Queued"
    percent: float = 0
    speed: float | None = None
    eta: float | None = None
    filename: str | None = None
    output_path: str | None = None
    error: dict | None = None
    warning: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    cancelled: bool = False
    last_update: float = 0
    owner: str = "admin:legacy"
    storage_mode: str = "legacy"
    storage_root: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    expires_at: str | None = None
    availability: str = "pending"

    def public(self) -> dict:
        return {key: getattr(self, key) for key in ("id", "kind", "url", "status", "stage", "percent", "speed", "eta", "filename", "error", "warning", "created_at", "started_at", "completed_at", "expires_at", "storage_mode")} | {"files": list(self.files), "availability": storage.availability(self)}


class History:
    """The existing JSON rows remain compatible; indexed columns enforce ownership."""
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
            columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            if exists and "owner" not in columns:
                backup = self.path.with_name(self.path.name + f".v0-{time.time_ns()}.bak")
                with sqlite3.connect(backup) as target:
                    conn.backup(target)
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            for name, declaration in (("owner", "TEXT NOT NULL DEFAULT 'admin:legacy'"), ("created", "TEXT NOT NULL DEFAULT ''"), ("status", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            if "owner" not in columns:
                for job_id, data in conn.execute("SELECT id, data FROM jobs"):
                    try:
                        value = json.loads(data)
                        value.setdefault("created_at", "1970-01-01T00:00:00+00:00")
                        if value.get("storage_mode", "legacy") == "legacy":
                            value.setdefault("storage_root", str(settings.output_dir.resolve()))
                            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(value), job_id))
                        conn.execute("UPDATE jobs SET owner=?, created=?, status=? WHERE id=?",
                                     (value.get("owner", "admin:legacy"), value.get("created_at", ""), value.get("status", ""), job_id))
                    except (ValueError, TypeError):
                        logging.warning("Invalid legacy history row %s retained for manual recovery", job_id)
            conn.execute("CREATE INDEX IF NOT EXISTS history_owner_created ON jobs(owner, created DESC, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS history_status ON jobs(status)")
            conn.execute("PRAGMA user_version=1")

    @staticmethod
    def decode(data):
        value = json.loads(data)
        return Job(**{k: v for k, v in value.items() if k in Job.__dataclass_fields__})

    def save(self, job):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (id,data,owner,created,status) VALUES (?,?,?,?,?)",
                         (job.id, json.dumps(job.__dict__), job.owner, job.created_at, job.status))

    def get(self, job_id, owners):
        with self.connect() as conn:
            row = conn.execute(f"SELECT data FROM jobs WHERE id=? AND owner IN ({','.join('?' for _ in owners)})", (job_id, *owners)).fetchone()
        return self.decode(row[0]) if row else None

    def page(self, owners, limit, cursor=None):
        parameters = [*owners]
        condition = ""
        if cursor:
            condition = " AND (created < ? OR (created = ? AND id < ?))"
            parameters += [cursor[0], cursor[0], cursor[1]]
        with self.connect() as conn:
            rows = conn.execute(f"SELECT data FROM jobs WHERE owner IN ({','.join('?' for _ in owners)})" + condition + " ORDER BY created DESC, id DESC LIMIT ?", (*parameters, limit)).fetchall()
        return [self.decode(row[0]) for row in rows]

    def scan(self, *, interrupted=False):
        """Use a keyset cursor so cleaning/deleting rows cannot skip entries."""
        after = ""
        while True:
            with self.connect() as conn:
                rows = conn.execute("SELECT id,data FROM jobs WHERE id > ?" + (" AND status IN ('receiving','queued','running')" if interrupted else "") + " ORDER BY id LIMIT 100", (after,)).fetchall()
            if not rows:
                return
            for job_id, data in rows:
                after = job_id
                try:
                    yield self.decode(data)
                except (ValueError, TypeError):
                    logging.warning("Invalid history row %s", job_id)

    def remove(self, job_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


class Queue:
    def __init__(self):
        self.jobs: dict[str, Job] = {}  # Active work only; history stays on disk.
        self.pending = asyncio.Queue()
        self.workers = []
        self.operations = {}
        self.subscribers: dict[asyncio.Queue, str] = {}
        self.loop = None
        self.cleaner = None
        self.stopping = False
        self.histories = []
        self.healthy = True

    def history(self, job):
        if job.storage_mode == "temporary":
            path = Path(job.storage_root) / "history.db"
            # The snapshot is validated before using an old temporary root.
            storage.folder(job)
            return History(path)
        return self.histories[0]

    def persist(self, job):
        self.history(job).save(job)

    def get(self, job_id, owners):
        job = self.jobs.get(job_id)
        if job:
            return job if job.owner in owners else None
        for history in self.histories:
            job = history.get(job_id, owners)
            if job:
                return job
        return None

    def page(self, owners, limit=30, cursor=None):
        # SQL applies ownership and a bound before anything is loaded into RAM.
        rows = [job for history in self.histories for job in history.page(owners, limit, cursor)]
        rows.sort(key=lambda j: (j.created_at, j.id), reverse=True)
        return [self.jobs.get(j.id, j) for j in rows[:limit]]

    def publish(self, job):
        data = job.public()
        for subscriber, owner in list(self.subscribers.items()):
            if owner != job.owner:
                continue
            if subscriber.full():
                subscriber.get_nowait()
            subscriber.put_nowait(data)

    def update(self, job, percent, speed, eta, stage):
        now = time.monotonic()
        if now - job.last_update < 0.1 and stage == job.stage:
            return
        job.last_update = now
        job.percent, job.speed, job.eta, job.stage = min(99, percent), speed, eta, stage
        if self.loop:
            self.loop.call_soon_threadsafe(self.publish, job)

    async def start(self):
        storage.start()
        admission.start()
        self.jobs.clear()
        self.operations.clear()
        self.subscribers.clear()
        self.pending = asyncio.Queue(maxsize=settings.max_pending_jobs + settings.max_concurrent_jobs)
        self.stopping = False
        self.healthy = True
        self.histories = [History(settings.state_root / "history.db"), History(settings.temp_storage_dir / "history.db")]
        # This registry contains roots only, never media URLs, titles or paths
        # inside jobs. It preserves old temporary roots when configuration changes.
        registry = settings.state_root / "media-roots.json"
        roots = json.loads(registry.read_text()) if registry.exists() else []
        if not isinstance(roots, list) or len(roots) > 128:
            raise ValueError("Invalid media root registry; review it before startup")
        roots = [{"root": value, "mode": "temporary"} if isinstance(value, str) else value for value in roots]
        for root, mode in ((settings.output_dir, "persistent"), (settings.temp_storage_dir, "temporary")):
            entry = {"root": str(root.resolve()), "mode": mode}
            if entry not in roots:
                roots.append(entry)
        current = str(settings.temp_storage_dir.resolve())
        for entry in roots:
            root, mode = entry["root"], entry["mode"]
            if mode not in {"temporary", "persistent"}:
                raise ValueError("Invalid media root mode")
            if Path(root).is_dir():
                probe = Job("root-check", "probe", storage_mode=mode, storage_root=root)
                storage.folder(probe)  # Reject unsafe configuration, do not sweep it.
                storage.roots.add(Path(root))
            path = Path(root) / "history.db"
            if root != current and mode == "temporary" and path.is_file():
                self.histories.append(History(path))
        self.media_roots = roots
        temp = registry.with_suffix(".json.tmp")
        temp.write_text(json.dumps(roots))
        temp.replace(registry)
        for history in self.histories:
            history.start()
            for job in history.scan(interrupted=True):
                job.status, job.completed_at = "error", now_iso()
                job.error = error_body("job.interrupted", "The server restarted before this job finished.")["error"]
                history.save(job)
                try:
                    storage.delete(job)
                except (OSError, ValueError):
                    logging.exception("Unable to clean interrupted job %s", job.id)
        self.loop = asyncio.get_running_loop()
        await self.cleanup()
        self.workers = [asyncio.create_task(self.worker()) for _ in range(settings.max_concurrent_jobs)]
        self.cleaner = asyncio.create_task(self.cleanup_loop())

    async def stop(self):
        self.stopping = True
        if self.cleaner:
            self.cleaner.cancel()
            await asyncio.gather(self.cleaner, return_exceptions=True)
        for job in self.jobs.values():
            job.cancelled = True
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        for job in list(self.jobs.values()):
            job.status, job.completed_at = "error", now_iso()
            job.error = error_body("job.interrupted", "The server stopped during this job.")["error"]
            self.persist(job)
            admission.finish(job.id, bool(job.started_at))
            try:
                await run_sync(storage.delete, job)
            except (OSError, ValueError):
                logging.exception("Unable to clean stopped job %s", job.id)
        self.jobs.clear()
        self.loop = None

    def prepare(self, kind, url, *, owner, job_id=None):
        job = Job(job_id or uuid.uuid4().hex, kind, url, owner=owner, status="receiving")
        storage.assign(job)
        storage.create(job)
        self.jobs[job.id] = job
        try:
            self.persist(job)
        except Exception:
            self.jobs.pop(job.id, None)
            storage.delete(job)
            raise
        return job

    def submit(self, kind, url, operation, *, owner, job_id=None, prepared=None):
        if self.pending.full():
            raise AppError("capacity.exceeded", "The processing queue is full. Try again later.", 503, headers={"Retry-After": "5"})
        job = prepared or self.prepare(kind, url, owner=owner, job_id=job_id)
        try:
            job.status = "queued"
            self.persist(job)
            admission.accepted(job.id)
            self.pending.put_nowait((job, operation))
        except Exception:
            # No worker has this job yet. Fail closed and remove its workspace,
            # even if persisting the failure itself is currently impossible.
            self.healthy = False
            job.status, job.completed_at = "error", now_iso()
            job.error = error_body("internal.error", "The job could not be admitted because state persistence failed.")["error"]
            for action in (lambda: self.persist(job), lambda: admission.finish(job.id, False), lambda: storage.delete(job)):
                try:
                    action()
                except Exception:
                    logging.exception("Unable to recover admission %s; restore storage and restart", job.id)
            self.jobs.pop(job.id, None)
            raise
        self.publish(job)
        return job

    async def abort(self, job):
        job.cancelled, job.status, job.completed_at = True, "cancelled", now_iso()
        self.persist(job)
        admission.finish(job.id, False)
        await run_sync(storage.delete, job)
        self.jobs.pop(job.id, None)

    async def supervise(self, job, operation):
        task = asyncio.create_task(operation(job))
        self.operations[job.id] = task
        deadline = time.monotonic() + settings.job_timeout_seconds
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=1)
                if done:
                    break
                await run_sync(storage.check_job_size, job)
                if time.monotonic() >= deadline:
                    raise AppError("net.timeout", "The job exceeded the configured processing time.", 408)
            await task
            await run_sync(storage.check_job_size, job)
        finally:
            if not task.done():
                job.cancelled = True
                task.cancel()
            # Blocking adapters wait for writers before this task can finish.
            await asyncio.gather(task, return_exceptions=True)
            self.operations.pop(job.id, None)

    async def worker(self):
        while True:
            job, operation = await self.pending.get()
            try:
                if job.cancelled:
                    raise AppError("job.cancelled", "The job was cancelled.")
                job.status, job.stage, job.started_at = "running", "Starting", now_iso()
                admission.started(job.id)
                self.persist(job)
                self.publish(job)
                await self.supervise(job, operation)
                if job.cancelled:
                    raise AppError("job.cancelled", "The job was cancelled.")
                await run_sync(storage.complete, job)
                job.status, job.stage, job.percent = "done", "Done", 100
            except asyncio.CancelledError:
                job.status = "error" if self.stopping else "cancelled"
                job.error = error_body("job.interrupted" if self.stopping else "job.cancelled", "The job was stopped.")["error"]
                if self.stopping:
                    raise
            except AppError as exc:
                job.status = "cancelled" if exc.code == "job.cancelled" else "error"
                job.error = error_body(exc.code, str(exc))["error"]
            except Exception:
                logging.exception("Job %s failed", job.id)
                job.status = "error"
                job.error = error_body("internal.error", "The job could not be completed.")["error"]
            finally:
                if job.status in {"error", "cancelled"}:
                    job.completed_at = now_iso()
                    try:
                        await run_sync(storage.delete, job)
                    except (OSError, ValueError):
                        logging.exception("Unable to clean failed job %s; cleanup will retry", job.id)
                try:
                    self.persist(job)
                    admission.finish(job.id, bool(job.started_at))
                    self.publish(job)
                except Exception:
                    self.healthy = False
                    logging.exception("Unable to finalize job %s; new admission disabled until storage is restored and the server restarted", job.id)
                finally:
                    self.jobs.pop(job.id, None)
                    self.pending.task_done()

    async def cancel(self, job):
        job.cancelled = True
        if job.status == "queued":
            job.status, job.completed_at = "cancelled", now_iso()
            job.error = error_body("job.cancelled", "The job was cancelled.")["error"]
            self.persist(job)
            admission.finish(job.id, False)
            try:
                await run_sync(storage.delete, job)
            except (OSError, ValueError):
                logging.exception("Unable to clean cancelled job %s; cleanup will retry", job.id)
            self.jobs.pop(job.id, None)
        task = self.operations.get(job.id)
        if task:
            task.cancel()
        self.persist(job)
        self.publish(job)

    async def cleanup(self):
        for history in self.histories:
            for job in history.scan():
                if job.id in self.jobs:
                    continue
                state = storage.availability(job)
                if state != job.availability:
                    job.availability = state
                    history.save(job)
                    self.publish(job)
                if state == "expired" or job.status in {"error", "cancelled"}:
                    try:
                        deleted = await run_sync(storage.delete, job)
                    except (OSError, ValueError):
                        logging.exception("Unable to clean job %s; will retry", job.id)
                        continue
                    end = job.expires_at or job.completed_at or job.created_at
                    if deleted and job.storage_mode == "temporary" and time.time() - timestamp(end) > settings.temp_history_ttl_seconds:
                        history.remove(job.id)
        # Only marked UUID workspaces belonging to this app, not arbitrary /tmp.
        for entry in self.media_roots:
            root = Path(entry["root"])
            root = root / "jobs" if entry["mode"] == "temporary" else root
            if not root.is_dir():
                continue
            for folder in root.iterdir():
                if folder.is_symlink() or not folder.is_dir() or len(folder.name) != 32 or folder.name in self.jobs:
                    continue
                marker = folder / ".muvio-job"
                if marker.is_symlink() or not marker.is_file() or time.time() - marker.stat().st_mtime < settings.temp_cleanup_interval_seconds:
                    continue
                exists = False
                for history in self.histories:
                    with history.connect() as conn:
                        exists = bool(conn.execute("SELECT 1 FROM jobs WHERE id=?", (folder.name,)).fetchone())
                    if exists:
                        break
                if not exists:
                    orphan = Job(folder.name, "orphan", storage_mode=entry["mode"], storage_root=entry["root"])
                    try:
                        await run_sync(storage.delete, orphan)
                    except (OSError, ValueError):
                        logging.exception("Unable to clean orphan %s", folder.name)
        admission.prune()

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(settings.temp_cleanup_interval_seconds)
            try:
                await self.cleanup()
            except Exception:
                logging.exception("Retention cleanup failed; will retry")


queue = Queue()
