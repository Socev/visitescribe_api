"""Users, the devices bound to them, and what happens to their recordings.

A recording arrives with nothing but an X-Device-ID. Everything downstream --
whose OurMind account it goes to, which template makes the report, whether it
goes anywhere at all -- follows from which user that device is bound to. The
binding is an admin act, deliberately: a recorder is a physical object handed
to a person, so the coupling is made by whoever hands it over, not claimed by
whoever is holding it.

The three tables here are read on every processed recording, so they are kept
plain: no ORM, no caching, no clever invalidation.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from . import db, secretbox
from .errors import ApiError
from .util import now_iso

# How long before a token actually expires we go and refresh it. A job that
# takes ten minutes must not start with nine minutes of validity left.
REFRESH_MARGIN_SECONDS = 300


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def get(user_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return dict(row) if row else None


def by_email(email: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM users WHERE email = ?", (normalise_email(email),))
    return dict(row) if row else None


def listing() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT u.*, (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.user_id) "
        "AS device_count, (SELECT COUNT(*) FROM user_tokens t "
        "WHERE t.user_id = u.user_id) AS signed_in FROM users u ORDER BY u.email"
    )
    return [dict(r) for r in rows]


def create(email: str, *, display_name: str = "", actor: str = "admin") -> dict[str, Any]:
    address = normalise_email(email)
    if "@" not in address or len(address) > 254:
        raise ApiError("INVALID_REQUEST", "Dat is geen e-mailadres.")
    if by_email(address) is not None:
        raise ApiError("ALREADY_EXISTS", "Deze gebruiker bestaat al.")
    ts = now_iso()
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users(user_id, email, display_name, created_at, updated_at) "
        "VALUES(?,?,?,?,?)",
        (user_id, address, display_name.strip()[:120], ts, ts),
    )
    from . import audit

    audit.log("users", "created", "success", identity=actor,
              detail={"email": address, "user_id": user_id})
    return get(user_id)  # type: ignore[return-value]


def set_disabled(user_id: str, disabled: bool, *, actor: str = "admin") -> None:
    db.execute("UPDATE users SET disabled = ?, updated_at = ? WHERE user_id = ?",
               (1 if disabled else 0, now_iso(), user_id))
    from . import audit

    audit.log("users", "disabled" if disabled else "enabled", "success",
              identity=actor, detail={"user_id": user_id})


def touch(user_id: str) -> None:
    db.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?",
               (now_iso(), user_id))


def update_profile(user_id: str, *, display_name: str | None = None,
                   org_name: str | None = None) -> None:
    sets, params = [], []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name.strip()[:120])
    if org_name is not None:
        sets.append("org_name = ?")
        params.append(org_name.strip()[:120])
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([now_iso(), user_id])
    db.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", tuple(params))


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

def devices_of(user_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM devices WHERE user_id = ? ORDER BY device_id", (user_id,))]


def unbound_devices() -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM devices WHERE user_id IS NULL OR user_id = '' "
        "ORDER BY device_id")]


def bind_device(device_id: str, user_id: str | None, *, actor: str = "admin") -> None:
    """Point a recorder at a user, or at nobody.

    A device belongs to at most one user; a user may hold several. Rebinding
    is allowed and is audited, because it changes where every future recording
    from that recorder ends up.
    """
    device = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    if device is None:
        raise ApiError("UNKNOWN_DEVICE", "Onbekend device")
    if user_id:
        if get(user_id) is None:
            raise ApiError("UNKNOWN_USER", "Onbekende gebruiker")
    db.execute("UPDATE devices SET user_id = ? WHERE device_id = ?",
               (user_id or None, device_id))
    from . import audit

    audit.log("devices", "bound" if user_id else "unbound", "success", identity=actor,
              device_id=device_id, detail={"user_id": user_id,
                                           "previous": device["user_id"]})


def owner_of_device(device_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT u.* FROM users u JOIN devices d ON d.user_id = u.user_id "
        "WHERE d.device_id = ?", (device_id,))
    return dict(row) if row else None


def owner_of_session(session_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT u.* FROM users u JOIN devices d ON d.user_id = u.user_id "
        "JOIN sessions s ON s.device_id = d.device_id WHERE s.session_id = ?",
        (session_id,))
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# OurMind tokens
# ---------------------------------------------------------------------------

def store_token(user_id: str, access_token: str, refresh_token: str,
                expires_at: float | None) -> None:
    from .util import iso_from_epoch

    db.execute(
        "INSERT INTO user_tokens(user_id, access_token, refresh_token, expires_at, "
        "updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
        "access_token = excluded.access_token, refresh_token = excluded.refresh_token, "
        "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
        (user_id, secretbox.seal(access_token), secretbox.seal(refresh_token or ""),
         iso_from_epoch(expires_at) if expires_at else None, now_iso()),
    )


def forget_token(user_id: str) -> None:
    db.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))


def token_status(user_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM user_tokens WHERE user_id = ?", (user_id,))
    if row is None:
        return {"present": False, "expires_at": None, "expired": True}
    from .util import epoch_from_iso

    expires = epoch_from_iso(row["expires_at"]) if row["expires_at"] else None
    return {
        "present": True,
        "expires_at": row["expires_at"],
        "expired": bool(expires and expires <= time.time()),
        "refreshable": bool(secretbox.open_(row["refresh_token"])),
    }


def access_token(user_id: str) -> str:
    """A usable OurMind token, refreshed if it is about to lapse.

    Raises rather than returning an empty string: a caller that gets a token
    should be able to use it, and one that cannot must stop rather than
    quietly send a recording nowhere.
    """
    from .util import epoch_from_iso
    from .errors import ApiError as _ApiError

    row = db.query_one("SELECT * FROM user_tokens WHERE user_id = ?", (user_id,))
    if row is None:
        raise _ApiError("OURMIND_SIGNIN_REQUIRED",
                        "Deze gebruiker is niet ingelogd bij OurMind.")
    token = secretbox.open_(row["access_token"])
    expires = epoch_from_iso(row["expires_at"]) if row["expires_at"] else None
    if expires is not None and expires - REFRESH_MARGIN_SECONDS <= time.time():
        refresh_token = secretbox.open_(row["refresh_token"])
        if not refresh_token:
            raise _ApiError("OURMIND_SIGNIN_REQUIRED",
                            "De OurMind-sessie is verlopen. Log opnieuw in.")
        from .providers import supabase_auth

        session = supabase_auth.refresh(refresh_token)
        store_token(user_id, session["access_token"], session["refresh_token"],
                    session["expires_at"])
        return session["access_token"]
    return token


# ---------------------------------------------------------------------------
# recording types and routing
# ---------------------------------------------------------------------------

def recording_types() -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM recording_types ORDER BY position, mode")]


def ensure_recording_type(mode: str) -> None:
    """Record a mode we have never seen before.

    The hardware decides what it sends. When a recorder starts reporting a new
    kind -- an MDO button, say -- it appears in everyone's settings by itself
    instead of being dropped on the floor until someone ships a release. It is
    marked as carrying patient audio until a human says otherwise, because
    that is the safe assumption to make about an unknown recording.
    """
    if not mode or db.query_one(
            "SELECT 1 FROM recording_types WHERE mode = ?", (mode,)) is not None:
        return
    db.execute(
        "INSERT INTO recording_types(mode, title, description, patient_audio, "
        "position, builtin, created_at) VALUES(?,?,?,1,500,0,?) "
        "ON CONFLICT(mode) DO NOTHING",
        (mode, mode.replace("_", " ").capitalize(),
         "Automatisch toegevoegd toen een recorder dit type instuurde.", now_iso()),
    )
    from . import audit

    audit.log("recording_types", "discovered", "success", identity="system",
              detail={"mode": mode})


def rules_for(user_id: str) -> dict[str, dict[str, Any]]:
    rows = db.query("SELECT * FROM routing_rules WHERE user_id = ?", (user_id,))
    return {r["mode"]: dict(r) for r in rows}


def rule(user_id: str, mode: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM routing_rules WHERE user_id = ? AND mode = ?", (user_id, mode))
    return dict(row) if row else None


def set_rule(user_id: str, mode: str, *, route: str = "", template_id: str = "",
             template_type: str = "", template_title: str = "",
             auto: bool = False, actor: str = "user") -> None:
    from . import routing

    if route:
        routing.check(mode, route)
    db.execute(
        "INSERT INTO routing_rules(user_id, mode, route, template_id, template_type, "
        "template_title, auto, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, mode) DO UPDATE SET route = excluded.route, "
        "template_id = excluded.template_id, template_type = excluded.template_type, "
        "template_title = excluded.template_title, auto = excluded.auto, "
        "updated_at = excluded.updated_at",
        (user_id, mode, route, template_id, template_type, template_title,
         1 if auto else 0, now_iso()),
    )
    from . import audit

    audit.log("routing", "rule_set", "success", identity=actor,
              detail={"user_id": user_id, "mode": mode, "route": route,
                      "template_id": template_id, "auto": bool(auto)})
