"""Signed anonymous browser identities; the installation key grants a role only.

Accounts are deliberately not implemented until an identity provider is chosen.
No IP address, media URL or plaintext session token is stored in the state DB.
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from fastapi import Request

from app.config import settings
from app.errors import AppError

COOKIE = "muvio-session"


@dataclass(frozen=True)
class Principal:
    owner: str
    admin: bool = False

    @property
    def owners(self) -> tuple[str, ...]:
        return (self.owner, "admin:legacy") if self.admin else (self.owner,)


def signing_key() -> bytes:
    """Persist a private secret atomically, independently of media retention."""
    settings.state_root.mkdir(parents=True, exist_ok=True)
    if settings.signing_secret:
        return settings.signing_secret.encode()
    path = settings.state_root / "signing.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_bytes()
        if len(value) < 32:
            raise RuntimeError("Invalid signing.key; restore the stable secret")
        return value
    value = secrets.token_hex(32).encode()
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
    return value


def identify(request: Request) -> tuple[Principal, str]:
    """Validate an opaque cookie; API keys never merge visitor identities."""
    key = request.app.state.signing_key
    cookie = request.cookies.get(COOKIE, "")
    token, _, signature = cookie.partition(".")
    expected = hmac.new(key, ("session:" + token).encode(), hashlib.sha256).hexdigest()
    if len(token) != 64 or not hmac.compare_digest(signature, expected):
        token = secrets.token_hex(32)
        signature = hmac.new(key, ("session:" + token).encode(), hashlib.sha256).hexdigest()
    admin = bool(settings.api_key) and hmac.compare_digest(request.headers.get("Authorization", ""), "Api-Key " + settings.api_key)
    return Principal("anon:" + hashlib.sha256(token.encode()).hexdigest(), admin), token + "." + signature


def principal(request: Request) -> Principal:
    return request.state.principal


def require_admin(request: Request) -> Principal:
    actor = principal(request)
    if not actor.admin:
        raise AppError("auth.forbidden", "An administrator API key is required for this operation.", 403)
    return actor


def check_csrf(request: Request) -> None:
    """Require a non-simple header and reject foreign origins on all writes."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = request.headers.get("origin")
    expected = (settings.public_origin or str(request.base_url).rstrip("/")).rstrip("/")
    if request.headers.get("X-Muvio-Request") != "1" or (origin and origin != expected):
        raise AppError("auth.csrf", "The request origin or CSRF header is invalid.", 403)
