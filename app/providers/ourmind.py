"""OurMind: a Dutch clinical documentation service that does the whole job.

Unlike the Mistral route this is not transcription plus a prompt of our own —
OurMind produces the SOEP report and the ICPC-NHG-24 codes itself. One
consultation carries at most one patient, which lines up exactly with the
patient segments the recorder already reports.

Two things are worth knowing before reading the code:

* **Authentication.** The only non-interactive login documented is for named
  partners (Medicom, ChipSoft, Advitronics), each with an integration token
  issued by OurMind. Everyone else signs in with an emailed one-time code and
  gets a Supabase JWT. So this provider does not log in; it uses whatever token
  is in the credential store, and says so plainly when there is none. Token
  lifetime is not documented, hence the explicit re-auth error.
* **Audio format.** The API documents the upload body only as `audio/*` — no
  codec list, no size or duration limit. FLAC is sent as-is because that is
  what we store; if OurMind rejects it the error is surfaced verbatim rather
  than guessed at.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from .base import NoteResult, ProviderError, TranscriptResult, Usage
from . import credentials

DEFAULT_BASE = "https://api.ourmind.ai"
DEFAULT_VERSION = "2025-05-07"
JSON_API = "application/vnd.api+json"

TERMINAL_OK = "done"
TERMINAL_BAD = ("failed", "timed_out")


class OurMindProvider:
    name = "ourmind"

    def __init__(self, token: str | None = None) -> None:
        # A per-user token when we have one, so a recording lands in that
        # doctor's own OurMind account and counts against their own report
        # allowance. The stored server-wide credential is only a fallback for
        # a single-user installation.
        self.token = token or credentials.secret("ourmind")
        self.base = (os.environ.get("VS_OURMIND_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.version = os.environ.get("VS_OURMIND_API_VERSION") or DEFAULT_VERSION
        self.timeout = float(os.environ.get("VS_OURMIND_TIMEOUT", "120"))
        self.poll_seconds = float(os.environ.get("VS_OURMIND_POLL_SECONDS", "3"))
        self.poll_budget = float(os.environ.get("VS_OURMIND_POLL_BUDGET", "900"))
        self.language = os.environ.get("VS_OURMIND_LANGUAGE") or "nl-NL"
        self.template_id = os.environ.get("VS_OURMIND_TEMPLATE_ID") or ""
        self.delete_after = (os.environ.get("VS_OURMIND_DELETE_AFTER", "true") or "").lower() \
            in ("1", "true", "yes", "on")

    # -- plumbing --------------------------------------------------------
    def configured(self) -> tuple[bool, str]:
        if not self.token:
            return (False, "Niet ingelogd bij OurMind. Log in op de gebruikerssite "
                           "met de code die je per e-mail krijgt.")
        return (True, "")

    # -- who this token belongs to, and what it may use ------------------
    def me(self) -> dict[str, Any]:
        """The doctor behind this token: name, organisation, report quota.

        OurMind's identifier for a doctor IS their e-mail address, so this is
        also how a token is tied to a user in our own database.
        """
        payload = self._call("GET", "me") or {}
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        org = attrs.get("org") or {}
        return {
            "email": attrs.get("email") or data.get("id") or "",
            "name": attrs.get("name") or "",
            "org_name": (org or {}).get("name") or "",
            "plan": attrs.get("plan") or "",
            "language": attrs.get("language") or "",
            "monthly_reports": attrs.get("monthly_reports"),
            "reports_left": attrs.get("reports_left"),
            "default_template": attrs.get("template") or None,
        }

    def templates(self) -> list[dict[str, Any]]:
        """Every template this doctor can use: system, personal and shared.

        `/me/templates/all` rather than `/templates`, which is system-only and
        would silently hide the practice's own templates.
        """
        payload = self._call("GET", "me/templates/all") or {}
        out = []
        for item in payload.get("data") or []:
            attrs = item.get("attributes") or {}
            if attrs.get("available") is False:
                continue
            out.append({
                # A string here and an integer in the generate body. Their
                # spec really is asymmetric; the cast happens at use.
                "id": str(item.get("id") or ""),
                "type": item.get("type") or "template",
                "title": attrs.get("title") or "(zonder titel)",
                "language": attrs.get("language") or "",
                "slug": attrs.get("slug") or "",
                "has_codes": bool(attrs.get("has_codes")),
            })
        out.sort(key=lambda t: (t["title"] or "").lower())
        return out

    def _url(self, path: str) -> str:
        return f"{self.base}/{self.version}/{path.lstrip('/')}"

    def _call(self, method: str, path: str, *, json_body: dict | None = None,
              content: Any = None, content_type: str | None = None,
              timeout: float | None = None) -> Any:
        # Accept-Language decides the language of the report. Dutch is NOT the
        # system default -- it is what you get by asking for it. Verified
        # against the documentation after an earlier claim to the contrary.
        headers = {"Authorization": f"Bearer {self.token}", "Accept": JSON_API,
                   "Accept-Language": self.language}
        if json_body is not None:
            headers["Content-Type"] = JSON_API
        elif content_type:
            headers["Content-Type"] = content_type
        try:
            response = httpx.request(
                method, self._url(path), headers=headers, json=json_body,
                content=content, timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"OurMind niet bereikbaar: {exc}", retryable=True) from exc

        if response.status_code == 401:
            raise ProviderError(
                "OurMind wees het token af. Log opnieuw in met de e-mailcode.",
                code="PROVIDER_NOT_CONFIGURED",
            )
        if response.status_code >= 500:
            raise ProviderError(f"OurMind serverfout {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise ProviderError(_explain(response))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"Onleesbaar antwoord van OurMind: {exc}") from exc

    # -- quota -----------------------------------------------------------
    def quota(self) -> dict[str, Any]:
        """OurMind counts a monthly allowance of reports, not minutes."""
        payload = self._call("GET", "me") or {}
        attrs = ((payload.get("data") or {}).get("attributes") or {})
        allowance = attrs.get("monthly_reports")
        left = attrs.get("reports_left")
        used = None
        if isinstance(allowance, int) and isinstance(left, int):
            used = allowance - left
        return {"plan": attrs.get("plan"), "monthly_reports": allowance,
                "reports_left": left, "reports_used": used,
                "language": attrs.get("language"), "ephemeral": attrs.get("ephemeral")}

    # -- the documented nine-step flow -----------------------------------
    def transcribe(self, audio: Path, *, language: str | None,
                   context: dict[str, Any]) -> TranscriptResult:
        ok, why = self.configured()
        if not ok:
            raise ProviderError(why, code="PROVIDER_NOT_CONFIGURED")

        consultation = _id(self._call("POST", "consultations"))
        try:
            file_id = _id(self._call(
                "POST", f"consultation/{consultation}/files",
                json_body={"data": {"type": "file",
                                    "attributes": {"name": audio.name[:140]}}},
            ))
            # An open handle, not read_bytes(): httpx streams it and still
            # sends a real Content-Length, so a 45-minute consultation never
            # sits in memory whole -- which is the entire point of the
            # streaming reassembly in app/audio.py, and would be undone here.
            with audio.open("rb") as handle:
                self._call("PATCH", f"consultation/{consultation}/file/{file_id}",
                           content=handle, content_type="audio/flac",
                           timeout=max(self.timeout, 600))
            self._call("POST", f"consultation/{consultation}/file/{file_id}/seal")

            transcript = self._await_transcript(consultation)
        except ProviderError:
            self._discard(consultation)
            raise

        text = "\n".join(
            (seg.get("text") or "").strip()
            for seg in transcript.get("segments") or []
        ).strip()
        return TranscriptResult(
            text=text,
            language=context.get("language") or language,
            model="ourmind",
            segments=transcript.get("segments") or [],
            usage=Usage(audio_seconds=context.get("audio_seconds")),
            provider_ref=consultation,
        )

    def make_note(self, transcript: TranscriptResult, *,
                  context: dict[str, Any]) -> NoteResult:
        consultation = transcript.provider_ref
        if not consultation:
            raise ProviderError("Geen OurMind-consult gekoppeld aan dit transcript")

        body: dict[str, Any] = {"data": {"type": "generate_reports",
                                         "attributes": {"safe_mode": False}}}
        # The template the user chose for this KIND of recording, falling back
        # to the pod-wide default and then to whatever OurMind has set as the
        # doctor's own default. Note the asymmetry in their API: the listing
        # gives ids as strings ("13"), the generate body wants an integer.
        chosen_id = str(context.get("template_id") or self.template_id or "")
        chosen_type = context.get("template_type") or "template"
        if chosen_id:
            if chosen_type == "shared_template":
                # Their generate endpoint only accepts template|doctor_template.
                # How to generate directly from a shared template is not
                # documented, so rather than guess at an id that means
                # something else, say so.
                raise ProviderError(
                    "Dit is een gedeeld template. OurMind accepteert bij het "
                    "genereren alleen eigen of systeemtemplates; kloon het "
                    "eerst in OurMind en kies daarna de kopie.")
            try:
                body["data"]["attributes"]["template"] = {
                    "id": int(chosen_id), "type": chosen_type}
            except (TypeError, ValueError):
                raise ProviderError(f"Ongeldig template-id {chosen_id!r}") from None

        self._call("POST", f"consultation/{consultation}/reports/generate",
                   json_body=body)
        report = self._await_report(consultation)
        sections = self._call(
            "GET", f"consultation/{consultation}/report/{report['id']}/sections") or {}
        parts = []
        for item in sections.get("data") or []:
            attrs = item.get("attributes") or {}
            template = attrs.get("section_template") or {}
            parts.append((int(template.get("position") or 0),
                          template.get("title") or "",
                          (attrs.get("text") or "").strip()))
        parts.sort(key=lambda x: x[0])
        text = "\n\n".join(
            (f"{title}\n{body_}" if title else body_) for _, title, body_ in parts if body_
        )

        attrs = report.get("attributes") or {}
        note = NoteResult(
            body=text,
            title=attrs.get("title"),
            model="ourmind",
            template=str(attrs.get("template_id") or chosen_id or ""),
            codes=attrs.get("codes") or [],
            # OurMind bills a monthly report allowance rather than per call;
            # the consumed count is read separately from GET /me.
            usage=Usage(raw={"reports": 1}),
            provider_ref=consultation,
        )
        if self.delete_after:
            self._discard(consultation)
        return note

    # -- polling ---------------------------------------------------------
    def _await_transcript(self, consultation: str) -> dict:
        deadline = time.monotonic() + self.poll_budget
        while True:
            payload = self._call("GET", f"consultation/{consultation}/transcripts") or {}
            items = payload.get("data") or []
            states = [(i.get("attributes") or {}).get("status") for i in items]
            if items and all(s == TERMINAL_OK for s in states):
                merged: list[dict] = []
                for item in items:
                    merged.extend((item.get("attributes") or {}).get("segments") or [])
                return {"segments": merged}
            if any(s in TERMINAL_BAD for s in states):
                raise ProviderError("OurMind kon de opname niet transcriberen")
            if time.monotonic() > deadline:
                raise ProviderError("OurMind transcriptie duurde te lang", retryable=True)
            time.sleep(self.poll_seconds)

    def _await_report(self, consultation: str) -> dict:
        deadline = time.monotonic() + self.poll_budget
        while True:
            payload = self._call("GET", f"consultation/{consultation}/reports") or {}
            items = payload.get("data") or []
            if items:
                latest = max(items, key=lambda i: int(
                    (i.get("attributes") or {}).get("generation") or 0))
                status = (latest.get("attributes") or {}).get("status")
                if status == TERMINAL_OK:
                    return latest
                if status in TERMINAL_BAD:
                    raise ProviderError(f"OurMind rapportgeneratie {status}")
            if time.monotonic() > deadline:
                raise ProviderError("OurMind rapport duurde te lang", retryable=True)
            time.sleep(self.poll_seconds)

    def _discard(self, consultation: str) -> None:
        """Best effort: a consultation left behind expires after 72 hours anyway."""
        try:
            self._call("DELETE", f"consultation/{consultation}")
        except ProviderError:
            pass


def _id(payload: Any) -> str:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("id"):
        raise ProviderError("OurMind gaf geen id terug")
    return str(data["id"])


def _explain(response: httpx.Response) -> str:
    try:
        errors = response.json().get("errors") or []
        if errors:
            first = errors[0]
            code = first.get("code")
            detail = first.get("detail") or "onbekende fout"
            return f"OurMind: {detail}" + (f" ({code})" if code else "")
    except ValueError:
        pass
    return f"OurMind gaf {response.status_code}: {response.text[:300]}"
