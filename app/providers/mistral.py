"""Mistral: Voxtral for transcription, a chat model for the note.

Chosen because FLAC is on Mistral's documented list of accepted formats, so the
recorder's audio goes out exactly as it was stored — no transcode, no quality
loss, no extra failure mode. The API is served from EU data centres by default,
with a regional endpoint available when a contractual guarantee is needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from .base import NoteResult, ProviderError, TranscriptResult, Usage
from . import credentials

DEFAULT_BASE = "https://api.mistral.ai"
EU_BASE = "https://api.eu.mistral.ai"

# Pinned rather than "-latest": the previous Voxtral transcription model was
# retired with about three months' notice, and a medical pipeline should not
# swap models underneath itself.
DEFAULT_ASR_MODEL = "voxtral-mini-2602"
DEFAULT_NOTE_MODEL = "mistral-medium-3.5"

SOEP_PROMPT = """Je bent een ervaren Nederlandse huisarts die een consultverslag \
schrijft op basis van een letterlijk transcript van het consult.

Schrijf het verslag in SOEP-structuur, in het Nederlands:

S (Subjectief) — wat de patiënt vertelt: klacht, beloop, context, ongerustheid.
O (Objectief) — wat is waargenomen of gemeten: lichamelijk onderzoek, metingen.
E (Evaluatie) — je beoordeling: werkdiagnose, differentiaal waar relevant.
P (Plan) — beleid: behandeling, medicatie, controle, verwijzing, vangnetadvies.

