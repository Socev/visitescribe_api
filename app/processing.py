"""The processing layer: from a confirmed ingest to a draft note.

Deliberately separate from ingest. A session is only ever picked up here after
`ingest_confirmed` is true, and nothing in this module can hold up an upload:
the recorder's path never waits on a provider, a GPU or a network call to a
third party.

Work is a durable queue of jobs in the database rather than in-memory state, so
a restart mid-transcription resumes instead of losing the session. Every job
records what it consumed, whether or not a price is known for it.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from . import audit, db, pricing, routing, sessions, users
from .config import settings
from .errors import ApiError
from .providers import ProviderError, get as get_provider
from .util import now, now_iso, parse_iso

log = logging.getLogger("visitescribe.processing")

STAGES = ("transcribe", "note")
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (30, 120, 600, 1800)


# ---------------------------------------------------------------------------
# queueing
# ---------------------------------------------------------------------------

def enqueue(session_id: str, route: str, *, actor: str = "admin",
            force: bool = False, template_id: str = "",
            template_type: str = "") -> dict[str, Any]:
    """Queue a confirmed session for processing on `route`.

    `template_id` overrides the user's standing rule for this one run, which
    is what the admin panel's picker sets. Kept on the session rather than in
    a new column: it is a property of this queueing, not of the job rows, and
    both jobs of a session share it.
    """
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    if session["state"] == "PURGED":
        raise ApiError("SESSION_PURGED", "Deze sessie is gewist")
    if not session["ingest_confirmed"] and not force:
        raise ApiError(
            "INVALID_REQUEST",
            "Deze sessie is nog niet volledig binnen; verwerken kan pas na "
            "bevestigde ingest.",
        )

    # The recorder said what kind of recording this is; the policy follows from
    # that, not from whoever set the route.
    routing.check(session["mode"], route)

    # Check the credential the job will actually use, which for OurMind is
    # the owner's own token and not a server-wide one. Checking a different
    # credential here than the worker uses would accept jobs that can only
    # fail later, on someone else's behalf.
    owner = users.owner_of_session(session_id)
    token = None
    if owner is not None and route == "ourmind":
        token = users.access_token(owner["user_id"])
    provider = get_provider(route, token=token)
    ready, why = provider.configured()
    if not ready:
        raise ApiError("PROVIDER_NOT_CONFIGURED", why)

    segments = sessions.patient_segments(session_id)
    indices: list[int | None]
    if session["mode"] == "multi_patient" and len(segments) > 1:
        indices = [int(s["index"]) for s in segments]
    else:
        indices = [None]

    ts = now_iso()
    created = 0
    with db.tx() as conn:
        conn.execute(
            "UPDATE sessions SET processing_json = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps({"route": route, "queued_at": ts, "actor": actor,
                         "template_id": template_id,
                         "template_type": template_type or "template"}),
             ts, session_id),
        )
        for index in indices:
            cursor = conn.execute(
                "INSERT INTO processing_jobs(session_id, segment_index, route, stage, "
                "state, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES(?,?,?,?,'queued',0,?,?,?) "
                "ON CONFLICT(session_id, IFNULL(segment_index, -1), stage) DO UPDATE SET "
                "state = 'queued', route = excluded.route, attempts = 0, "
                "next_attempt_at = excluded.next_attempt_at, error = NULL, "
                "error_code = NULL, updated_at = excluded.updated_at",
                (session_id, index, route, "transcribe", ts, ts, ts),
            )
            created += cursor.rowcount or 0
        audit.log("processing", "queued", "success", session_id=session_id,
                  device_id=session["device_id"], identity=actor,
                  detail={"route": route, "segments": len(indices)}, conn=conn)

    sessions.set_state(session_id, "READY_FOR_PROCESSING", ingest_confirmed=True,
                       actor=actor)
    return {"session_id": session_id, "route": route, "jobs": len(indices),
            "segments": indices}


def cancel(session_id: str, *, actor: str = "admin") -> int:
    cursor = db.execute(
        "UPDATE processing_jobs SET state = 'cancelled', updated_at = ? "
        "WHERE session_id = ? AND state IN ('queued','running')",
        (now_iso(), session_id),
    )
    audit.log("processing", "cancelled", "success", session_id=session_id,
              identity=actor, detail={"jobs": cursor.rowcount})
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def _claim() -> dict[str, Any] | None:
    """Take the oldest job that is due. One writer, so a plain update suffices."""
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE state = 'queued' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT 1",
            (now_iso(),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE processing_jobs SET state = 'running', attempts = attempts + 1, "
            "started_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), row["id"]),
        )
        return dict(row)


def _describe(exc: Exception) -> str:
    """The message plus where it came from.

    A bare str(exc) can be untraceable: "sequence item 1: expected a bytes-like
    object, tuple found" came out of h11, three libraries below our own code,
    and said nothing about which call produced it. The deepest frame plus the
    deepest frame inside `app/` turns that into something you can open.

    Only exception text and code locations -- no local variables, so no keys,
    no audio, no patient data.
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    tb = exc.__traceback__
    deepest = ours = None
    while tb is not None:
        frame = tb.tb_frame.f_code
        deepest = (frame.co_filename, tb.tb_lineno, frame.co_name)
        if "/app/" in frame.co_filename.replace("\\", "/"):
            ours = deepest
        tb = tb.tb_next
    for label, loc in (("at", deepest), ("via", ours)):
        if loc and loc != (ours if label == "at" else None):
            fn = loc[0].rsplit("/", 2)[-2:] 
            parts.append(f"{label} {'/'.join(fn)}:{loc[1]} in {loc[2]}()")
    return " | ".join(parts)


