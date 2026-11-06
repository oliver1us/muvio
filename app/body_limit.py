"""Bound request bodies before multipart parsing/spooling, including chunked HTTP."""
from app.config import settings
from app.errors import fail


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        maximum = (settings.max_upload_mb + 1) * 1024 * 1024 if scope["path"] == "/api/transcribe" else 1024 * 1024
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    # HTTPException lets Starlette's multipart parser close all
                    # spooled file descriptors on its error path.
                    raise fail("capacity.exceeded", "The request exceeds the configured size limit.", 413)
            return message

        await self.app(scope, bounded_receive, send)
