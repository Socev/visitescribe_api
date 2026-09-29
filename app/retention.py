"""Audio leaves the server once it has done its job.

A recording exists to become a report. Once every part of it has been
transcribed and has a report, the audio has no further use here and is
patient data we should not be holding, so it is removed: the encrypted
chunks, any decrypted working copies, the cached session key and the
wrapped key in the database. What stays is the session itself, the
transcript, the report, the costs and the audit trail -- including a record
of this removal.

Two exceptions:

* A recorder can be put in *diagnostische modus* by an admin. Every
  recording it makes while that is on is marked ``keep_audio`` at creation
  and keeps its audio, so a problem can be investigated -- but never for
  longer than ``VS_DIAGNOSTIC_AUDIO_DAYS`` (30 by default), so a switch left
  on by mistake does not keep patient audio for ever.
* A recording whose processing failed, was cancelled or never started keeps
  its audio: without it there is nothing to try again with.

Recordings that were already on the server before this module existed are
not touched automatically; the admin can remove their audio in one go
(``purge_processed_backlog``).

The recorder is not affected by any of this: its copy is deleted as soon as
the server confirms ingest. A removed session keeps ``ingest_confirmed``, so
a recorder that asks again is told "done" and never uploads it a second time.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit, crypto, db, sessions, storage
from .config import settings
from .util import now_iso

log = logging.getLogger("visitescribe.retention")

# When automatic removal started on this installation. Sessions created before
# it are left to the admin (backlog), so an upgrade never silently deletes
# audio that was made under the old rules.
SINCE_KEY = "audio_retention_since"


def since() -> str:
    value = db.get_meta(SINCE_KEY)
    if not value:
        value = now_iso()
        db.set_meta(SINCE_KEY, value)
    return value


def _iso_days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.isoformat().replace("+00:00", "Z")


def processing_finished(session_id: str) -> bool:
    """Every job of the session is done and at least one report exists."""
    row = db.query_one(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN stage = 'note' AND state = 'done' THEN 1 ELSE 0 END) AS notes "
        "FROM processing_jobs WHERE session_id = ?", (session_id,))
    if row is None or not row["n"]:
        return False
    return int(row["done"] or 0) == int(row["n"]) and int(row["notes"] or 0) > 0


def ingest_whole(session: dict[str, Any]) -> bool:
    """Every manifested chunk arrived and verified, and the recorder said done.

    An admin can force processing of an incomplete recording. Its audio must
    then stay: the recorder deletes its own copy once the session reads as
    confirmed, so the server copy may be the only one of what did arrive.
    """
    if not session.get("complete_requested"):
        return False
    sid = session["session_id"]
    return not sessions.missing_chunks(sid) and not sessions.unverified_chunks(sid)


def purge_audio(session_id: str, *, who: str, reason: str) -> dict[str, Any] | None:
    """Remove all audio of one session. Returns None if there was nothing to do.

    The session is claimed first with one conditional UPDATE, in the same
    write lock that enqueue() takes: either a new job gets in first (and the
    purge backs off) or the claim does (and enqueue refuses AUDIO_PURGED).
    """
    session = sessions.get(session_id)
    if session is None or session["state"] == "PURGED" or session.get("audio_purged_at"):
        return None
    ts = now_iso()
    with db.tx() as conn:
        claimed = conn.execute(
            "UPDATE sessions SET audio_purged_at = ?, updated_at = ? "
            "WHERE session_id = ? AND audio_purged_at IS NULL AND state != 'PURGED' "
            "AND NOT EXISTS (SELECT 1 FROM processing_jobs j WHERE j.session_id = ? "
            "AND j.state IN ('queued', 'running'))",
            (ts, ts, session_id, session_id)).rowcount
    if not claimed:
        return None

    removed = 0
    for row in db.query(
        "SELECT plaintext_blob_path FROM chunks WHERE session_id = ? "
        "AND plaintext_blob_path IS NOT NULL", (session_id,)
    ):
        if storage.delete_blob(row["plaintext_blob_path"]):
            removed += 1
    removed += storage.delete_session_blobs(session_id)
    shutil.rmtree(settings.data_dir / "work" / session_id, ignore_errors=True)
    crypto.drop_session_key(session_id)

    with db.tx() as conn:
        # The chunk rows stay as a record of what was received (hashes and
        # sizes, no audio): they are what keeps the session "confirmed" for
        # the recorder. Only the pointers to decrypted copies go.
        conn.execute("UPDATE chunks SET plaintext_blob_path = NULL WHERE session_id = ?",
                     (session_id,))
        conn.execute(
            "UPDATE sessions SET wrap_ciphertext_b64 = NULL, updated_at = ? "
            "WHERE session_id = ?", (now_iso(), session_id))
        conn.execute(
            "INSERT INTO purges(session_id, scope, actor, ts, detail_json) "
            "VALUES(?,?,?,?,?)",
            (session_id, "audio", who, ts,
             json.dumps({"files_removed": removed, "reason": reason})))
        audit.log("purge", "audio_removed", "success", session_id=session_id,
                  device_id=session["device_id"], identity=who,
                  detail={"files_removed": removed, "reason": reason}, conn=conn)
    log.info("audio of %s removed (%s, %d files)", session_id, reason, removed)
    return {"session_id": session_id, "files_removed": removed, "reason": reason}


def after_processing(session_id: str) -> dict[str, Any] | None:
    """Called by the worker when a session has no jobs left. Never raises."""
    if not settings.auto_purge_audio:
        return None
    try:
        session = sessions.get(session_id)
        if session is None or session.get("keep_audio"):
            return None
        if (session.get("created_at") or "") < since():
            return None
        if not processing_finished(session_id) or not ingest_whole(session):
            return None
        return purge_audio(session_id, who="auto", reason="processed")
    except Exception as exc:  # noqa: BLE001 - a report must never fail over this
        log.warning("removing audio of %s failed: %s", session_id, exc)
        audit.log("purge", "audio_removed", "failure", session_id=session_id,
                  identity="auto", detail={"reason": str(exc)[:300]})
        return None


def sweep() -> dict[str, int]:
    """Periodic backstop: expired diagnostic audio and missed removals."""
    out = {"processed": 0, "diagnostic_expired": 0}
    if not settings.auto_purge_audio:
        return out
    cutoff = _iso_days_ago(max(1, settings.diagnostic_audio_days))
    for row in db.query(
        "SELECT session_id FROM sessions WHERE keep_audio = 1 "
        "AND audio_purged_at IS NULL AND state != 'PURGED' AND created_at < ?",
        (cutoff,),
    ):
        if purge_audio(row["session_id"], who="auto", reason="diagnostic_expired"):
            out["diagnostic_expired"] += 1
    for row in db.query(
        "SELECT session_id FROM sessions WHERE keep_audio = 0 "
        "AND audio_purged_at IS NULL AND state != 'PURGED' AND created_at >= ?",
        (since(),),
    ):
        session = sessions.get(row["session_id"])
        if session and processing_finished(row["session_id"]) and ingest_whole(session):
            if purge_audio(row["session_id"], who="auto", reason="processed"):
                out["processed"] += 1
    # A purge cut off half-way (claimed, files not yet all gone) is finished.
    for row in db.query("SELECT session_id FROM sessions WHERE audio_purged_at IS NOT NULL "
                        "AND state != 'PURGED'"):
        if storage.session_dir(row["session_id"]).exists():
            storage.delete_session_blobs(row["session_id"])
            shutil.rmtree(settings.data_dir / "work" / row["session_id"], ignore_errors=True)
            db.execute("UPDATE sessions SET wrap_ciphertext_b64 = NULL WHERE session_id = ?",
                       (row["session_id"],))
    return out


def backlog() -> list[str]:
    """Processed, not diagnostic, audio still present, made before `since`."""
    return [r["session_id"] for r in db.query(
        "SELECT session_id FROM sessions WHERE keep_audio = 0 "
        "AND audio_purged_at IS NULL AND state != 'PURGED' AND created_at < ? "
        "ORDER BY created_at", (since(),))
        if processing_finished(r["session_id"])
        and ingest_whole(sessions.get(r["session_id"]) or {"complete_requested": 0})]


def purge_processed_backlog(who: str) -> dict[str, int]:
    removed = 0
    for session_id in backlog():
        if purge_audio(session_id, who=who, reason="backlog"):
            removed += 1
    return {"sessions": removed}


def audio_status(session: dict[str, Any]) -> dict[str, Any]:
    """For the admin page: what will happen to this recording's audio."""
    if session.get("state") == "PURGED":
        return {"state": "purged", "label": "Sessie gewist"}
    if session.get("audio_purged_at"):
        return {"state": "removed", "at": session["audio_purged_at"],
                "label": "Audio verwijderd na verwerking"}
    if session.get("keep_audio"):
        created = session.get("created_at") or ""
        until = ""
        try:
            base = datetime.fromisoformat(created.replace("Z", "+00:00"))
            until = (base + timedelta(days=settings.diagnostic_audio_days)).strftime(
                "%Y-%m-%d")
        except ValueError:
            pass
        return {"state": "kept", "until": until,
                "label": f"Bewaard (diagnostische modus) tot {until or '?'}"}
    return {"state": "present",
            "label": "Wordt verwijderd zodra de verwerking klaar is"}
