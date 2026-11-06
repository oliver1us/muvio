"""Muvio FastAPI application and static frontend."""
import logging
import os
import tempfile
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import load_settings, settings
from app.errors import AppError, error_body
from app.queue.jobs import queue
from app.identity import COOKIE, check_csrf, identify, signing_key
from app.body_limit import BodyLimitMiddleware


class RedactDownloadQueries(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if str(args[2]).startswith("/api/files/"):
                args[2] = str(args[2]).split("?", 1)[0]
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactDownloadQueries())


@contextmanager
def single_instance():
    """Refuse multiple workers/processes sharing this installation's state."""
    import fcntl
    settings.state_root.mkdir(parents=True, exist_ok=True)
    with (settings.state_root / ".instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Muvio requires one process per STATE_DIR; do not add workers/replicas") from exc
        yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start local workers and restore history."""
    load_settings()
    os.umask(0o077)
    if settings.state_dir is None:
        logging.warning("STATE_DIR is unset; preserving legacy state at %s. Set STATE_DIR explicitly before changing OUTPUT_DIR.", settings.state_root)
    if settings.api_key_required and not settings.api_key:
        raise RuntimeError("API_KEY_REQUIRED needs a configured API_KEY")
    with single_instance():
        app.state.signing_key = signing_key()
        await queue.start()
        incoming = settings.temp_storage_dir / "incoming"
        incoming.mkdir(mode=0o700, exist_ok=True)
        old_tempdir, tempfile.tempdir = tempfile.tempdir, str(incoming.resolve())
        logging.info("Muvio ready on http://localhost:3838")
        try:
            yield
        finally:
            await queue.stop()
            tempfile.tempdir = old_tempdir


app = FastAPI(title="muvio", version="0.1.0", lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_allow_origin.split(",") if x.strip()], allow_methods=["GET"], allow_headers=["Authorization", "Content-Type"])
_requests: OrderedDict[str, deque[float]] = OrderedDict()


def application_error(exc: AppError):
    body = error_body(exc.code, str(exc))
    body["error"]["context"].update(exc.context)
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Resolve per-browser ownership, enforce CSRF and bound expensive requests."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    actor, cookie = identify(request)
    request.state.principal = actor
    try:
        check_csrf(request)
        path = request.url.path
        if settings.api_key_required and path not in {"/api/health", "/api/session"} and not path.startswith("/api/files/") and request.method != "OPTIONS" and not actor.admin:
            raise AppError("auth.missing", "Enter the installation API key in Settings.", 401)
        if (path == "/api/transcribe" or path.startswith("/api/models/") or path == "/api/settings") and not actor.admin:
            raise AppError("auth.forbidden", "An administrator API key is required for this operation.", 403)
        if request.method == "POST" and path == "/api/transcribe":
            length = request.headers.get("Content-Length")
            if length and int(length) > (settings.max_upload_mb + 1) * 1024 * 1024:
                raise AppError("capacity.exceeded", "The upload exceeds the configured size limit.", 413)
    except AppError as exc:
        return application_error(exc)
    expensive = request.url.path in {"/api/info", "/api/subtitles", "/api/download", "/api/subtitle-download", "/api/transcribe"} or request.url.path.startswith("/api/models/")
    if expensive:
        client = request.client.host if request.client else "unknown"
        recent = _requests.setdefault(client, deque())
        _requests.move_to_end(client)
        if len(_requests) > 10000:
            _requests.popitem(last=False)
        now = time.monotonic()
        while recent and now - recent[0] > settings.rate_limit_window:
            recent.popleft()
        if len(recent) >= settings.rate_limit_max:
            return JSONResponse(error_body("rate.limited", "Too many jobs. Please try again shortly."), status_code=429, headers={"RateLimit-Limit": str(settings.rate_limit_max), "RateLimit-Remaining": "0", "RateLimit-Reset": str(max(0, int(settings.rate_limit_window - (now - recent[0]))))})
        recent.append(now)
    response = await call_next(request)
    if not request.url.path.startswith("/api/files/"):
        response.set_cookie(COOKIE, cookie, max_age=settings.session_max_age_seconds, httponly=True,
                            secure=settings.cookie_secure, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    """Return structured application failures."""
    return application_error(exc)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """Keep HTTP failures in the same top-level error envelope."""
    body = exc.detail if isinstance(exc.detail, dict) and exc.detail.get("status") == "error" else error_body("file.not_found" if exc.status_code == 404 else "internal.error", str(exc.detail))
    return JSONResponse(body, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Return structured invalid-input failures."""
    return JSONResponse(error_body("url.invalid", "Invalid request: " + str(exc.errors())[:400]), status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Log unexpected errors without sending tracebacks to clients."""
    logging.exception("Unhandled request failure", exc_info=exc)
    return JSONResponse(error_body("internal.error", "An unexpected server error occurred."), status_code=500)


app.include_router(router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "app/web", html=True), name="web")