def _fail(job: dict[str, Any], exc: Exception, retryable: bool) -> None:
    attempts = int(job["attempts"]) + 1
    give_up = (not retryable) or attempts >= MAX_ATTEMPTS
    if give_up:
        db.execute(
            "UPDATE processing_jobs SET state = 'failed', error = ?, error_code = ?, "
            "finished_at = ?, updated_at = ? WHERE id = ?",
            (_describe(exc)[:2000], getattr(exc, "code", "PROVIDER_FAILED"),
             now_iso(), now_iso(), job["id"]),
        )
        sessions.set_state(
            job["session_id"],
            "PROCESSING_FAILED" if job["stage"] == "note" else "TRANSCRIPTION_FAILED",
            ingest_confirmed=True, error_code=getattr(exc, "code", "PROVIDER_FAILED"),
            error_message=str(exc)[:500], actor="worker",
        )
    else:
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        retry_at = parse_iso(now_iso())
        retry_at = retry_at.timestamp() + delay if retry_at else time.time() + delay
        from datetime import UTC, datetime

        db.execute(
            "UPDATE processing_jobs SET state = 'queued', error = ?, error_code = ?, "
            "next_attempt_at = ?, updated_at = ? WHERE id = ?",
            (_describe(exc)[:2000], getattr(exc, "code", "PROVIDER_FAILED"),
             datetime.fromtimestamp(retry_at, UTC).isoformat().replace("+00:00", "Z"),
             now_iso(), job["id"]),
        )
    audit.log("processing", f"{job['stage']}_failed",
              "failure" if give_up else "retry",
              session_id=job["session_id"], identity="worker",
              detail={"route": job["route"], "attempt": attempts,
                      "segment": job["segment_index"], "error": str(exc)[:400],
                      "final": give_up})


def record_usage(session_id: str, segment_index: int | None, provider: str,
                 model: str | None, operation: str, usage, *,
                 eu_endpoint: bool = False) -> dict[str, Any]:
    """Store what a call consumed, and price it when a rate is known."""
    if operation == "transcribe":
        cost, note = pricing.audio_cost(provider, model, usage.audio_seconds,
                                        eu_endpoint=eu_endpoint)
    else:
        cost, note = pricing.token_cost(provider, model, usage.prompt_tokens,
                                        usage.completion_tokens,
                                        eu_endpoint=eu_endpoint)
    db.execute(
        "INSERT INTO usage_records(session_id, segment_index, provider, model, "
        "operation, audio_seconds, prompt_tokens, completion_tokens, total_tokens, "
        "cost_usd, priced, price_note, raw_usage_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, segment_index, provider, model, operation,
         usage.audio_seconds, usage.prompt_tokens, usage.completion_tokens,
         usage.total_tokens, cost, 1 if cost is not None else 0, note,
         json.dumps(usage.raw or {}, default=str), now_iso()),
    )
    return {"cost_usd": cost, "priced": cost is not None, "note": note}


def _work_dir(session_id: str) -> Path:
    return settings.data_dir / "work" / session_id


