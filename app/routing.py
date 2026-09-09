"""Which providers a session is allowed to reach.

The recorder already states what kind of recording it made, so the policy is
derived from that rather than trusted to whoever sets the route. A consultation
cannot be sent to a provider that is only approved for meetings, and the check
runs both when the route is set and again in the worker before anything leaves
the machine.
"""
from __future__ import annotations

from .errors import ApiError

# Providers that can actually run a session end to end.
PROVIDERS = ("mistral", "ourmind")

# mode -> providers approved for that kind of recording.
POLICY: dict[str, frozenset[str]] = {
    "single_patient": frozenset({"mistral", "ourmind"}),
    "multi_patient": frozenset({"mistral", "ourmind"}),
    "meeting": frozenset({"mistral"}),
}

# Providers that were considered and deliberately not built, kept here so a
# stale route in an old session gives a real explanation instead of "unknown".
RETIRED = {
    "plaud": "Plaud is niet gekoppeld: transcriptie zonder samenvatting, "
             "standaard buiten de EU, en geen verwijder-endpoint.",
    "local": "De lokale GPU-route is niet gebouwd; Mistral vervangt hem.",
}


def allowed_for(mode: str) -> frozenset[str]:
    return POLICY.get(mode, frozenset())


def check(mode: str, route: str) -> None:
    """Raise unless `route` may process a recording made in `mode`."""
    if route in RETIRED:
        raise ApiError("ROUTE_NOT_AVAILABLE", RETIRED[route], status_code=400)
    if route not in PROVIDERS:
        raise ApiError(
            "ROUTE_NOT_AVAILABLE",
            f"Onbekende route {route!r}; beschikbaar: {', '.join(PROVIDERS)}",
            status_code=400,
        )
    permitted = allowed_for(mode)
    if not permitted:
        raise ApiError("ROUTE_NOT_ALLOWED", f"Onbekende opnamemodus {mode!r}", status_code=400)
    if route not in permitted:
        raise ApiError(
            "ROUTE_NOT_ALLOWED",
            f"Een opname van het type {mode!r} mag niet naar {route!r}. "
            f"Toegestaan: {', '.join(sorted(permitted))}.",
            status_code=403,
        )


def describe() -> list[dict]:
    return [
        {"mode": mode, "allowed": sorted(providers)}
        for mode, providers in POLICY.items()
    ]
