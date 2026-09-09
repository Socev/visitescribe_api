"""Session state, completeness evaluation and patient segmentation."""
from __future__ import annotations

import json
from typing import Any

from . import audit, db
from .config import DURABLE_STATES

# States an administrator sets deliberately to stop a session going further.
# Automatic re-evaluation must not quietly undo them; ingest_confirmed is still
# kept accurate underneath, so the recorder's contract is unaffected.
ADMIN_STICKY_STATES = frozenset({
    "ERROR", "POLICY_BLOCKED", "TRANSCRIPTION_FAILED", "PROCESSING_FAILED",
    "BOUNDARY_REVIEW_REQUIRED", "PURGED",
})
from .util import now_iso


def get(session_id: str) -> dict[str, Any] | None:
    return db.row_to_dict(
        db.query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    )


def manifest_sequences(session_id: str) -> list[int]:
    return [
        int(r["sequence"])
        for r in db.query(
            "SELECT sequence FROM manifest_chunks WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        )
    ]


def received_sequences(session_id: str) -> list[int]:
    return [
        int(r["sequence"])
        for r in db.query(
            "SELECT sequence FROM chunks WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        )
    ]


def verified_count(session_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ? AND ciphertext_verified = 1 "
        "AND decrypt_verified = 1 AND plaintext_verified = 1 AND flac_valid = 1",
        (session_id,),
    )
    return int(row["n"]) if row else 0


def missing_chunks(session_id: str) -> list[int]:
    expected = set(manifest_sequences(session_id))
    got = set(received_sequences(session_id))
    return sorted(expected - got)


def unverified_chunks(session_id: str) -> list[int]:
    return [
        int(r["sequence"])
        for r in db.query(
            "SELECT sequence FROM chunks WHERE session_id = ? AND NOT ("
            "ciphertext_verified = 1 AND decrypt_verified = 1 AND plaintext_verified = 1 "
            "AND flac_valid = 1) ORDER BY sequence",
            (session_id,),
        )
    ]


def events_for(session_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM events WHERE session_id = ? "
        "ORDER BY COALESCE(offset_ms, 0), id",
        (session_id,),
    )
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (ValueError, TypeError):
            item["payload"] = {}
        out.append(item)
    return out


def _session_duration_ms(session_id: str) -> int | None:
    """Total audio duration from the decoded chunks, when known."""
    row = db.query_one(
        "SELECT flac_json FROM chunks WHERE session_id = ? ORDER BY sequence", (session_id,)
    )
    if row is None:
        return None
    total = 0
    any_known = False
    for r in db.query(
        "SELECT flac_json FROM chunks WHERE session_id = ? ORDER BY sequence", (session_id,)
    ):
        try:
            info = json.loads(r["flac_json"] or "{}")
        except (ValueError, TypeError):
            continue
        ms = info.get("duration_ms")
        if isinstance(ms, int) and ms > 0:
            total += ms
            any_known = True
    return total if any_known else None


def patient_segments(session_id: str) -> list[dict[str, Any]]:
    """Logical patient segments derived from patient_boundary events.

    Segments are never merged and never interpreted clinically; they are only
    a timeline split that a later processing stage can act on.
    """
    session = get(session_id)
    if session is None:
        return []
    end = _session_duration_ms(session_id)
    raw = {
        int(e["offset_ms"])
        for e in events_for(session_id)
        if e["event"] == "patient_boundary" and e.get("offset_ms") is not None
    }
    # A boundary at or past the end of the audio (a recorder with a clock or
    # offset bug) would otherwise produce a segment with a negative length.
    # Segments are what keep two patients' audio apart, so anything that cannot
    # describe a real split is discarded rather than emitted.
    boundaries = sorted(
        b for b in raw if b > 0 and (end is None or b < end)
    )
    edges = [0, *boundaries]
    segments: list[dict[str, Any]] = []
    for index, start in enumerate(edges):
        stop = edges[index + 1] if index + 1 < len(edges) else end
        segments.append(
            {
                "index": index + 1,
                "start_ms": start,
                "end_ms": stop,
                "duration_ms": (stop - start) if stop is not None else None,
                "open_ended": stop is None,
            }
        )
    return segments


def privacy_gaps(session_id: str) -> list[dict[str, Any]]:
    """Periods where the recorder deliberately captured nothing."""
    gaps: list[dict[str, Any]] = []
    open_start: int | None = None
    for event in events_for(session_id):
        if event["event"] == "privacy_pause_started":
            open_start = event.get("offset_ms")
        elif event["event"] == "privacy_pause_ended" and open_start is not None:
            gaps.append(
                {
                    "start_ms": open_start,
                    "end_ms": event.get("offset_ms"),
                    "closed": True,
                }
            )
            open_start = None
    if open_start is not None:
        gaps.append({"start_ms": open_start, "end_ms": None, "closed": False})
    return gaps


def set_state(
    session_id: str,
    state: str,
    *,
    ingest_confirmed: bool | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    actor: str = "system",
    conn=None,
) -> None:
    current = get(session_id)
    if current is None:
        return
    target = conn if conn is not None else db.get_conn()
    fields = ["state = ?", "updated_at = ?"]
    params: list[Any] = [state, now_iso()]
    if ingest_confirmed is not None:
        fields.append("ingest_confirmed = ?")
        params.append(1 if ingest_confirmed else 0)
        if ingest_confirmed and not current.get("ingested_at"):
            fields.append("ingested_at = ?")
            params.append(now_iso())
    fields.append("error_code = ?")
    params.append(error_code)
    fields.append("error_message = ?")
    params.append(error_message)
    params.append(session_id)
    target.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE session_id = ?", tuple(params))

    if current["state"] != state or (
        ingest_confirmed is not None
        and bool(current["ingest_confirmed"]) != bool(ingest_confirmed)
    ):
        audit.log(
            "session",
            "state_transition",
            "success",
            session_id=session_id,
            device_id=current["device_id"],
            identity=actor,
            detail={
                "from": current["state"],
                "to": state,
                "ingest_confirmed": ingest_confirmed,
                "error_code": error_code,
            },
            conn=target,
        )


def evaluate(session_id: str, *, conn=None) -> dict[str, Any]:
    """Recompute completeness and, when the client has finished, confirm ingest.

    ``ingest_confirmed`` is only ever set once every manifested chunk is on
    durable storage with its ciphertext hash, GCM tag, plaintext hash and FLAC
    structure all verified, and the client has said it will send nothing more.
    """
    session = get(session_id)
    if session is None:
        return {}
    if session["state"] == "PURGED":
        return status_payload(session_id)

    expected = manifest_sequences(session_id)
    missing = missing_chunks(session_id)
    bad = unverified_chunks(session_id)
    complete_requested = bool(session["complete_requested"])
    declared = session["complete_chunk_count"]

    problems: list[str] = []
    if missing:
        problems.append(f"{len(missing)} manifested chunk(s) not received")
    if bad:
        problems.append(f"{len(bad)} received chunk(s) failed verification")
    if declared is not None and int(declared) != len(expected):
        problems.append(
            f"complete declared chunk_count {declared} but the manifest lists {len(expected)}"
        )
    if not session["wrap_ciphertext_b64"]:
        problems.append("session has no server key wrap")
    if not expected:
        problems.append("manifest lists no chunks")

    sticky = session["state"] in ADMIN_STICKY_STATES

    if not complete_requested:
        # Still receiving; nothing to confirm yet.
        if sticky or session["state"] not in ("RECEIVING", "CREATED"):
            return status_payload(session_id)
        set_state(session_id, "RECEIVING", ingest_confirmed=False, conn=conn)
        return status_payload(session_id)

    if problems:
        set_state(
            session_id,
            session["state"] if sticky else "RECEIVING",
            ingest_confirmed=False,
            error_code="MISSING_CHUNKS" if missing else "INCOMPLETE_INGEST",
            error_message="; ".join(problems),
            conn=conn,
        )
        return status_payload(session_id)

    if sticky or session["state"] in DURABLE_STATES:
        set_state(session_id, session["state"], ingest_confirmed=True, conn=conn)
    else:
        set_state(session_id, "INGESTED", ingest_confirmed=True, conn=conn)
    return status_payload(session_id)


def status_payload(session_id: str, *, verbose: bool = True) -> dict[str, Any]:
    session = get(session_id)
    if session is None:
        return {}
    expected = manifest_sequences(session_id)
    received = received_sequences(session_id)
    missing = sorted(set(expected) - set(received))
    payload: dict[str, Any] = {
        "session_id": session_id,
        "state": session["state"],
        "ingest_confirmed": bool(session["ingest_confirmed"]),
    }
    if verbose:
        payload.update(
            {
                "mode": session["mode"],
                "client_status": session["client_status"],
                "expected_chunks": len(expected),
                "received_chunks": len(received),
                "verified_chunks": verified_count(session_id),
                "missing_chunks": missing,
                "unverified_chunks": unverified_chunks(session_id),
                "last_updated_at": session["updated_at"],
                "error": (
                    {"code": session["error_code"], "message": session["error_message"]}
                    if session["error_code"]
                    else None
                ),
            }
        )
    return payload
