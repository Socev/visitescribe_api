"""Append-only audit trail.

Never records plaintext audio, session keys or private key material. Chunk
records carry hashes and sizes only.
"""
from __future__ import annotations

import json
from typing import Any

from . import db
from .util import now_iso


def log(
    category: str,
    action: str,
    outcome: str = "success",
    *,
    device_id: str | None = None,
    session_id: str | None = None,
    sequence: int | None = None,
    identity: str | None = None,
    auth_method: str | None = None,
    source_ip: str | None = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
    conn=None,
) -> None:
    payload = json.dumps(_sanitise(detail or {}), ensure_ascii=False, default=str)
    sql = (
        "INSERT INTO audit(ts, category, action, outcome, device_id, session_id, sequence, "
        "identity, auth_method, source_ip, idempotency_key, request_id, detail_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    params = (
        now_iso(), category, action, outcome, device_id, session_id, sequence,
        identity, auth_method, source_ip, idempotency_key, request_id, payload,
    )
    target = conn if conn is not None else db.get_conn()
    target.execute(sql, params)


_FORBIDDEN_KEYS = {
    "session_key", "aes_key", "key", "private_key", "plaintext", "audio",
    "password", "token", "secret",
}


def _sanitise(detail: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in detail.items():
        if k.lower() in _FORBIDDEN_KEYS:
            clean[k] = "[redacted]"
        elif isinstance(v, bytes):
            clean[k] = f"<{len(v)} bytes>"
        elif isinstance(v, dict):
            clean[k] = _sanitise(v)
        else:
            clean[k] = v
    return clean


def recent(limit: int = 200, offset: int = 0, **filters: Any) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    for column in ("device_id", "session_id", "category", "outcome"):
        value = filters.get(column)
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    search = filters.get("q")
    if search:
        where.append("(action LIKE ? OR detail_json LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    rows = db.query(
        f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params)
    )
    return [dict(r) for r in rows]


def count(**filters: Any) -> int:
    where: list[str] = []
    params: list[Any] = []
    for column in ("device_id", "session_id", "category", "outcome"):
        value = filters.get(column)
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    row = db.query_one(f"SELECT COUNT(*) AS n FROM audit {clause}", tuple(params))
    return int(row["n"]) if row else 0
