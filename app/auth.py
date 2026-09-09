"""Device identity resolution for every /v1 request.

A request is only accepted when the ``X-Device-ID`` header names a device that
exists, is enabled, and satisfies whatever proof that device requires:

* a **pinned client certificate** (mTLS) — strongest, enforced by fingerprint;
* a **device token** (``Authorization: Bearer`` or ``X-Device-Token``);
* or, for a device explicitly marked ``allow_header_only``, the device ID
  alone — which is exactly what the stock VisiteScribe v0.2 recorder sends.

Unknown device IDs are never registered on their own. An administrator opens a
short enrolment window for one specific device ID first.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from fastapi import Request

from . import audit, db, mtls
from .config import settings
from .errors import ApiError
from .util import is_device_id, now, now_iso, parse_iso, token_hash


@dataclass
class DeviceIdentity:
    device_id: str
    row: dict[str, Any]
    auth_method: str
    cert_fingerprint: str | None = None
    cert_subject: str | None = None
    source_ip: str | None = None

    @property
    def config(self) -> dict[str, Any]:
        try:
            return json.loads(self.row.get("config_json") or "{}")
        except (ValueError, TypeError):
            return {}


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


def _cert_from_header(request: Request) -> bytes | None:
    header = settings.mtls_header
    if not header:
        return None
    raw = request.headers.get(header)
    if not raw:
        return None
    fmt = settings.mtls_header_format
    try:
        if fmt in ("pem_urlencoded", "pem"):
            text = unquote(raw) if fmt == "pem_urlencoded" else raw
            text = text.replace("\t", "\n")
            if "BEGIN CERTIFICATE" not in text:
                return None
            cert = x509.load_pem_x509_certificate(text.encode("utf-8"))
            return cert.public_bytes(encoding=serialization.Encoding.DER)
        if fmt == "der_base64":
            import base64

            return base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - a malformed header is simply "no cert"
        return None
    return None


def _peer_cert(request: Request) -> tuple[bytes | None, str]:
    """Return (DER certificate, how it was obtained)."""
    der = mtls.lookup(request.scope.get("client"))
    if der:
        return der, "tls"
    der = _cert_from_header(request)
    if der:
        return der, "header"
    return None, ""


def _fingerprint(der: bytes) -> tuple[str, str]:
    cert = x509.load_der_x509_certificate(der)
    fp = cert.fingerprint(hashes.SHA256()).hex()
    try:
        subject = cert.subject.rfc4514_string()
    except Exception:  # noqa: BLE001
        subject = ""
    return fp, subject


def get_device(device_id: str) -> dict[str, Any] | None:
    return db.row_to_dict(
        db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    )


def _consume_enrolment(device_id: str) -> bool:
    row = db.query_one(
        "SELECT expires_at FROM enrolment_windows WHERE device_id = ?", (device_id,)
    )
    if row is None:
        return False
    expires = parse_iso(row["expires_at"])
    if expires is None or expires <= now():
        db.execute("DELETE FROM enrolment_windows WHERE device_id = ?", (device_id,))
        return False
    return True


def create_device(
    device_id: str,
    *,
    display_name: str = "",
    allow_header_only: bool | None = None,
    cert_fingerprint: str | None = None,
    cert_subject: str | None = None,
    token_hash_value: str | None = None,
    token_hint: str | None = None,
    conn=None,
) -> dict[str, Any]:
    if allow_header_only is None:
        allow_header_only = not settings.require_device_auth
    ts = now_iso()
    target = conn if conn is not None else db.get_conn()
    target.execute(
        "INSERT INTO devices(device_id, display_name, enabled, allow_header_only, "
        "token_hash, token_hint, cert_fingerprint, cert_subject, config_json, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(device_id) DO NOTHING",
        (
            device_id, display_name or device_id, 1, 1 if allow_header_only else 0,
            token_hash_value, token_hint, cert_fingerprint, cert_subject, "{}", ts, ts,
        ),
    )
    return get_device(device_id) or {}


def touch_device(device_id: str, ip: str, auth_method: str) -> None:
    db.execute(
        "UPDATE devices SET last_seen_at = ?, last_source_ip = ?, last_auth_method = ?, "
        "updated_at = ? WHERE device_id = ?",
        (now_iso(), ip, auth_method, now_iso(), device_id),
    )


def authenticate(request: Request) -> DeviceIdentity:
    ip = client_ip(request)
    device_id = (request.headers.get("x-device-id") or "").strip()

    if not device_id:
        audit.log("auth", "missing_device_header", "failure", source_ip=ip)
        raise ApiError("INVALID_DEVICE", "X-Device-ID header is required")
    if not is_device_id(device_id):
        audit.log("auth", "malformed_device_id", "failure", source_ip=ip,
                  detail={"device_id_length": len(device_id)})
        raise ApiError("INVALID_DEVICE", "X-Device-ID is malformed")

    der, cert_source = _peer_cert(request)
    cert_fp: str | None = None
    cert_subject: str | None = None
    if der:
        try:
            cert_fp, cert_subject = _fingerprint(der)
        except Exception:  # noqa: BLE001
            cert_fp, cert_subject = None, None

    device = get_device(device_id)

    if device is None:
        if _consume_enrolment(device_id):
            device = create_device(
                device_id,
                cert_fingerprint=cert_fp,
                cert_subject=cert_subject,
            )
            db.execute("DELETE FROM enrolment_windows WHERE device_id = ?", (device_id,))
            audit.log("device", "enrolled", "success", device_id=device_id, source_ip=ip,
                      detail={"cert_pinned": bool(cert_fp)})
        else:
            audit.log("auth", "unknown_device", "failure", device_id=device_id, source_ip=ip)
            raise ApiError("INVALID_DEVICE", "Unknown device")

    if not device.get("enabled") or device.get("revoked_at"):
        audit.log("auth", "device_disabled", "failure", device_id=device_id, source_ip=ip)
        raise ApiError("DEVICE_DISABLED", "Device is disabled or revoked")

    methods: list[str] = []

    # --- client certificate -------------------------------------------
    pinned = device.get("cert_fingerprint")
    if pinned:
        if not cert_fp:
            audit.log("security", "cert_missing", "failure", device_id=device_id,
                      source_ip=ip)
            raise ApiError(
                "DEVICE_CERT_MISMATCH",
                "This device requires a client certificate",
            )
        if not hmac.compare_digest(cert_fp, pinned):
            audit.log("security", "cert_mismatch", "failure", device_id=device_id,
                      source_ip=ip, detail={"presented_fingerprint": cert_fp[:16]})
            raise ApiError(
                "DEVICE_CERT_MISMATCH",
                "Client certificate does not match the certificate pinned to this device",
            )
        methods.append(f"mtls:{cert_source}")
    elif cert_fp:
        # A certificate was presented but this device is not pinned to one.
        # Reject if it belongs to a different device: X-Device-ID must agree
        # with the authenticated identity.
        other = db.query_one(
            "SELECT device_id FROM devices WHERE cert_fingerprint = ?", (cert_fp,)
        )
        if other is not None and other["device_id"] != device_id:
            audit.log("security", "cert_belongs_to_other_device", "failure",
                      device_id=device_id, source_ip=ip,
                      detail={"certificate_device": other["device_id"]})
            raise ApiError(
                "DEVICE_CERT_MISMATCH",
                "X-Device-ID does not match the authenticated certificate identity",
            )
        methods.append(f"mtls-unpinned:{cert_source}")

    # --- device token ---------------------------------------------------
    presented = _presented_token(request)
    stored = device.get("token_hash")
    if stored:
        if not presented or not hmac.compare_digest(token_hash(presented), stored):
            audit.log("auth", "bad_device_token", "failure", device_id=device_id,
                      source_ip=ip)
            raise ApiError("INVALID_DEVICE", "Invalid device token")
        methods.append("token")

    if not methods:
        if device.get("allow_header_only"):
            methods.append("device-id")
        else:
            audit.log("auth", "credentials_required", "failure", device_id=device_id,
                      source_ip=ip)
            raise ApiError(
                "INVALID_DEVICE",
                "This device requires a device token or a client certificate",
            )

    auth_method = "+".join(methods)
    touch_device(device_id, ip, auth_method)
    return DeviceIdentity(
        device_id=device_id,
        row=device,
        auth_method=auth_method,
        cert_fingerprint=cert_fp,
        cert_subject=cert_subject,
        source_ip=ip,
    )


def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    direct = request.headers.get("x-device-token")
    return direct.strip() if direct else None
