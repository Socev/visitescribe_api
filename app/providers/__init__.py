"""Processing providers.

Each provider turns audio into a transcript, and a transcript into a note. What
they have in common is the result shape and, importantly, that every call
reports what it consumed so the cost can be recorded rather than estimated.
"""
from __future__ import annotations

from .base import NoteResult, Provider, ProviderError, TranscriptResult, Usage
from .mistral import MistralProvider
from .ourmind import OurMindProvider

_REGISTRY: dict[str, type[Provider]] = {
    "mistral": MistralProvider,
    "ourmind": OurMindProvider,
}


def get(name: str, *, token: str | None = None) -> Provider:
    """A provider client, optionally acting for one specific user.

    `token` is the signed-in user's own OurMind credential. Providers that do
    not take one ignore it, so callers do not have to know which is which.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(f"Onbekende provider {name!r}")
    try:
        return cls(token=token) if token else cls()
    except TypeError:
        return cls()


def names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["NoteResult", "Provider", "ProviderError", "TranscriptResult",
           "Usage", "get", "names"]
