"""Structured public error taxonomy."""
from fastapi import HTTPException

CODES = {"url.invalid", "url.unsupported", "media.unavailable", "media.private", "media.live", "media.bot_check", "format.unavailable", "ffmpeg.missing", "ffmpeg.failed", "whisper.disabled", "whisper.model_missing", "whisper.failed", "demucs.failed", "llm.failed", "file.not_found", "job.cancelled", "job.interrupted", "rate.limited", "net.timeout", "auth.missing", "internal.error"}


class AppError(Exception):
    """A failure safe to report to a client."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def error_body(code: str, message: str) -> dict:
    """Build the common JSON error envelope."""
    return {"status": "error", "error": {"code": code, "context": {"message": message}}}


def fail(code: str, message: str, status: int = 400) -> HTTPException:
    """Construct an HTTP error with the shared envelope."""
    return HTTPException(status_code=status, detail=error_body(code, message))
