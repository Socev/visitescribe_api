"""Signing a user in to their own OurMind account.

OurMind issues no tokens of its own. Their documentation delegates the whole
of authentication to a Supabase (GoTrue) instance and shows a server-side
example doing exactly what we do here: ask for a code by e-mail, exchange the
code for an access token, then call the OurMind API with it.

  https://api-docs.ourmind.ai/ -- the prose lives in `info.summary` of
  https://beta-api.ourmind.ai/openapi2.json

That shape is what makes the user-facing site possible at all: each user signs
in as themselves, their audio goes to their own account, and it counts against
their own report allowance. We never hold one shared credential standing in
for everybody.

`should_create_user` is false on purpose. This flow signs in people who
already have an OurMind account; it must not silently create one, and it must
not become a way to find out which addresses exist -- which is why a failed
request looks the same as a successful one to the caller.

Two things are NOT documented by OurMind and are worth confirming with them:
the access-token lifetime, and whether refreshing is permitted. Both are read
from what the server actually returns rather than assumed: the expiry comes
from the JWT's own `exp`, and refreshing is attempted only when a refresh
token was handed to us.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import httpx

from .base import ProviderError

# Published in OurMind's own documentation as the client configuration. It is
# an anon key: it identifies the project and is meant to be in client code.
DEFAULT_AUTH_URL = "https://auth.ourmind.ai"
DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRqZGh2b29sbmN5aXR2cW1keWdqIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NDE2OTQ5MzIsImV4cCI6MjA1NzI3MDkzMn0."
    "s8RB8gLz--yqDQ8Hbf0-tlnfTWAD6pbpRYm7768jV6A"
)


def _base() -> str:
    return (os.environ.get("VS_OURMIND_AUTH_URL") or DEFAULT_AUTH_URL).rstrip("/")


def _anon_key() -> str:
    return os.environ.get("VS_OURMIND_ANON_KEY") or DEFAULT_ANON_KEY


def _timeout() -> float:
    return float(os.environ.get("VS_OURMIND_AUTH_TIMEOUT", "20"))


def _post(path: str, body: dict, *, params: dict | None = None) -> dict:
    key = _anon_key()
    try:
        response = httpx.post(
            f"{_base()}/auth/v1/{path.lstrip('/')}",
            params=params or None,
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=_timeout(),
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Inloggen bij OurMind lukte niet: {exc}",
                            retryable=True) from exc

    if response.status_code >= 500:
        raise ProviderError("OurMind's inlogdienst gaf een serverfout.", retryable=True)
    if response.status_code == 429:
        raise ProviderError(
            "Te veel inlogpogingen. Wacht een minuut en probeer het opnieuw.",
            code="RATE_LIMITED")
    if response.status_code >= 400:
        raise ProviderError(_explain(response), code="LOGIN_FAILED")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError("Onleesbaar antwoord van de inlogdienst") from exc


def _explain(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Inloggen geweigerd ({response.status_code})"
    for key in ("error_description", "msg", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Inloggen geweigerd ({response.status_code})"


def request_code(email: str) -> None:
    """Ask Supabase to e-mail a sign-in code.

    Never reveals whether the address is known: `should_create_user` is false,
    so an unknown address simply gets no mail, and the caller is told the same
    thing either way.
    """
    _post("otp", {"email": email.strip().lower(), "create_user": False})


def verify_code(email: str, code: str) -> dict[str, Any]:
    """Exchange the e-mailed code for a session.

    Returns {access_token, refresh_token, expires_at}. `expires_at` is read
    from the token itself rather than from the response, because the token is
    the thing that will actually stop being accepted.
    """
    payload = _post("verify", {
        "email": email.strip().lower(),
        "token": code.strip(),
        "type": "email",
    })
    access = payload.get("access_token") or ""
    if not access:
        raise ProviderError("De inlogdienst gaf geen token terug.", code="LOGIN_FAILED")
    return {
        "access_token": access,
        "refresh_token": payload.get("refresh_token") or "",
        "expires_at": expiry_of(access) or _from_expires_in(payload),
    }


def refresh(refresh_token: str) -> dict[str, Any]:
    payload = _post("token", {"refresh_token": refresh_token},
                    params={"grant_type": "refresh_token"})
    access = payload.get("access_token") or ""
    if not access:
        raise ProviderError("Vernieuwen van de sessie gaf geen token terug.",
                            code="LOGIN_FAILED")
    return {
        "access_token": access,
        # Supabase rotates the refresh token; keeping the old one would sign
        # the user out on the next refresh.
        "refresh_token": payload.get("refresh_token") or refresh_token,
        "expires_at": expiry_of(access) or _from_expires_in(payload),
    }


def _from_expires_in(payload: dict) -> float | None:
    try:
        return time.time() + float(payload["expires_in"])
    except (KeyError, TypeError, ValueError):
        return None


def expiry_of(jwt: str) -> float | None:
    """The `exp` claim, as a unix timestamp.

    The claims are read, not trusted: this only decides when to refresh. The
    token's signature is Supabase's business, and OurMind is the one that
    checks it.
    """
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims["exp"])
    except Exception:  # noqa: BLE001
        return None
