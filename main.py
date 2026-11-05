"""Muvio FastAPI application and static frontend."""
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start local workers and restore history."""
    load_settings()
    if settings.api_key_required and not settings.api_key:
        raise RuntimeError("API_KEY_REQUIRED needs a configured API_KEY")
    app.state.signing_key = (settings.signing_secret or secrets.token_hex(32)).encode()
    await queue.start()
    logging.info("Muvio ready on http://localhost:3838")
    try:
        yield
    finally:
        await queue.stop()


app = FastAPI(title="muvio", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_allow_origin.split(",") if x.strip()], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])
_requests: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Require optional API key for protected endpoints."""
    if settings.api_key_required and request.url.path.startswith("/api/") and request.url.path != "/api/health" and not request.url.path.startswith("/api/files/"):
        value = request.headers.get("Authorization", "")
        if not hmac.compare_digest(value, "Api-Key " + settings.api_key):
            return JSONResponse(error_body("auth.missing", "Enter the API key in Settings."), status_code=401)
    if request.method == "POST" and request.url.path in {"/api/download", "/api/transcribe"}:
        client = request.client.host if request.client else "unknown"
        recent = _requests[client]
        now = time.monotonic()
        while recent and now - recent[0] > settings.rate_limit_window:
            recent.popleft()
        if len(recent) >= settings.rate_limit_max:
            return JSONResponse(error_body("rate.limited", "Too many jobs. Please try again shortly."), status_code=429, headers={"RateLimit-Limit": str(settings.rate_limit_max), "RateLimit-Remaining": "0", "RateLimit-Reset": str(max(0, int(settings.rate_limit_window - (now - recent[0]))))})
        recent.append(now)
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    """Return structured application failures."""
    return JSONResponse(error_body(exc.code, str(exc)), status_code=exc.status_code)


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
