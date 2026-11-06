"""Job-owned disk workspaces, retention and bounded file-transfer leases."""
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

import anyio
from starlette.responses import FileResponse

from app.config import settings
from app.errors import AppError

ROOT_MARKER = ".muvio-media-root"
JOB_MARKER = ".muvio-job"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def safe_root(root: Path, *, temporary=False) -> Path:
    """Reject overlapping protected roots before creating or cleaning anything."""
    resolved = root.resolve()
    protected = (settings.state_root.resolve(), settings.models_dir.resolve())
    if resolved == Path(resolved.anchor) or any(p == resolved or p.is_relative_to(resolved) for p in protected):
        raise ValueError("Media roots cannot contain the state/models root or be a filesystem root")
    if temporary:
        persistent = settings.output_dir.resolve()
        state = settings.state_root.resolve()
        if resolved.is_relative_to(state) or resolved.is_relative_to(persistent) or persistent.is_relative_to(resolved):
            raise ValueError("TEMP_STORAGE_DIR must be outside persistent state/media")
    if any(resolved.is_relative_to(p) for p in (settings.models_dir.resolve(),)):
        raise ValueError("Media roots cannot be inside MODELS_DIR")
    if root.is_symlink():
        raise ValueError("Media roots cannot be symlinks")
    return resolved


