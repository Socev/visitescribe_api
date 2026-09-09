from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().isoformat().replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256_hex(value: str | None) -> bool:
    return bool(value) and bool(_HEX64.match(value.strip().lower()))


def norm_hex(value: str) -> str:
    return value.strip().lower()


def b64decode_strict(value: str) -> bytes:
    """Decode base64 rejecting whitespace-free non-canonical input."""
    s = value.strip()
    if not s:
        raise ValueError("empty base64")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64: {exc}") from exc


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj).encode("utf-8"))


def is_uuid_like(value: str) -> bool:
    """Accept a real UUID or any other sufficiently unique opaque id.

    The spec asks for "a valid UUID or equivalent unique session ID", so this
    stays permissive but refuses anything that could escape a path segment.
    """
    if not value or len(value) > 128:
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        pass
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$", value))


DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def is_device_id(value: str | None) -> bool:
    return bool(value) and bool(DEVICE_ID_RE.match(value))


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact(value: str | None, keep: int = 6) -> str:
    if not value:
        return ""
    return value[:keep] + "…" if len(value) > keep else value


def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    step = 1024.0
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < step or unit == "TiB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} TiB"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None
