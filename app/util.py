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


def iso_from_epoch(seconds: float) -> str:
    """A unix timestamp as the same ISO-8601 shape used everywhere else."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(float(seconds), UTC).isoformat().replace("+00:00", "Z")


def epoch_from_iso(value: str | None) -> float | None:
    parsed = parse_iso(value)
    return parsed.timestamp() if parsed else None


# The pod stores every timestamp in UTC, which is right, and used to show them
# in UTC too, which was not: a consultation at 21:45 in Leusden appeared as
# 19:45. Display goes through here instead. TZ is set on the container
# (Europe/Amsterdam); VS_DISPLAY_TZ overrides it, and an unknown zone falls
# back to UTC rather than failing to render a page.
def display_zone():
    import os
    from datetime import timezone

    name = os.environ.get("VS_DISPLAY_TZ") or os.environ.get("TZ") or ""
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def local_time(value: str | None, *, with_date: bool = True) -> str:
    """An ISO-8601 UTC timestamp as the practice's own wall clock."""
    parsed = parse_iso(value)
    if parsed is None:
        return "" if value is None else str(value)
    local = parsed.astimezone(display_zone())
    return local.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
