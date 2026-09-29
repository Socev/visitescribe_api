"""The recorder's own log, kept on the server for the admin.

Brian keeps everything it prints (what the serial monitor shows) in a buffer
and on its SD card, and sends the lines added since its last upload on every
sync. An admin can also ask for everything it still has ("Haal volledige log
op"); it then resends the lot at the next sync. Each line carries the
recorder's own numbering -- boot counter and line within that boot -- so a
resend or a retried upload is ignored rather than stored twice.

Wire format, one line each, UTF-8 text/plain:

    B<boot>.<line> <iso-time or -> <uptime-ms> <text>

What is in there: session numbers, network names, battery, firmware state.
Not in there: audio, patient data, Wi-Fi passwords, the device token (the
firmware masks the one line that used to print it). Admin-only.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import audit, db
from .config import settings
from .errors import ApiError
from .util import now, now_iso

_LINE = re.compile(r"^B(\d{1,9})\.(\d{1,9}) (\S+) (\d{1,12}) ?(.*)$")
MAX_TEXT = 1000


def ingest(device_id: str, body: bytes, *, request_id: int | None = None) -> dict[str, Any]:
    if len(body) > settings.device_log_max_upload:
        raise ApiError("PAYLOAD_TOO_LARGE",
                       f"Log upload exceeds {settings.device_log_max_upload} bytes")
    text = body.decode("utf-8", errors="replace")
    ts = now_iso()
    rows = []
    rejected = 0
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            if raw.strip():
                rejected += 1
            continue
        boot, line, when, uptime, msg = m.groups()
        rows.append((device_id, int(boot), int(line),
                     None if when == "-" else when[:40], int(uptime),
                     msg[:MAX_TEXT], ts))
    stored = 0
    with db.tx() as conn:
        for row in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO device_log_lines(device_id, boot, line, device_time, "
                "uptime_ms, text, received_at) VALUES(?,?,?,?,?,?,?)", row)
            stored += cur.rowcount
        if request_id:
            conn.execute(
                "UPDATE devices SET log_request_done = MAX(log_request_done, ?) "
                "WHERE device_id = ? AND log_request_id >= ?",
                (int(request_id), device_id, int(request_id)))
    _prune(device_id)
    highest = max(((r[1], r[2]) for r in rows), default=None)
    if request_id:
        audit.log("device", "log_full_received", "success", device_id=device_id,
                  detail={"request_id": request_id, "lines": len(rows), "new": stored})
    return {"accepted": len(rows), "stored": stored, "rejected": rejected,
            "highest": {"boot": highest[0], "line": highest[1]} if highest else None}


def _prune(device_id: str) -> None:
    cutoff = (now() - timedelta(days=max(1, settings.device_log_days))
              ).isoformat().replace("+00:00", "Z")
    db.execute("DELETE FROM device_log_lines WHERE received_at < ?", (cutoff,))
    row = db.query_one("SELECT COUNT(*) AS n FROM device_log_lines WHERE device_id = ?",
                       (device_id,))
    excess = int(row["n"]) - settings.device_log_max_lines if row else 0
    if excess > 0:
        db.execute(
            "DELETE FROM device_log_lines WHERE rowid IN (SELECT rowid FROM device_log_lines "
            "WHERE device_id = ? ORDER BY boot, line LIMIT ?)", (device_id, excess))


def request_full(device_id: str, *, actor: str) -> int:
    row = db.query_one("SELECT log_request_id FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        raise ApiError("INVALID_REQUEST", "Onbekend apparaat", status_code=404)
    new_id = int(row["log_request_id"] or 0) + 1
    db.execute("UPDATE devices SET log_request_id = ?, log_request_at = ? WHERE device_id = ?",
               (new_id, now_iso(), device_id))
    audit.log("device", "log_full_requested", "success", device_id=device_id, identity=actor,
              detail={"request_id": new_id})
    return new_id


def config_block(device: dict[str, Any]) -> dict[str, Any] | None:
    """Tells the recorder to resend everything, until it reports this id back."""
    wanted = int(device.get("log_request_id") or 0)
    done = int(device.get("log_request_done") or 0)
    return {"id": wanted} if wanted > done else None


def status(device: dict[str, Any]) -> dict[str, Any]:
    row = db.query_one(
        "SELECT COUNT(*) AS n, MAX(received_at) AS last FROM device_log_lines "
        "WHERE device_id = ?", (device["device_id"],))
    return {"lines": int(row["n"]) if row else 0,
            "last_received": row["last"] if row else None,
            "requested": config_block(device) is not None,
            "requested_at": device.get("log_request_at")}


def lines(device_id: str, *, q: str = "", limit: int = 300) -> list[dict[str, Any]]:
    """Newest `limit` lines (optionally containing `q`), returned oldest first."""
    params: list[Any] = [device_id]
    where = "device_id = ?"
    if q:
        where += " AND text LIKE ? ESCAPE '\\'"
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    params.append(max(1, min(int(limit), 5000)))
    rows = db.query(f"SELECT boot, line, device_time, uptime_ms, text, received_at "
                    f"FROM device_log_lines WHERE {where} ORDER BY boot DESC, line DESC LIMIT ?",
                    tuple(params))
    return [dict(r) for r in reversed(rows)]


def export_text(device_id: str) -> str:
    out = []
    for r in db.query("SELECT boot, line, device_time, uptime_ms, text FROM device_log_lines "
                      "WHERE device_id = ? ORDER BY boot, line", (device_id,)):
        out.append(f"B{r['boot']}.{r['line']} {r['device_time'] or '-'} "
                   f"{r['uptime_ms']} {r['text']}")
    return "\n".join(out) + ("\n" if out else "")
