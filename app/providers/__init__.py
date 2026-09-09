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


def get(name: str) -> Provider:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(f"Onbekende provider {name!r}")
    return cls()


def names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["NoteResult", "Provider", "ProviderError", "TranscriptResult",
           "Usage", "get", "names"]