def maybe_autostart(session_id: str) -> dict[str, Any] | None:
    """Send a finished recording on its way if its owner asked for that.

    "Standaard ergens heen" is a per-user, per-recording-type setting, not a
    server switch: one doctor may want every consultation to go straight to
    OurMind while meetings wait for a human. It runs on the ingest path, so it
    must never raise -- a recording that is safely stored is safely stored
    even if nothing can be done with it yet.
    """
    if not settings.auto_process:
        return None      # administrator has stopped all outbound processing
    try:
        session = sessions.get(session_id)
        if session is None or not session["ingest_confirmed"]:
            return None
        if db.query_one("SELECT 1 FROM processing_jobs WHERE session_id = ?",
                        (session_id,)) is not None:
            return None
        owner = users.owner_of_session(session_id)
        if owner is None or owner["disabled"]:
            return None
        rule = users.rule(owner["user_id"], session["mode"])
        if not rule or not rule["auto"] or not rule["route"]:
            return None
        return enqueue(session_id, rule["route"], actor="auto")
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-processing %s skipped: %s", session_id, exc)
        audit.log("processing", "autostart_skipped", "failure",
                  session_id=session_id, identity="auto",
                  detail={"reason": str(exc)[:300]})
        return None


def run_job(job: dict[str, Any]) -> None:
    session = sessions.get(job["session_id"])
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Session disappeared")
    routing.check(session["mode"], job["route"])   # re-checked before anything leaves

    # Whose recording is this? The device was bound to a user by an admin, and
    # everything personal follows from that: which OurMind account receives the
    # audio, whose report allowance it spends, and which template makes the
    # report. Without an owner we fall back to the pod-wide credential, which
    # is what a single-user installation has.
    owner = users.owner_of_session(job["session_id"])
    token = None
    if owner is not None and job["route"] == "ourmind":
        # Deliberately not caught: if the user's session has lapsed the job
        # must stop and say so, not quietly go out under someone else's token.
        token = users.access_token(owner["user_id"])

    provider = get_provider(job["route"], token=token)
    ready, why = provider.configured()
    if not ready:
        raise ProviderError(why, code="PROVIDER_NOT_CONFIGURED")

    rule = users.rule(owner["user_id"], session["mode"]) if owner else None
    # A template chosen for this particular run wins over the standing rule.
    try:
        queued = json.loads(session["processing_json"] or "{}")
    except (TypeError, ValueError):
        queued = {}
    template_id = queued.get("template_id") or (rule or {}).get("template_id") or ""
    template_type = (queued.get("template_type") if queued.get("template_id")
                     else (rule or {}).get("template_type")) or "template"

    segment_index = job["segment_index"]
    context = {
        "mode": session["mode"],
        "client_status": session["client_status"],
        "segment_index": segment_index,
        "user_id": owner["user_id"] if owner else None,
        "template_id": template_id,
        "template_type": template_type,
        "language": "nl",
        "privacy_gaps": bool(sessions.privacy_gaps(job["session_id"])),
    }

    if job["stage"] == "transcribe":
        sessions.set_state(job["session_id"], "TRANSCRIBING", ingest_confirmed=True,
                           actor="worker")
        from . import audio as audio_mod

        segments = sessions.patient_segments(job["session_id"])
        bounds = next((s for s in segments if s["index"] == segment_index), None)
        work = _work_dir(job["session_id"])

        def _slice(fmt: str):
            if segment_index is None or bounds is None:
                return audio_mod.build_slice(job["session_id"], work, fmt=fmt)
            return audio_mod.build_slice(
                job["session_id"], work, segment_index=segment_index,
                start_ms=int(bounds["start_ms"]),
                end_ms=int(bounds["end_ms"]) if bounds["end_ms"] is not None else None,
                fmt=fmt,
            )

        # Which container to send. FLAC is what the recorder produced, but a
        # provider may simply refuse it -- OurMind answers "invalid-format"
        # and documents nothing beyond `audio/*`. So the provider names the
        # containers it prefers and we work down the list, re-encoding from
        # the stored chunks each time. The one that is accepted is remembered,
        # so this costs a second upload once and never again.
        formats = list(getattr(provider, "upload_formats", lambda: ("flac",))())
        result = None
        try:
            for index, fmt in enumerate(formats):
                sliced = _slice(fmt)
                context["audio_seconds"] = sliced.seconds
                context["audio_format"] = fmt
                try:
                    result = provider.transcribe(sliced.path, language="nl",
                                                 context=context)
                    break
                except ProviderError as exc:
                    if getattr(exc, "code", "") != "UNSUPPORTED_AUDIO" \
                            or index == len(formats) - 1:
                        raise
                    log.info("%s refused %s (%s); trying %s",
                             job["route"], fmt, exc, formats[index + 1])
                    audit.log("processing", "audio_format_rejected", "retry",
                              session_id=job["session_id"], identity="worker",
                              detail={"provider": job["route"], "rejected": fmt,
                                      "next": formats[index + 1]})
                finally:
                    sliced.path.unlink(missing_ok=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if result is None:
            raise ProviderError(
                f"{job['route']} accepteerde geen van de formaten "
                f"{', '.join(formats)}.", code="UNSUPPORTED_AUDIO")

        if result.usage.audio_seconds is None:
            result.usage.audio_seconds = context.get("audio_seconds")
        priced = record_usage(job["session_id"], segment_index, job["route"],
                              result.model, "transcribe", result.usage,
                              eu_endpoint=getattr(provider, "eu_endpoint", False))

        ts = now_iso()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO transcripts(session_id, segment_index, provider, model, "
                "language, text, segments_json, audio_seconds, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id, IFNULL(segment_index, -1)) DO UPDATE SET "
                "provider = excluded.provider, model = excluded.model, "
                "language = excluded.language, text = excluded.text, "
                "segments_json = excluded.segments_json, "
                "audio_seconds = excluded.audio_seconds, created_at = excluded.created_at",
                (job["session_id"], segment_index, job["route"], result.model,
                 result.language, result.text,
                 json.dumps(result.segments, default=str),
                 result.usage.audio_seconds, ts),
            )
            conn.execute(
                "UPDATE processing_jobs SET state = 'done', finished_at = ?, "
                "updated_at = ?, detail_json = ? WHERE id = ?",
                (ts, ts, json.dumps({"provider_ref": result.provider_ref,
                                     "cost_usd": priced["cost_usd"]}), job["id"]),
            )
            # Chain the note stage, carrying the provider's own reference so a
            # provider that keeps server-side state can pick it up again.
            conn.execute(
                "INSERT INTO processing_jobs(session_id, segment_index, route, stage, "
                "state, attempts, next_attempt_at, created_at, updated_at, detail_json) "
                "VALUES(?,?,?,'note','queued',0,?,?,?,?) "
                "ON CONFLICT(session_id, IFNULL(segment_index, -1), stage) DO UPDATE SET "
                "state = 'queued', attempts = 0, error = NULL, error_code = NULL, "
                "next_attempt_at = excluded.next_attempt_at, "
                "detail_json = excluded.detail_json, updated_at = excluded.updated_at",
                (job["session_id"], segment_index, job["route"], ts, ts, ts,
                 json.dumps({"provider_ref": result.provider_ref})),
            )
            audit.log("processing", "transcribed", "success",
                      session_id=job["session_id"], identity="worker",
                      detail={"route": job["route"], "segment": segment_index,
                              "model": result.model,
                              "audio_seconds": result.usage.audio_seconds,
                              "characters": len(result.text),
                              "cost_usd": priced["cost_usd"]}, conn=conn)
        return

    # -- note ------------------------------------------------------------
    row = db.query_one(
        "SELECT * FROM transcripts WHERE session_id = ? "
        "AND IFNULL(segment_index, -1) = IFNULL(?, -1)",
        (job["session_id"], segment_index),
    )
    if row is None:
        raise ProviderError("Geen transcript om een verslag van te maken")
    from .providers.base import TranscriptResult, Usage

    try:
        detail = json.loads(job.get("detail_json") or "{}")
    except (ValueError, TypeError):
        detail = {}
    transcript = TranscriptResult(
        text=row["text"], language=row["language"], model=row["model"],
        segments=json.loads(row["segments_json"] or "[]"), usage=Usage(),
        provider_ref=detail.get("provider_ref"),
    )
    sessions.set_state(job["session_id"], "PROCESSING", ingest_confirmed=True,
                       actor="worker")
    note = provider.make_note(transcript, context=context)
    priced = record_usage(job["session_id"], segment_index, job["route"],
                          note.model, "note", note.usage,
                          eu_endpoint=getattr(provider, "eu_endpoint", False))

    ts = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO notes(session_id, segment_index, provider, model, template, "
            "title, body, codes_json, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'draft',?,?) "
            "ON CONFLICT(session_id, IFNULL(segment_index, -1)) DO UPDATE SET "
            "provider = excluded.provider, model = excluded.model, "
            "template = excluded.template, title = excluded.title, "
            "body = excluded.body, codes_json = excluded.codes_json, "
            "status = 'draft', updated_at = excluded.updated_at",
            (job["session_id"], segment_index, job["route"], note.model, note.template,
             note.title, note.body, json.dumps(note.codes, default=str), ts, ts),
        )
        conn.execute(
            "UPDATE processing_jobs SET state = 'done', finished_at = ?, updated_at = ?, "
            "detail_json = ? WHERE id = ?",
            (ts, ts, json.dumps({"cost_usd": priced["cost_usd"]}), job["id"]),
        )
        audit.log("processing", "note_generated", "success",
                  session_id=job["session_id"], identity="worker",
                  detail={"route": job["route"], "segment": segment_index,
                          "model": note.model, "characters": len(note.body),
                          "codes": len(note.codes),
                          "cost_usd": priced["cost_usd"]}, conn=conn)

    if not _outstanding(job["session_id"]):
        sessions.set_state(job["session_id"], "REVIEW_REQUIRED", ingest_confirmed=True,
                           actor="worker")


def _outstanding(session_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM processing_jobs WHERE session_id = ? "
        "AND state IN ('queued','running')", (session_id,))
    return int(row["n"]) if row else 0


def run_once() -> bool:
    """Run at most one due job. Returns True if it did any work."""
    job = _claim()
    if job is None:
        return False
    try:
        run_job(job)
    except ProviderError as exc:
        _fail(job, exc, retryable=exc.retryable)
    except ApiError as exc:
        _fail(job, exc, retryable=False)
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s crashed", job["id"])
        _fail(job, exc, retryable=True)
    return True


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def jobs_for(session_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM processing_jobs WHERE session_id = ? ORDER BY id", (session_id,))]


def results_for(session_id: str) -> dict[str, Any]:
    transcripts = [dict(r) for r in db.query(
        "SELECT * FROM transcripts WHERE session_id = ? "
        "ORDER BY IFNULL(segment_index, 0)", (session_id,))]
    for item in transcripts:
        try:
            item["segments"] = json.loads(item.pop("segments_json") or "[]")
        except (ValueError, TypeError):
            item["segments"] = []
    notes = [dict(r) for r in db.query(
        "SELECT * FROM notes WHERE session_id = ? ORDER BY IFNULL(segment_index, 0)",
        (session_id,))]
    for item in notes:
        try:
            item["codes"] = json.loads(item.pop("codes_json") or "[]")
        except (ValueError, TypeError):
            item["codes"] = []
    return {"transcripts": transcripts, "notes": notes,
            "jobs": jobs_for(session_id), "usage": usage_for(session_id)}


def usage_for(session_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM usage_records WHERE session_id = ? ORDER BY id", (session_id,))]


def cost_summary(since: str | None = None) -> dict[str, Any]:
    """Totals for the admin. Unpriced calls are counted separately rather than
    folded in as zero — an unknown cost must not look like a free one."""
    clause = "WHERE created_at >= ?" if since else ""
    params = (since,) if since else ()
    rows = db.query(
        f"SELECT provider, operation, COUNT(*) AS calls, "
        f"COALESCE(SUM(audio_seconds),0) AS audio_seconds, "
        f"COALESCE(SUM(total_tokens),0) AS tokens, "
        f"COALESCE(SUM(CASE WHEN priced = 1 THEN cost_usd ELSE 0 END),0) AS cost_usd, "
        f"SUM(CASE WHEN priced = 0 THEN 1 ELSE 0 END) AS unpriced "
        f"FROM usage_records {clause} GROUP BY provider, operation "
        f"ORDER BY provider, operation", params)
    lines = [dict(r) for r in rows]
    return {
        "since": since,
        "lines": lines,
        "total_cost_usd": round(sum(line["cost_usd"] for line in lines), 4),
        "unpriced_calls": sum(int(line["unpriced"] or 0) for line in lines),
        "total_audio_seconds": round(
            sum(line["audio_seconds"] for line in lines), 1),
        "total_audio_hours": round(
            sum(line["audio_seconds"] for line in lines) / 3600.0, 4),
    }


def queue_overview() -> dict[str, Any]:
    rows = db.query(
        "SELECT state, COUNT(*) AS n FROM processing_jobs GROUP BY state")
    return {r["state"]: int(r["n"]) for r in rows}
