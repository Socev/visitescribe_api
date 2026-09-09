"""What each provider call costs.

Prices are data, not code: they change, and a wrong number that silently looks
right is worse than no number. Every rate below carries its source and the date
it was read, and anything unpriced is recorded as `priced = 0` rather than
guessed at zero — an unpriced call must show up as unknown in the totals, never
as free.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    unit: str            # "audio_minute" | "token"
    amount: float        # USD per unit
    source: str
    read_on: str


# docs.mistral.ai/models/pricing, read 2026-09-09. Audio is billed per minute
# of audio, not per token — the token counts in the response are informational
# and Mistral's own examples are inconsistent about them.
MISTRAL_AUDIO: dict[str, Rate] = {
    "voxtral-mini-2602": Rate("audio_minute", 0.003, "docs.mistral.ai/models/pricing", "2026-09-09"),
    "voxtral-mini-transcribe-realtime-2602": Rate("audio_minute", 0.006, "docs.mistral.ai/models/pricing", "2026-09-09"),
    "voxtral-small-2507": Rate("audio_minute", 0.004, "docs.mistral.ai/models/pricing", "2026-09-09"),
}

# USD per million tokens (input, output).
MISTRAL_TEXT: dict[str, tuple[float, float]] = {
    "mistral-large-3": (0.5, 1.5),
    "mistral-medium-3.5": (1.5, 7.5),
    "mistral-small-4": (0.15, 0.6),
    "ministral-3-14b": (0.2, 0.2),
    "ministral-3-8b": (0.15, 0.15),
    "ministral-3-3b": (0.1, 0.1),
}

# The EU regional endpoint is documented as 1.1x list price for tokens. Whether
# the uplift also applies to the per-minute audio price is not documented, so
# it is applied and flagged rather than assumed away.
EU_ENDPOINT_MULTIPLIER = 1.1

_ALIAS = {
    "voxtral-mini-latest": "voxtral-mini-2602",
    "voxtral-mini-transcribe-realtime-latest": "voxtral-mini-transcribe-realtime-2602",
    "voxtral-small-latest": "voxtral-small-2507",
    "mistral-large-latest": "mistral-large-3",
    "mistral-medium-latest": "mistral-medium-3.5",
    "mistral-small-latest": "mistral-small-4",
}


def _overrides() -> dict:
    raw = os.environ.get("VS_PRICE_OVERRIDES", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def canonical(model: str | None) -> str:
    if not model:
        return ""
    return _ALIAS.get(model, model)


def audio_cost(provider: str, model: str | None, seconds: float | None,
               eu_endpoint: bool = False) -> tuple[float | None, str]:
    """USD for `seconds` of audio, plus a note explaining the number."""
    if provider == "ourmind":
        return (0.0, "included in the OurMind subscription; counted in reports, not minutes")
    if seconds is None:
        return (None, "provider returned no audio duration")
    key = canonical(model)
    override = _overrides().get(f"{provider}:{key}:audio_minute")
    if isinstance(override, (int, float)):
        return (seconds / 60.0 * float(override), f"override {override}/min")
    rate = MISTRAL_AUDIO.get(key) if provider == "mistral" else None
    if rate is None:
        return (None, f"no published rate for {provider}:{model}")
    amount = seconds / 60.0 * rate.amount
    note = f"{rate.amount}/min · {rate.source} · gelezen {rate.read_on}"
    if eu_endpoint:
        amount *= EU_ENDPOINT_MULTIPLIER
        note += f" · EU-endpoint x{EU_ENDPOINT_MULTIPLIER} (uplift op audio niet gedocumenteerd)"
    return (amount, note)


def token_cost(provider: str, model: str | None, prompt_tokens: int | None,
               completion_tokens: int | None, eu_endpoint: bool = False
               ) -> tuple[float | None, str]:
    if provider == "ourmind":
        return (0.0, "included in the OurMind subscription")
    key = canonical(model)
    rates = MISTRAL_TEXT.get(key) if provider == "mistral" else None
    if rates is None:
        return (None, f"no published rate for {provider}:{model}")
    if prompt_tokens is None and completion_tokens is None:
        return (None, "provider returned no token counts")
    amount = ((prompt_tokens or 0) * rates[0] + (completion_tokens or 0) * rates[1]) / 1_000_000
    note = f"in {rates[0]}/M · uit {rates[1]}/M · docs.mistral.ai/models/pricing"
    if eu_endpoint:
        amount *= EU_ENDPOINT_MULTIPLIER
        note += f" · EU-endpoint x{EU_ENDPOINT_MULTIPLIER}"
    return (amount, note)


def known_models() -> dict[str, list[str]]:
    return {
        "mistral_audio": sorted(MISTRAL_AUDIO),
        "mistral_text": sorted(MISTRAL_TEXT),
    }