class Storage:
    def __init__(self):
        self.lock = RLock()
        self.leases: Counter = Counter()
        self.owners: Counter = Counter()
        self.roots: set[Path] = set()

    def start(self):
        persistent = safe_root(settings.output_dir)
        temporary = safe_root(settings.temp_storage_dir, temporary=True)
        for root in (persistent, temporary):
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker = root / ROOT_MARKER
            if marker.is_symlink():
                raise ValueError("Invalid media root marker")
            if marker.exists() and marker.read_text() != "muvio-v1\n":
                raise ValueError("Unrecognized media root marker")
            if not marker.exists():
                marker.write_text("muvio-v1\n")
        for name in ("jobs", "incoming"):
            child = temporary / name
            if child.is_symlink():
                raise ValueError("Temporary workspace directories cannot be symlinks")
            child.mkdir(exist_ok=True, mode=0o700)
        self.roots = {persistent, temporary}
        self.leases.clear()
        self.owners.clear()

    def assign(self, job):
        job.storage_mode = "persistent" if settings.persist_storage else "temporary"
        job.storage_root = str((settings.output_dir if settings.persist_storage else settings.temp_storage_dir).resolve())

    def folder(self, job) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job.id):
            raise ValueError("Invalid server job identifier")
        root = Path(job.storage_root) if job.storage_root else settings.output_dir
        if job.storage_mode != "legacy":
            root = safe_root(root, temporary=job.storage_mode == "temporary")
            if not (root / ROOT_MARKER).is_file() or (root / ROOT_MARKER).is_symlink():
                raise ValueError("Unmanaged media root")
        folder = root / "jobs" / job.id if job.storage_mode == "temporary" else root / job.id
        if folder.is_symlink() or folder.parent.is_symlink() or folder.resolve() != folder.absolute():
            raise ValueError("Symlink workspace is not allowed")
        return folder

    def create(self, job) -> Path:
        folder = self.folder(job)
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        (folder / JOB_MARKER).write_text(job.id)
        return folder

    def path(self, job, value: str) -> Path:
        folder = self.folder(job).resolve()
        path = Path(value)
        if ".." in path.parts:
            raise ValueError("Parent traversal is not allowed")
        if not path.is_absolute():
            # Historical local installs recorded paths relative to the process
            # working directory, rather than relative to the job workspace.
            old = path.absolute()
            path = old if job.storage_mode == "legacy" and old.resolve().is_relative_to(folder) else folder / path
        if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.resolve() == folder:
            raise ValueError("Result path escapes its workspace")
        current = path
        while current != folder:
            if current.is_symlink():
                raise ValueError("Symlink results are not allowed")
            current = current.parent
        return path

    def complete(self, job):
        """Normalize paths and remove intermediates, retaining all named results."""
        folder = self.folder(job)
        keep = set()
        if job.output_path:
            path = self.path(job, job.output_path)
            if not path.is_file():
                raise AppError("file.not_found", "No output file was produced.")
            job.output_path = str(path.relative_to(folder))
            keep.add(path)
        for ext, value in job.files.items():
            path = self.path(job, value)
            if not path.is_file():
                raise AppError("file.not_found", "A result file is missing.")
            job.files[ext] = str(path.relative_to(folder))
            keep.add(path)
        if not keep and job.kind != "model":
            raise AppError("file.not_found", "No output file was produced.")
        # All engine outputs currently live immediately under the job directory.
        # Keep ancestors too, so future nested exports remain safe.
        for child in folder.iterdir():
            if child.name == JOB_MARKER or child in keep or any(p.is_relative_to(child) for p in keep):
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        now = utcnow()
        job.completed_at = now.isoformat()
        job.expires_at = (now + timedelta(seconds=settings.temp_file_ttl_seconds)).isoformat() if job.storage_mode == "temporary" else None
        job.availability = "available" if keep else "missing"

    def availability(self, job) -> str:
        if job.status != "done":
            return "pending" if job.status in {"receiving", "queued", "running"} else "missing"
        if job.expires_at and timestamp(job.expires_at) <= time.time():
            return "expired"
        if job.availability in {"expired", "missing"}:
            return job.availability
        try:
            values = set(job.files.values()) | ({job.output_path} if job.output_path else set())
            return "available" if values and all(self.path(job, p).is_file() for p in values) else "missing"
        except (ValueError, OSError):
            return "missing"

    def delete(self, job) -> bool:
        """Only remove a server-created marked folder, never legacy downloads."""
        if job.storage_mode == "legacy":
            return False
        with self.lock:
            if self.leases[job.id]:
                return False
            folder = self.folder(job)
            if not folder.exists():
                return True
            marker = folder / JOB_MARKER
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != job.id:
                raise ValueError("Refusing to delete an unmarked workspace")
            shutil.rmtree(folder)
            return True

    def acquire(self, job):
        with self.lock:
            state = self.availability(job)
            if state != "available":
                raise AppError("file.expired" if state == "expired" else "file.not_found", "The result has expired." if state == "expired" else "The result is unavailable.", 410 if state == "expired" else 404)
            if sum(self.leases.values()) >= settings.max_transfers or self.owners[job.owner] >= settings.max_transfers_per_owner:
                raise AppError("capacity.exceeded", "Too many active file transfers.", 429, headers={"Retry-After": "5"})
            self.leases[job.id] += 1
            self.owners[job.owner] += 1

    def release(self, job):
        with self.lock:
            self.leases[job.id] -= 1
            self.owners[job.owner] -= 1
            if not self.leases[job.id]:
                del self.leases[job.id]
            if not self.owners[job.owner]:
                del self.owners[job.owner]

    def usage(self, root: Path) -> int:
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())

    def check_capacity(self, active_count=0):
        root = settings.output_dir if settings.persist_storage else settings.temp_storage_dir
        reserved = (active_count + 1) * settings.max_job_mb * 1024 * 1024
        roots = self.roots
        if (sum(self.usage(p) for p in roots) + reserved > settings.max_storage_mb * 1024 * 1024
                or shutil.disk_usage(root).free < reserved + settings.min_free_disk_mb * 1024 * 1024):
            raise AppError("capacity.exceeded", "The server has no storage capacity for another job.", 503, headers={"Retry-After": "60"})

    def check_job_size(self, job):
        if self.usage(self.folder(job)) > settings.max_job_mb * 1024 * 1024:
            raise AppError("capacity.exceeded", "The job exceeded the configured disk limit.", 413)


storage = Storage()


class LeasedFileResponse(FileResponse):
    """Release even on Range errors, disconnects, timeouts and send failures."""
    def __init__(self, job, path):
        super().__init__(path, filename=path.name, media_type="application/octet-stream")
        self.job = job

    async def __call__(self, scope, receive, send):
        # pathsend can hand transfer ownership to a server beyond this coroutine;
        # use normal streamed FileResponse delivery so lease lifetime is exact.
        scope = dict(scope, extensions={k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"})
        try:
            with anyio.fail_after(settings.transfer_timeout_seconds):
                await super().__call__(scope, receive, send)
        finally:
            storage.release(self.job)
