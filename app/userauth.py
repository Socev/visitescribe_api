"""Sign-in for the user-facing site.

The identity comes from OurMind: a user proves who they are by receiving a
code at the e-mail address their OurMind account uses, and we exchange that
code for a token belonging to them. That single act does two things at once --
it establishes who is looking at the page, and it hands us the credential we
need to send their audio to their own OurMind account.

The cookie issued here is OUR session, not OurMind's token. They have
different lifetimes and different jobs: the cookie says "this browser is
Marieke", the token says "act as Marieke at OurMind". Keeping them apart means
a stolen cookie cannot be replayed against OurMind, and an expired OurMind
token logs you out of OurMind, not out of this site.

Admin cookies are a separate table with a separate name. A user must never be
able to reach the admin interface by having signed in here.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import Request

from . import db, users
from .errors import ApiError
from .util import now, now_iso, token_hash

COOKIE_NAME = "vs_user"
SESSION_HOURS = 12

# A login attempt is a request for e-mail to be sent to somebody. Bound so
# this cannot be used to send mail at someone, or to work through a list of
# addresses to find which ones exist.
MAX_ATTEMPTS = 8


def start_login(email: str) -> None:
    """Ask OurMind's auth service to e-mail a code.

    Says nothing about whether the address is known. The page shows the same
    message either way, and no user row is created here: an address that has
    not been given a device by an admin gets a code it cannot use, which is
    the correct amount of information to leak (none).
    """
    from .providers import supabase_auth

    address = users.normalise_email(email)
    if "@" not in address or len(address) > 254:
        raise ApiError("INVALID_REQUEST", "Vul een geldig e-mailadres in.")

    row = db.query_one("SELECT * FROM login_attempts WHERE email = ?", (address,))
    if row is not None and row["attempts"] >= MAX_ATTEMPTS:
        raise ApiError("RATE_LIMITED",
                       "Te veel inlogpogingen voor dit adres. Probeer het later "
                       "opnieuw.", status_code=429)
    db.execute(
        "INSERT INTO login_attempts(email, started_at, attempts) VALUES(?,?,1) "
        "ON CONFLICT(email) DO UPDATE SET started_at = excluded.started_at, "
        "attempts = login_attempts.attempts + 1",
        (address, now_iso()),
    )
    supabase_auth.request_code(address)


def finish_login(email: str, code: str) -> tuple[str, dict[str, Any]]:
    """Exchange the code for a session cookie, and remember the OurMind token.

    The user must already exist: an admin creates the account and binds a
    device to it. Someone with a valid OurMind account but no place here gets
    told to ask an administrator, rather than being quietly given an empty
    page or, worse, an account.
    """
    from . import audit
    from .providers import supabase_auth

    address = users.normalise_email(email)
    session = supabase_auth.verify_code(address, code)

    user = users.by_email(address)
    if user is None:
        audit.log("user_login", "unknown_account", "failure", identity=address)
        raise ApiError(
            "NO_ACCOUNT",
            "Dit adres is bekend bij OurMind, maar heeft hier nog geen account. "
            "Vraag de beheerder om je toe te voegen en een recorder te koppelen.",
            status_code=403,
        )
    if user["disabled"]:
        audit.log("user_login", "disabled_account", "failure", identity=address)
        raise ApiError("ACCOUNT_DISABLED", "Dit account is uitgeschakeld.",
                       status_code=403)

    users.store_token(user["user_id"], session["access_token"],
                      session["refresh_token"], session["expires_at"])
    db.execute("DELETE FROM login_attempts WHERE email = ?", (address,))

    # Fill in the name and practice from OurMind, so the admin does not have to
    # type them and they stay right when they change there.
    try:
        from .providers import get as get_provider

        me = get_provider("ourmind", token=session["access_token"]).me()
        users.update_profile(user["user_id"], display_name=me.get("name") or None,
                             org_name=me.get("org_name") or None)
    except Exception:  # noqa: BLE001
        pass   # a name is a nicety; being signed in is not

    token = _create_session(user["user_id"])
    users.touch(user["user_id"])
    audit.log("user_login", "signed_in", "success", identity=address,
              detail={"user_id": user["user_id"]})
    return token, users.get(user["user_id"])   # type: ignore[return-value]


def _create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(hours=SESSION_HOURS)
    db.execute(
        "INSERT INTO user_sessions(token_hash, user_id, created_at, expires_at) "
        "VALUES(?,?,?,?)",
        (token_hash(token), user_id, now_iso(),
         expires.isoformat().replace("+00:00", "Z")),
    )
    db.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now_iso(),))
    return token


def sign_out(token: str | None) -> None:
    """Ends the browser session. Deliberately does NOT drop the OurMind token.

    Signing out of this site should not silently stop recordings that are
    still queued from being processed. Disconnecting OurMind is its own,
    clearly labelled action.
    """
    if token:
        db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash(token),))


def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.query_one(
        "SELECT s.user_id, s.expires_at FROM user_sessions s WHERE s.token_hash = ?",
        (token_hash(token),))
    if row is None:
        return None
    expires = parse_expiry(row["expires_at"])
    if expires is not None and expires < now():
        db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash(token),))
        return None
    user = users.get(row["user_id"])
    if user is None or user["disabled"]:
        return None
    return user


def parse_expiry(value: str):
    from .util import parse_iso

    return parse_iso(value)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise ApiError("NOT_SIGNED_IN", "Log opnieuw in.", status_code=401)
    return user