Regels die je strikt volgt:
- Neem uitsluitend op wat in het transcript staat. Verzin niets, ook geen \
metingen, doseringen of bevindingen die niet zijn uitgesproken.
- Staat een rubriek niet in het gesprek, schrijf dan "niet besproken" in \
plaats van iets aannemelijks.
- Spraakherkenning maakt fouten. Herken je een verhaspeld medisch woord, \
schrijf dan de meest waarschijnlijke term en zet die tussen vierkante haken, \
bijvoorbeeld [amoxicilline?].
- Schrijf bondig en in de derde persoon. Geen aanhef, geen afsluiting.
- Geef alleen het verslag terug, zonder inleiding of toelichting."""


class MistralProvider:
    name = "mistral"

    def __init__(self) -> None:
        self.api_key = credentials.secret("mistral")
        self.eu_endpoint = (os.environ.get("VS_MISTRAL_EU_ENDPOINT", "") or "").lower() in (
            "1", "true", "yes", "on")
        self.base = (os.environ.get("VS_MISTRAL_BASE_URL")
                     or (EU_BASE if self.eu_endpoint else DEFAULT_BASE)).rstrip("/")
        self.asr_model = os.environ.get("VS_MISTRAL_ASR_MODEL") or DEFAULT_ASR_MODEL
        self.note_model = os.environ.get("VS_MISTRAL_NOTE_MODEL") or DEFAULT_NOTE_MODEL
        self.timeout = float(os.environ.get("VS_MISTRAL_TIMEOUT", "900"))

    def configured(self) -> tuple[bool, str]:
        if not self.api_key:
            return (False, "Geen Mistral API-sleutel ingesteld")
        return (True, "")

    # -- transcription ---------------------------------------------------
    def transcribe(self, audio: Path, *, language: str | None,
                   context: dict[str, Any]) -> TranscriptResult:
        ok, why = self.configured()
        if not ok:
            raise ProviderError(why, code="PROVIDER_NOT_CONFIGURED")

        data: list[tuple[str, str]] = [
            ("model", self.asr_model),
            ("diarize", "true"),
        ]
        # Mistral documents timestamp_granularities as incompatible with
        # `language`, so this picks one: an explicit language is worth more for
        # a known-Dutch recording than segment timings are.
        if language:
            data.append(("language", language))
        else:
            data.append(("timestamp_granularities", "segment"))
        for term in context.get("context_bias") or []:
            data.append(("context_bias", str(term)))

        try:
            with audio.open("rb") as handle:
                response = httpx.post(
                    f"{self.base}/v1/audio/transcriptions",
                    headers={"x-api-key": self.api_key},
                    data=data,
                    files={"file": (audio.name, handle, "audio/flac")},
                    timeout=self.timeout,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Mistral niet bereikbaar: {exc}", retryable=True) from exc

        payload = self._payload(response)
        usage = payload.get("usage") or {}
        return TranscriptResult(
            text=(payload.get("text") or "").strip(),
            language=payload.get("language") or language,
            model=payload.get("model") or self.asr_model,
            segments=payload.get("segments") or [],
            usage=Usage(
                audio_seconds=_num(usage.get("prompt_audio_seconds")),
                prompt_tokens=_int(usage.get("prompt_tokens")),
                completion_tokens=_int(usage.get("completion_tokens")),
                total_tokens=_int(usage.get("total_tokens")),
                raw=usage,
            ),
        )

    # -- note ------------------------------------------------------------
    def make_note(self, transcript: TranscriptResult, *,
                  context: dict[str, Any]) -> NoteResult:
        ok, why = self.configured()
        if not ok:
            raise ProviderError(why, code="PROVIDER_NOT_CONFIGURED")
        if not transcript.text.strip():
            raise ProviderError("Leeg transcript; geen verslag gemaakt")

        mode = context.get("mode", "single_patient")
        system = SOEP_PROMPT if mode != "meeting" else _MEETING_PROMPT
        body = {
            "model": self.note_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _user_message(transcript, context)},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=body, timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Mistral niet bereikbaar: {exc}", retryable=True) from exc

        payload = self._payload(response)
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("Mistral gaf geen verslag terug")
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return NoteResult(
            body=text.strip(),
            title=None,
            model=payload.get("model") or self.note_model,
            template="SOEP" if mode != "meeting" else "vergaderverslag",
            usage=Usage(
                prompt_tokens=_int(usage.get("prompt_tokens")),
                completion_tokens=_int(usage.get("completion_tokens")),
                total_tokens=_int(usage.get("total_tokens")),
                raw=usage,
            ),
        )

    # -- shared ----------------------------------------------------------
    @staticmethod
    def _payload(response: httpx.Response) -> dict:
        if response.status_code == 429:
            raise ProviderError("Mistral rate limit bereikt", retryable=True)
        if response.status_code >= 500:
            raise ProviderError(f"Mistral serverfout {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise ProviderError(
                f"Mistral weigerde het verzoek ({response.status_code}): "
                f"{response.text[:400]}"
            )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Onleesbaar antwoord van Mistral: {exc}") from exc


_MEETING_PROMPT = """Je maakt een verslag van een vergadering op basis van een \
letterlijk transcript.

Lever in het Nederlands:
1. Een korte samenvatting van waar het overleg over ging.
2. De besluiten, elk als losse regel.
3. De actiepunten, met wie het oppakt als dat genoemd is.
4. Openstaande punten die zijn doorgeschoven.

Neem alleen op wat is uitgesproken. Is een rubriek leeg, schrijf dan dat er \
niets over is besloten. Geef alleen het verslag terug."""


def _user_message(transcript: TranscriptResult, context: dict[str, Any]) -> str:
    head = []
    if context.get("segment_index"):
        head.append(f"Dit is patiëntsegment {context['segment_index']} van een "
                    f"opname met meerdere patiënten.")
    if context.get("client_status") == "interrupted":
        head.append("Let op: de opname is onderbroken geweest; het einde kan "
                    "ontbreken.")
    if context.get("privacy_gaps"):
        head.append("Tijdens de opname zijn privacypauzes gebruikt; er zitten "
                    "gaten in de tijdlijn.")
    prefix = (" ".join(head) + "\n\n") if head else ""
    return f"{prefix}Transcript:\n\n{transcript.text}"


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
