"""Bounded background job queue and persistent history."""
import asyncio
import json
import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

from app.config import settings
from app.errors import AppError, error_body


@dataclass
class Job:
    """A queued operation and its public progress state."""

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
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cancelled: bool = False
    last_update: float = 0

    def public(self) -> dict:
        """Serialize public job fields without exposing filesystem paths."""
        return {key: getattr(self, key) for key in ("id", "kind", "url", "status", "stage", "percent", "speed", "eta", "filename", "error", "warning", "created_at")} | {"files": list(self.files)}


class Queue:
    """Execute blocking extraction in threads while keeping API requests responsive."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.pending: asyncio.Queue[tuple[Job, Any]] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.db = settings.output_dir.parent / "history.db"
        self.loop: asyncio.AbstractEventLoop | None = None

    def persist(self, job: Job) -> None:
        """Write a compact history row in a worker thread or for short mutations."""
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", (job.id, json.dumps(job.__dict__)))

    def publish(self, job: Job) -> None:
        """Notify each SSE subscriber of a changed job."""
        data = job.public()
        for subscriber in list(self.subscribers):
            if subscriber.full():
                subscriber.get_nowait()
            subscriber.put_nowait(data)

    def update(self, job: Job, percent: float, speed: float | None, eta: float | None, stage: str) -> None:
        """Throttle thread-safe progress to ten events per second."""
        now = time.monotonic()
        if now - job.last_update < 0.1 and stage == job.stage:
            return
        job.last_update = now
        job.percent, job.speed, job.eta, job.stage = min(99, percent), speed, eta, stage
        if self.loop:
            self.loop.call_soon_threadsafe(self.publish, job)

    async def start(self) -> None:
        """Open the history database and start the configured number of workers."""
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        self.db = settings.output_dir.parent / "history.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        interrupted = []
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            for job_id, data in conn.execute("SELECT id, data FROM jobs"):
                try:
                    job = Job(**{k: v for k, v in json.loads(data).items() if k in Job.__dataclass_fields__})
                    if job.status in {"running", "queued"}:
                        job.status = "error"
                        job.error = error_body("job.interrupted", "The server restarted before this job finished.")["error"]
                        interrupted.append(job)
                    self.jobs[job_id] = job
                except (TypeError, ValueError) as exc:
                    logging.warning("Invalid history row %s: %s", job_id, exc)
        for job in interrupted:
            self.persist(job)
        self.loop = asyncio.get_running_loop()
        self.workers = [asyncio.create_task(self.worker()) for _ in range(max(1, settings.max_concurrent_jobs))]

    async def stop(self) -> None:
        """Stop workers and mark in-flight work as interrupted."""
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        for job in self.jobs.values():
            if job.status in {"queued", "running"}:
                job.cancelled = True
                job.status = "error"
                job.error = error_body("job.interrupted", "The server stopped during this job.")["error"]
                self.persist(job)

    def submit(self, kind: str, url: str, operation: Any) -> Job:
        """Submit a job and immediately return its ID."""
        job = Job(uuid.uuid4().hex, kind, url)
        self.jobs[job.id] = job
        self.persist(job)
        self.pending.put_nowait((job, operation))
        self.publish(job)
        return job

    async def worker(self) -> None:
        """Process jobs independently; one failure never stops the queue."""
        while True:
            job, operation = await self.pending.get()
            try:
                if job.cancelled:
                    continue
                job.status, job.stage = "running", "Starting"
                self.publish(job)
                await operation(job)
                if job.cancelled:
                    raise AppError("job.cancelled", "The job was cancelled.")
                job.status, job.stage, job.percent = "done", "Done", 100
            except AppError as exc:
                job.status = "cancelled" if exc.code == "job.cancelled" else "error"
                job.error = error_body(exc.code, str(exc))["error"]
            except Exception as exc:
                logging.exception("Job %s failed", job.id)
                job.status = "error"
                job.error = error_body("internal.error", "The job failed: " + str(exc)[:250])["error"]
            finally:
                if job.status in {"error", "cancelled"}:
                    await anyio.to_thread.run_sync(shutil.rmtree, settings.output_dir / job.id, True)
                self.persist(job)
                self.publish(job)
                self.pending.task_done()

    def cancel(self, job: Job) -> None:
        """Request cooperative cancellation, including queued jobs."""
        job.cancelled = True
        if job.status == "queued":
            job.status = "cancelled"
            job.error = error_body("job.cancelled", "The job was cancelled.")["error"]
        self.persist(job)
        self.publish(job)


queue = Queue()
