"""Admin authentication.

Two layers, both optional and independent:

* the Olares gateway, which only routes the admin entrance after the user has
  signed in (the entrance ships with authLevel `private`);
* an optional password (``VS_ADMIN_PASSWORD``) enforced inside the app, so the
  admin interface is still protected if the entrance is ever made public.

When no password is configured the app trusts the gateway and says so on
every page, rather than pretending to be protected.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from fastapi import Request

from . import db
from .config import settings
from .errors import ApiError
from .util import now, now_iso, parse_iso, token_hash

COOKIE_NAME = "vs_admin"


def password_required() -> bool:
    return bool(settings.admin_password)


def gateway_user(request: Request) -> str:
    for header in ("x-bfl-user", "x-auth-request-user", "x-forwarded-user", "x-olares-user"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    return ""


def create_session(label: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(hours=max(1, settings.admin_session_hours))
    db.execute(
        "INSERT INTO admin_sessions(token_hash, created_at, expires_at, label) "
        "VALUES(?,?,?,?)",
        (token_hash(token), now_iso(), expires.isoformat().replace("+00:00", "Z"), label),
    )
    _prune()
    return token, expires.isoformat().replace("+00:00", "Z")


def _prune() -> None:
    db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now_iso(),))


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash(token),))


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    row = db.query_one(
        "SELECT expires_at FROM admin_sessions WHERE token_hash = ?", (token_hash(token),)
    )
    if row is None:
        return False
    expires = parse_iso(row["expires_at"])
    if expires is None or expires <= now():
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash(token),))
        return False
    return True


def check_password(candidate: str) -> bool:
    if not settings.admin_password:
        return True
    return hmac.compare_digest(candidate or "", settings.admin_password)


def is_authenticated(request: Request) -> bool:
    if not password_required():
        return True
    return valid_session(request.cookies.get(COOKIE_NAME))


def require(request: Request) -> str:
    """Return the acting identity, or raise 401."""
    if not is_authenticated(request):
        raise ApiError("UNAUTHORIZED", "Admin authentication required", status_code=401)
    return gateway_user(request) or "admin"
