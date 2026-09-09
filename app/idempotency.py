"""Idempotency-Key bookkeeping.

Two behaviours are needed:

* **replay** — session create and chunk upload return the stored response for
  a byte-identical retry, and 409 when the same key carries different content.
* **re-evaluate** — ``complete`` must recompute on every call. Its second call
  in the acceptance flow deliberately arrives *after* a previously missing
  chunk was uploaded and has to report the new state, so replaying a cached
  body there would be wrong.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import db
from .errors import ApiError
from .util import now_iso


@dataclass
class Replay:
    status_code: int
    body: dict[str, Any]


def lookup(idem_key: str, device_id: str) -> dict[str, Any] | None:
    return db.row_to_dict(
        db.query_one(
            "SELECT * FROM idempotency WHERE idem_key = ? AND device_id = ?",
            (idem_key, device_id),
        )
    )


def check_replay(
    idem_key: str | None,
    device_id: str,
    endpoint: str,
    request_hash: str,
    *,
    strict: bool = True,
) -> Replay | None:
    """Return a stored response for an identical retry, or raise on a mismatch."""
    if not idem_key:
        return None
    row = lookup(idem_key, device_id)
    if row is None:
        return None
    if row["endpoint"] != endpoint:
        raise ApiError(
            "IDEMPOTENCY_CONFLICT",
            "Idempotency-Key was already used for a different endpoint",
            extra={"previous_endpoint": row["endpoint"]},
        )
    if row["request_hash"] != request_hash:
        if strict:
            raise ApiError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used with a different payload",
            )
        return None
    try:
        body = json.loads(row["response_json"])
    except (ValueError, TypeError):
        return None
    return Replay(status_code=int(row["status_code"]), body=body)


def record(
    idem_key: str | None,
    device_id: str,
    endpoint: str,
    request_hash: str,
    status_code: int,
    body: dict[str, Any],
    conn=None,
) -> None:
    if not idem_key:
        return
    target = conn if conn is not None else db.get_conn()
    target.execute(
        "INSERT INTO idempotency(idem_key, device_id, endpoint, request_hash, "
        "status_code, response_json, created_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(idem_key, device_id) DO UPDATE SET "
        "request_hash = excluded.request_hash, status_code = excluded.status_code, "
        "response_json = excluded.response_json",
        (
            idem_key, device_id, endpoint, request_hash, status_code,
            json.dumps(body, ensure_ascii=False, default=str), now_iso(),
        ),
    )


def key_of(request_headers) -> str | None:
    value = request_headers.get("idempotency-key")
    return value.strip() if value else None
