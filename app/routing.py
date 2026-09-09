"""Which providers a recording of a given kind may reach.

The recorder states what kind of recording it made, and the policy follows
from that rather than from whoever set the route. The check runs twice: when a
job is queued and again in the worker, immediately before anything leaves the
machine. Once is not enough -- a job can sit in the queue while the policy
changes underneath it.

The rule is expressed the other way round from how it started. It used to be a
hardcoded table of mode -> providers, which meant a new recording type was
either forgotten or silently forbidden. Now each PROVIDER declares whether it
may receive patient audio, and each recording type declares whether it carries
any (`recording_types.patient_audio`). A new type discovered from a recorder
is treated as carrying patient audio until a human says otherwise, so the
unknown case fails closed.

Meetings may now go to OurMind. They could not before, on the reasoning that
OurMind models a recording as one consultation of one patient. That reasoning
weakened once report templates entered the picture: a meeting sent with a
meeting template is a report of a meeting, and it was David's explicit
instruction that a "Vergadering" should be routable to OurMind with its own
template. Recorded here rather than in a commit message, because the next
person to read this file will wonder why the restriction went.
"""
from __future__ import annotations

from .errors import ApiError

# provider -> may it receive audio containing patients?
PROVIDER_PATIENT_AUDIO: dict[str, bool] = {
    # Mistral: EU endpoint available, no training on API data, per-call
    # deletion is not needed because we send audio and keep nothing there.
    "mistral": True,
    # OurMind: processing within the EEA, ISO 27001 and NEN 7510, and the
    # recording is deleted again once the report is in.
    "ourmind": True,
}

PROVIDERS = tuple(PROVIDER_PATIENT_AUDIO)

# Considered and deliberately not built. Kept so a stale route in an old
# session gives a real explanation instead of "unknown provider".
RETIRED = {
    "plaud": "Plaud is niet gekoppeld: transcriptie zonder samenvatting, "
             "standaard buiten de EU, en geen verwijder-endpoint.",
    "local": "De lokale GPU-route is niet gebouwd; Mistral vervangt hem.",
}


def carries_patient_audio(mode: str) -> bool:
    """Unknown modes count as carrying patient audio: fail closed."""
    from . import db

    row = db.query_one(
        "SELECT patient_audio FROM recording_types WHERE mode = ?", (mode,))
    return True if row is None else bool(row["patient_audio"])


def allowed_for(mode: str) -> frozenset[str]:
    if not carries_patient_audio(mode):
        return frozenset(PROVIDERS)
    return frozenset(p for p, ok in PROVIDER_PATIENT_AUDIO.items() if ok)


def check(mode: str, route: str) -> None:
    """Raise unless `route` may process a recording made in `mode`."""
    if route in RETIRED:
        raise ApiError("ROUTE_NOT_AVAILABLE", RETIRED[route], status_code=400)
    if route not in PROVIDERS:
        raise ApiError("ROUTE_NOT_AVAILABLE",
                       f"Onbekende route {route!r}", status_code=400)
    if route not in allowed_for(mode):
        raise ApiError(
            "ROUTE_NOT_ALLOWED",
            f"Een opname van het type {mode!r} mag niet naar {route!r}.",
            status_code=400,
        )


def describe() -> list[dict]:
    """What the settings pages show. Derived, never a second copy of the rule."""
    from . import users

    return [{
        "mode": t["mode"],
        "title": t["title"],
        "patient_audio": bool(t["patient_audio"]),
        "allowed": sorted(allowed_for(t["mode"])),
    } for t in users.recording_types()]
