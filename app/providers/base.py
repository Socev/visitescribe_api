from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ProviderError(Exception):
    """A provider could not complete the call. Message is shown to the admin."""

    def __init__(self, message: str, *, retryable: bool = False,
                 code: str = "PROVIDER_FAILED", detail: dict | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.detail = detail or {}


@dataclass
class Usage:
    """What one call consumed. Whatever the provider does not report stays
    None, so an unknown quantity is never silently recorded as zero."""
    audio_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TranscriptResult:
    text: str
    language: str | None = None
    model: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider_ref: str | None = None


@dataclass
class NoteResult:
    body: str
    title: str | None = None
    model: str | None = None
    template: str | None = None
    codes: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider_ref: str | None = None


class Provider(Protocol):
    name: str

    def configured(self) -> tuple[bool, str]:
        """(ready, human-readable reason when not ready)."""
        ...

    def transcribe(self, audio: Path, *, language: str | None,
                   context: dict[str, Any]) -> TranscriptResult: ...

    def make_note(self, transcript: TranscriptResult, *,
                  context: dict[str, Any]) -> NoteResult: ...
