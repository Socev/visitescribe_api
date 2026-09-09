"""The /v1 ingest API — the contract the recorder depends on."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import audit, crypto, db, flacinfo, idempotency, ratelimit, sessions, storage
from .auth import DeviceIdentity, authenticate
from .config import SUPPORTED_MODES, settings
from .errors import ApiError
from .models import CompleteRequest, EventsRequest, HeartbeatRequest, SessionManifest
from .util import (
    canonical_hash,
    b64decode_strict,
    is_sha256_hex,
    is_uuid_like,
    norm_hex,
    now_iso,
    sha256_hex,
)

router = APIRouter(prefix="/v1")

# SQLite stores signed 64-bit integers; anything beyond this is a payload
# problem, not a server fault.
MAX_SEQUENCE = 2**31 - 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require_uploads_enabled(ident: DeviceIdentity) -> None:
    """An administrator can pause a device without disabling it entirely."""
    if not ident.row.get("upload_enabled", 1):
        audit.log("auth", "uploads_paused", "failure", device_id=ident.device_id,
                  source_ip=ident.source_ip)
        raise ApiError(
            "DEVICE_UPLOADS_PAUSED",
            "Uploads are paused for this device by an administrator",
        )


def _identity(request: Request) -> DeviceIdentity:
    ident = authenticate(request)
    if not ratelimit.allow(
        ident.device_id, settings.rate_limit_per_minute, settings.rate_limit_burst
    ):
        audit.log("auth", "rate_limited", "failure", device_id=ident.device_id,
                  source_ip=ident.source_ip)
        raise ApiError("RATE_LIMITED", "Too many requests for this device")
    return ident


async def _read_capped(request: Request, cap: int, what: str) -> bytes:
    """Read a body, aborting as soon as it exceeds `cap`.

    Content-Length is only a hint — a chunked request has none — so the limit
    is enforced while streaming rather than after buffering the whole thing.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise ApiError("PAYLOAD_TOO_LARGE", f"{what} exceeds {cap} bytes")
    parts: list[bytes] = []
    total = 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            raise ApiError("PAYLOAD_TOO_LARGE", f"{what} exceeds {cap} bytes")
        parts.append(piece)
    return b"".join(parts)


async def _json_body(request: Request, limit: int | None = None) -> Any:
    cap = limit or settings.max_json_bytes
    raw = await _read_capped(request, cap, "JSON body")
    if not raw:
        raise ApiError("INVALID_REQUEST", "Request body is empty")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ApiError("INVALID_REQUEST", f"Body is not valid JSON: {exc}") from exc


def _raw_header(request: Request, name: str) -> bytes | None:
    """The header's bytes exactly as they arrived.

    Starlette decodes header values as latin-1, so re-encoding them as UTF-8
    silently corrupts any non-ASCII AAD. The GCM AAD has to be the bytes on the
    wire, so it is taken from the raw scope.
    """
    wanted = name.lower().encode("latin-1")
    for key, value in request.scope.get("headers", ()):
        if key.lower() == wanted:
            return value
    return None


def _owned_session(session_id: str, ident: DeviceIdentity) -> dict[str, Any]:
    if not is_uuid_like(session_id):
        raise ApiError("UNKNOWN_SESSION", "Malformed session_id")
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    if session["device_id"] != ident.device_id:
        audit.log("security", "session_ownership_violation", "failure",
                  device_id=ident.device_id, session_id=session_id,
                  source_ip=ident.source_ip,
                  detail={"owner": session["device_id"]})
        raise ApiError("DEVICE_NOT_OWNER", "Session belongs to a different device")
    if session["state"] == "PURGED":
        raise ApiError("SESSION_PURGED", "Session has been purged")
    return session


def _session_key(session: dict[str, Any]) -> bytes:
    cached = crypto.cached_session_key(session["session_id"])
    if cached is not None:
        return cached
    wrapped = session.get("wrap_ciphertext_b64")
    if not wrapped:
        raise ApiError("INVALID_KEY_WRAP", "Session has no server key wrap")
    try:
        key, _ = crypto.unwrap_session_key(
            b64decode_strict(wrapped), session.get("wrap_key_id")
        )
    except (ValueError, crypto.KeyWrapError) as exc:
        raise ApiError("INVALID_KEY_WRAP", str(exc)) from exc
    crypto.cache_session_key(
        session["session_id"], key, settings.session_key_cache_seconds
    )
    return key


# ---------------------------------------------------------------------------
# POST /v1/sessions
# ---------------------------------------------------------------------------

@router.post("/sessions")
async def create_session(request: Request) -> Response:
    ident = _identity(request)
    _require_uploads_enabled(ident)
    payload = await _json_body(request)
    idem_key = idempotency.key_of(request.headers)

    try:
        manifest = SessionManifest.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(
            "INVALID_MANIFEST",
            "Manifest failed validation",
            extra={"details": _short_errors(exc)},
        ) from exc

    if manifest.schema_version != settings.schema_version:
        raise ApiError(
            "INVALID_SCHEMA_VERSION",
            f"schema_version must be {settings.schema_version}, got {manifest.schema_version}",
        )
    if not is_uuid_like(manifest.session_id):
        raise ApiError("INVALID_MANIFEST", "session_id is not a valid unique identifier")
    if manifest.device_id and manifest.device_id != ident.device_id:
        audit.log("security", "manifest_device_mismatch", "failure",
                  device_id=ident.device_id, session_id=manifest.session_id,
                  source_ip=ident.source_ip,
                  detail={"manifest_device_id": manifest.device_id})
        raise ApiError(
            "INVALID_MANIFEST",
            "manifest device_id does not match the authenticated device",
        )
    if manifest.mode not in SUPPORTED_MODES:
        raise ApiError(
            "INVALID_MANIFEST",
            f"mode must be one of {', '.join(SUPPORTED_MODES)}",
        )

    encryption = manifest.encryption
    if (encryption.algorithm or "").upper() != crypto.AES_ALGORITHM:
        raise ApiError(
            "INVALID_MANIFEST",
            f"encryption.algorithm must be {crypto.AES_ALGORITHM}",
        )
    wrap = encryption.server_key_wrap
    if wrap is None or not wrap.ciphertext_b64:
        raise ApiError("INVALID_KEY_WRAP", "encryption.server_key_wrap is required")
    if (wrap.algorithm or "").upper() != crypto.WRAP_ALGORITHM:
        raise ApiError(
            "INVALID_KEY_WRAP",
            f"server_key_wrap.algorithm must be {crypto.WRAP_ALGORITHM}",
        )
    try:
        wrapped_bytes = b64decode_strict(wrap.ciphertext_b64)
    except ValueError as exc:
        raise ApiError("INVALID_KEY_WRAP", f"ciphertext_b64 is not valid base64: {exc}") from exc
    try:
        session_key, key_id = crypto.unwrap_session_key(wrapped_bytes, wrap.key_id)
    except crypto.KeyWrapError as exc:
        audit.log("security", "key_wrap_rejected", "failure", device_id=ident.device_id,
                  session_id=manifest.session_id, source_ip=ident.source_ip,
                  detail={"reason": str(exc)})
        raise ApiError("INVALID_KEY_WRAP", str(exc)) from exc

    chunk_specs = _validate_manifest_chunks(manifest)

    # RSA-OAEP padding is randomised, so a client that re-wraps the same session
    # key produces different manifest bytes for the same session. Identity is
    # therefore judged on what the manifest *means* — including the unwrapped
    # session key — not on the exact bytes that carried it.
    request_hash = _manifest_fingerprint(manifest, chunk_specs, session_key)

    replay = idempotency.check_replay(
        idem_key, ident.device_id, "sessions.create", request_hash, strict=True
    )
    if replay is not None:
        body = dict(replay.body)
        body["created"] = False
        fresh = sessions.status_payload(body.get("session_id", ""), verbose=False)
        if fresh:
            body.update(fresh)
        audit.log("session", "create_replayed", "success", device_id=ident.device_id,
                  session_id=body.get("session_id"), source_ip=ident.source_ip,
                  idempotency_key=idem_key, auth_method=ident.auth_method)
        return JSONResponse(status_code=200, content=body)

    existing = sessions.get(manifest.session_id)
    if existing is not None:
        if existing["device_id"] != ident.device_id:
            audit.log("security", "session_device_conflict", "failure",
                      device_id=ident.device_id, session_id=manifest.session_id,
                      source_ip=ident.source_ip, detail={"owner": existing["device_id"]})
            raise ApiError(
                "SESSION_DEVICE_CONFLICT",
                "This session_id already exists for a different device",
            )
        if existing["manifest_fingerprint"] != request_hash:
            audit.log("session", "manifest_conflict", "failure",
                      device_id=ident.device_id, session_id=manifest.session_id,
                      source_ip=ident.source_ip, idempotency_key=idem_key)
            raise ApiError(
                "IDEMPOTENCY_CONFLICT",
                "This session already exists with a different manifest",
            )
        body = sessions.status_payload(manifest.session_id, verbose=False)
        body["created"] = False
        idempotency.record(idem_key, ident.device_id, "sessions.create", request_hash,
                           200, body)
        crypto.cache_session_key(manifest.session_id, session_key,
                                 settings.session_key_cache_seconds)
        return JSONResponse(status_code=200, content=body)

    ts = now_iso()
    manifest_json = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id, device_id, schema_version, mode, "
            "client_status, started_at, completed_at, audio_json, encryption_json, "
            "wrap_algorithm, wrap_ciphertext_b64, wrap_key_id, manifest_json, "
            "manifest_fingerprint, expected_chunks, state, ingest_confirmed, "
            "processing_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manifest.session_id, ident.device_id, manifest.schema_version,
                manifest.mode, manifest.status, manifest.started_at,
                manifest.completed_at,
                json.dumps(manifest.audio.model_dump(exclude_none=False), default=str),
                json.dumps(
                    {"algorithm": encryption.algorithm,
                     "server_key_wrap": {"algorithm": wrap.algorithm,
                                         "key_id": key_id}},
                    default=str,
                ),
                wrap.algorithm, wrap.ciphertext_b64, key_id, manifest_json,
                request_hash, len(chunk_specs), "RECEIVING", 0,
                json.dumps(manifest.processing or {}, default=str), ts, ts,
            ),
        )
        for spec in chunk_specs:
            conn.execute(
                "INSERT INTO manifest_chunks(session_id, sequence, file, nonce_b64, aad, "
                "plaintext_sha256, ciphertext_sha256, plaintext_size, ciphertext_size) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    manifest.session_id, spec["sequence"], spec["file"], spec["nonce_b64"],
                    spec["aad"], spec["plaintext_sha256"], spec["ciphertext_sha256"],
                    spec["plaintext_size"], spec["ciphertext_size"],
                ),
            )
        audit.log(
            "session", "created", "success", device_id=ident.device_id,
            session_id=manifest.session_id, identity=ident.device_id,
            auth_method=ident.auth_method, source_ip=ident.source_ip,
            idempotency_key=idem_key,
            detail={
                "mode": manifest.mode,
                "client_status": manifest.status,
                "expected_chunks": len(chunk_specs),
                "wrap_key_id": key_id,
                "started_at": manifest.started_at,
            },
            conn=conn,
        )
        body = {
            "session_id": manifest.session_id,
            "state": "RECEIVING",
            "created": True,
            "ingest_confirmed": False,
        }
        idempotency.record(idem_key, ident.device_id, "sessions.create", request_hash,
                           201, body, conn=conn)

    crypto.cache_session_key(manifest.session_id, session_key,
                             settings.session_key_cache_seconds)
    return JSONResponse(status_code=201, content=body)


def _validate_manifest_chunks(manifest: SessionManifest) -> list[dict[str, Any]]:
    if len(manifest.chunks) > settings.max_chunks_per_session:
        raise ApiError("INVALID_MANIFEST", "manifest lists too many chunks")
    seen: set[int] = set()
    nonces: dict[str, int] = {}
    specs: list[dict[str, Any]] = []
    for chunk in manifest.chunks:
        seq = chunk.sequence
        if not isinstance(seq, int) or not 0 <= seq <= MAX_SEQUENCE:
            raise ApiError("INVALID_MANIFEST", f"invalid chunk sequence {seq!r}")
        if seq in seen:
            raise ApiError("INVALID_MANIFEST", f"duplicate chunk sequence {seq}")
        seen.add(seq)
        if chunk.nonce_b64:
            previous = nonces.get(chunk.nonce_b64)
            if previous is not None:
                raise ApiError(
                    "NONCE_REUSE",
                    f"chunks {previous} and {seq} declare the same AES-GCM nonce; "
                    "reusing a nonce under one session key destroys its security",
                )
            nonces[chunk.nonce_b64] = seq
        for field in ("plaintext_sha256", "ciphertext_sha256"):
            value = getattr(chunk, field)
            if value is not None and not is_sha256_hex(value):
                raise ApiError(
                    "INVALID_MANIFEST",
                    f"chunk {seq}: {field} is not a SHA-256 hex digest",
                )
        if chunk.nonce_b64:
            try:
                b64decode_strict(chunk.nonce_b64)
            except ValueError as exc:
                raise ApiError(
                    "INVALID_MANIFEST", f"chunk {seq}: nonce_b64 is not valid base64"
                ) from exc
        specs.append(
            {
                "sequence": seq,
                "file": chunk.file,
                "nonce_b64": chunk.nonce_b64,
                "aad": chunk.aad,
                "plaintext_sha256": norm_hex(chunk.plaintext_sha256)
                if chunk.plaintext_sha256 else None,
                "ciphertext_sha256": norm_hex(chunk.ciphertext_sha256)
                if chunk.ciphertext_sha256 else None,
                "plaintext_size": chunk.plaintext_size,
                "ciphertext_size": chunk.ciphertext_size,
            }
        )
    return specs


def _manifest_fingerprint(
    manifest: SessionManifest, chunk_specs: list[dict[str, Any]], session_key: bytes
) -> str:
    """A stable identity for a session manifest.

    Deliberately excludes the key-wrap ciphertext (non-deterministic) and any
    forward-compatible extra fields, and includes a hash of the unwrapped
    session key so a retry that re-wraps a *different* key is still a conflict.
    """
    return canonical_hash(
        {
            "schema_version": manifest.schema_version,
            "session_id": manifest.session_id,
            "device_id": manifest.device_id,
            "mode": manifest.mode,
            "status": manifest.status,
            "started_at": manifest.started_at,
            "completed_at": manifest.completed_at,
            "audio": manifest.audio.model_dump(exclude_none=False),
            "encryption_algorithm": manifest.encryption.algorithm,
            "wrap_algorithm": (manifest.encryption.server_key_wrap.algorithm
                               if manifest.encryption.server_key_wrap else None),
            "session_key_sha256": sha256_hex(session_key),
            "chunks": chunk_specs,
        }
    )


def _short_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.append(f"{loc}: {err.get('msg')}")
    return out


# ---------------------------------------------------------------------------
# PUT /v1/sessions/{session_id}/chunks/{sequence}
# ---------------------------------------------------------------------------

@router.put("/sessions/{session_id}/chunks/{sequence}")
async def put_chunk(session_id: str, sequence: int, request: Request) -> Response:
    ident = _identity(request)
    _require_uploads_enabled(ident)
    session = _owned_session(session_id, ident)
    idem_key = idempotency.key_of(request.headers)

    declared_ct_sha = (request.headers.get("x-chunk-sha256") or "").strip().lower()
    nonce_b64 = (request.headers.get("x-chunk-nonce") or "").strip()
    aad_text = request.headers.get("x-chunk-aad")
    declared_pt_sha = (request.headers.get("x-plaintext-sha256") or "").strip().lower()

    if not is_sha256_hex(declared_ct_sha):
        raise ApiError("INVALID_REQUEST", "X-Chunk-SHA256 must be a SHA-256 hex digest")
    if not is_sha256_hex(declared_pt_sha):
        raise ApiError("INVALID_REQUEST", "X-Plaintext-SHA256 must be a SHA-256 hex digest")
    if not nonce_b64:
        raise ApiError("INVALID_REQUEST", "X-Chunk-Nonce is required")
    if aad_text is None:
        raise ApiError("INVALID_REQUEST", "X-Chunk-AAD is required")
    try:
        nonce = b64decode_strict(nonce_b64)
    except ValueError as exc:
        raise ApiError("INVALID_REQUEST", f"X-Chunk-Nonce is not valid base64: {exc}") from exc
    # AAD is used byte-for-byte: no trimming, no normalisation, no re-serialising.
    aad = _raw_header(request, "x-chunk-aad")
    if aad is None:
        raise ApiError("INVALID_REQUEST", "X-Chunk-AAD is required")
    aad_sha = sha256_hex(aad)
    # Stored for display; the hash above is what identity comparisons use.
    aad_text = aad.decode("utf-8", errors="replace")

    spec = db.row_to_dict(
        db.query_one(
            "SELECT * FROM manifest_chunks WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        )
    )
    if spec is None:
        audit.log("chunk", "not_in_manifest", "failure", device_id=ident.device_id,
                  session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                  idempotency_key=idem_key)
        raise ApiError(
            "CHUNK_NOT_IN_MANIFEST",
            f"Sequence {sequence} is not listed in the session manifest",
        )

    body = await _read_capped(request, settings.max_chunk_bytes, "Chunk")
    if not body:
        raise ApiError("INVALID_REQUEST", "Chunk body is empty")

    # --- 1. ciphertext integrity, before anything is trusted --------------
    actual_ct_sha = sha256_hex(body)
    if actual_ct_sha != declared_ct_sha:
        audit.log("integrity", "ciphertext_hash_mismatch", "failure",
                  device_id=ident.device_id, session_id=session_id, sequence=sequence,
                  source_ip=ident.source_ip, idempotency_key=idem_key,
                  detail={"declared": declared_ct_sha, "computed": actual_ct_sha,
                          "bytes": len(body)})
        raise ApiError(
            "CHUNK_HASH_MISMATCH",
            "Ciphertext SHA-256 does not match X-Chunk-SHA256",
        )

    # --- 2. nonce reuse ---------------------------------------------------
    # Two chunks encrypted with the same nonce under one session key is a total
    # break of AES-GCM: the plaintexts XOR out and the authentication subkey
    # falls. The server is the only party able to notice a recorder with a
    # broken RNG or a restarted counter, so it refuses the upload loudly.
    reused = db.query_one(
        "SELECT sequence FROM chunks WHERE session_id = ? AND nonce_b64 = ? "
        "AND sequence != ?", (session_id, nonce_b64, sequence))
    if reused is not None:
        audit.log("security", "nonce_reuse", "failure", device_id=ident.device_id,
                  session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                  idempotency_key=idem_key,
                  detail={"nonce_already_used_by_sequence": int(reused["sequence"])})
        raise ApiError(
            "NONCE_REUSE",
            f"This nonce was already used by chunk {int(reused['sequence'])} in this "
            "session; reusing an AES-GCM nonce under one key destroys its security",
        )

    # --- 3. idempotent retry ---------------------------------------------
    # Checked before the manifest cross-check so that re-sending altered bytes
    # under an existing sequence is reported as the conflict it is, rather than
    # as a generic hash mismatch.
    existing = db.row_to_dict(
        db.query_one(
            "SELECT * FROM chunks WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        )
    )
    if existing is not None:
        identical = (
            existing["ciphertext_sha256"] == actual_ct_sha
            and existing["plaintext_sha256"] == declared_pt_sha
            and existing["nonce_b64"] == nonce_b64
            and (existing["aad_sha256"] or sha256_hex(
                (existing["aad"] or "").encode("utf-8"))) == aad_sha
        )
        if identical:
            audit.log("chunk", "duplicate_accepted", "success", device_id=ident.device_id,
                      session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                      idempotency_key=idem_key, auth_method=ident.auth_method)
            return JSONResponse(status_code=200, content=_chunk_response(existing, duplicate=True))
        audit.log("integrity", "chunk_conflict", "failure", device_id=ident.device_id,
                  session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                  idempotency_key=idem_key,
                  detail={
                      "stored_ciphertext_sha256": existing["ciphertext_sha256"],
                      "uploaded_ciphertext_sha256": actual_ct_sha,
                      "nonce_changed": existing["nonce_b64"] != nonce_b64,
                      "aad_changed": existing["aad_sha256"] != aad_sha,
                  })
        raise ApiError(
            "CHUNK_CONFLICT",
            "This chunk already exists with different content or metadata",
        )

    if session["ingest_confirmed"]:
        raise ApiError(
            "SESSION_ALREADY_FINALIZED",
            "Ingest for this session is already confirmed",
        )

    # --- 4. agreement with the manifest -----------------------------------
    if spec["ciphertext_sha256"] and spec["ciphertext_sha256"] != actual_ct_sha:
        audit.log("integrity", "manifest_ciphertext_mismatch", "failure",
                  device_id=ident.device_id, session_id=session_id, sequence=sequence,
                  source_ip=ident.source_ip,
                  detail={"manifest": spec["ciphertext_sha256"], "uploaded": actual_ct_sha})
        raise ApiError(
            "CHUNK_HASH_MISMATCH",
            "Uploaded ciphertext does not match the hash declared in the manifest",
        )
    if spec["plaintext_sha256"] and spec["plaintext_sha256"] != declared_pt_sha:
        audit.log("integrity", "manifest_plaintext_mismatch", "failure",
                  device_id=ident.device_id, session_id=session_id, sequence=sequence,
                  source_ip=ident.source_ip)
        raise ApiError(
            "CHUNK_HASH_MISMATCH",
            "X-Plaintext-SHA256 does not match the hash declared in the manifest",
        )

    # --- 5. decrypt and authenticate --------------------------------------
    key = _session_key(session)
    try:
        plaintext = await asyncio.to_thread(crypto.decrypt_chunk, key, nonce, aad, body)
    except crypto.DecryptError as exc:
        audit.log("security", "chunk_decrypt_failed", "failure", device_id=ident.device_id,
                  session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                  idempotency_key=idem_key,
                  detail={"reason": str(exc), "nonce_bytes": len(nonce),
                          "aad_bytes": len(aad), "ciphertext_bytes": len(body)})
        raise ApiError("CHUNK_DECRYPT_FAILED", str(exc)) from exc

    # --- 6. plaintext integrity -------------------------------------------
    actual_pt_sha = sha256_hex(plaintext)
    if actual_pt_sha != declared_pt_sha:
        audit.log("integrity", "plaintext_hash_mismatch", "failure",
                  device_id=ident.device_id, session_id=session_id, sequence=sequence,
                  source_ip=ident.source_ip, idempotency_key=idem_key,
                  detail={"declared": declared_pt_sha, "computed": actual_pt_sha})
        raise ApiError(
            "PLAINTEXT_HASH_MISMATCH",
            "Decrypted SHA-256 does not match X-Plaintext-SHA256",
        )

    # --- 7. the plaintext must really be FLAC ------------------------------
    try:
        info = await asyncio.to_thread(
            flacinfo.validate, plaintext, settings.flac_deep_verify,
            settings.max_decoded_bytes,
        )
    except flacinfo.FlacError as exc:
        audit.log("integrity", "invalid_flac", "failure", device_id=ident.device_id,
                  session_id=session_id, sequence=sequence, source_ip=ident.source_ip,
                  idempotency_key=idem_key, detail={"reason": str(exc)})
        raise ApiError("INVALID_FLAC", f"Decrypted payload is not valid FLAC: {exc}") from exc

    try:
        audio_spec = json.loads(session.get("audio_json") or "{}")
    except (ValueError, TypeError):
        audio_spec = {}
    conflicts = flacinfo.check_against_manifest(info, audio_spec)
    if conflicts:
        audit.log("integrity", "audio_manifest_conflict", "failure",
                  device_id=ident.device_id, session_id=session_id, sequence=sequence,
                  source_ip=ident.source_ip, detail={"conflicts": conflicts})
        raise ApiError("INVALID_FLAC", "; ".join(conflicts))

    if spec["plaintext_size"] is not None and int(spec["plaintext_size"]) != len(plaintext):
        info.warnings.append(
            f"manifest declares plaintext_size {spec['plaintext_size']} "
            f"but the decrypted chunk is {len(plaintext)} bytes"
        )

    # --- 8. durable storage, then the database row -------------------------
    blob = storage.chunk_path(session_id, sequence)
    await asyncio.to_thread(storage.write_durable, blob, body)
    plaintext_blob: str | None = None
    if settings.store_plaintext:
        pt_path = storage.plaintext_path(session_id, sequence)
        await asyncio.to_thread(storage.write_durable, pt_path, plaintext)
        plaintext_blob = str(pt_path)

    received_at = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO chunks(session_id, sequence, ciphertext_sha256, plaintext_sha256, "
            "nonce_b64, aad, aad_sha256, ciphertext_size, plaintext_size, blob_path, "
            "plaintext_blob_path, ciphertext_verified, decrypt_verified, "
            "plaintext_verified, flac_valid, flac_deep_verified, flac_json, received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,1,1,?,?,?) "
            "ON CONFLICT(session_id, sequence) DO NOTHING",
            (
                session_id, sequence, actual_ct_sha, actual_pt_sha, nonce_b64, aad_text,
                aad_sha, len(body), len(plaintext), str(blob), plaintext_blob,
                1 if info.deep_verified else 0,
                json.dumps(info.to_dict(), default=str), received_at,
            ),
        )
        audit.log(
            "chunk", "received", "success", device_id=ident.device_id,
            session_id=session_id, sequence=sequence, identity=ident.device_id,
            auth_method=ident.auth_method, source_ip=ident.source_ip,
            idempotency_key=idem_key,
            detail={
                "ciphertext_sha256": actual_ct_sha,
                "plaintext_sha256": actual_pt_sha,
                "ciphertext_bytes": len(body),
                "plaintext_bytes": len(plaintext),
                "flac": {
                    "sample_rate": info.sample_rate,
                    "channels": info.channels,
                    "bits_per_sample": info.bits_per_sample,
                    "duration_ms": info.duration_ms,
                    "deep_verified": info.deep_verified,
                    "md5_verified": info.md5_verified,
                },
            },
            conn=conn,
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (received_at, session_id),
        )

    # A late chunk can complete a session whose `complete` call already arrived.
    if session["complete_requested"]:
        sessions.evaluate(session_id)

    stored = db.row_to_dict(
        db.query_one(
            "SELECT * FROM chunks WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        )
    )
    return JSONResponse(status_code=200, content=_chunk_response(stored or {}))


def _chunk_response(row: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
    try:
        info = json.loads(row.get("flac_json") or "{}")
    except (ValueError, TypeError):
        info = {}
    return {
        "session_id": row.get("session_id"),
        "sequence": row.get("sequence"),
        "accepted": True,
        "duplicate": duplicate,
        "ciphertext_sha256_verified": bool(row.get("ciphertext_verified")),
        "plaintext_sha256_verified": bool(row.get("plaintext_verified")),
        "decrypt_verified": bool(row.get("decrypt_verified")),
        "flac_valid": bool(row.get("flac_valid")),
        "flac_deep_verified": bool(row.get("flac_deep_verified")),
        "duration_ms": info.get("duration_ms"),
        "ciphertext_size": row.get("ciphertext_size"),
        "plaintext_size": row.get("plaintext_size"),
    }


# ---------------------------------------------------------------------------
# POST /v1/sessions/{session_id}/events
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/events")
async def post_events(session_id: str, request: Request) -> Response:
    ident = _identity(request)
    session = _owned_session(session_id, ident)
    idem_key = idempotency.key_of(request.headers)
    payload = await _json_body(request)
    request_hash = canonical_hash(payload)
    # Non-strict: the key may legitimately carry a longer event list on a later
    # attempt, and events are deduplicated on content anyway. Reusing the key
    # for a *different endpoint* is still a conflict.
    idempotency.check_replay(idem_key, ident.device_id, "sessions.events",
                             request_hash, strict=False)

    try:
        parsed = EventsRequest.model_validate(payload)
    except ValidationError as exc:
        raise ApiError("INVALID_REQUEST", "Events payload failed validation",
                       extra={"details": _short_errors(exc)}) from exc

    if len(parsed.events) > settings.max_events_per_request:
        raise ApiError("PAYLOAD_TOO_LARGE", "Too many events in one request")

    stored = 0
    duplicates = 0
    received_at = now_iso()
    with db.tx() as conn:
        for event in parsed.events:
            raw = event.model_dump(exclude_none=False)
            name = (raw.get("event") or "").strip()
            if not name or len(name) > 128:
                raise ApiError("INVALID_REQUEST", "event name is missing or too long")
            offset = raw.get("offset_ms")
            if offset is not None and (not isinstance(offset, int) or offset < 0):
                raise ApiError("INVALID_REQUEST", f"{name}: offset_ms must be a non-negative integer")
            patient_index = raw.get("patient_index")
            if patient_index is not None and not isinstance(patient_index, int):
                raise ApiError("INVALID_REQUEST", f"{name}: patient_index must be an integer")
            # Deduplicate on the full event content, so an identical retry can
            # never add a second copy while a genuinely new event still lands.
            dedup = canonical_hash(raw)
            cursor = conn.execute(
                "INSERT INTO events(session_id, event, offset_ms, patient_index, at, "
                "payload_json, dedup_hash, received_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id, dedup_hash) DO NOTHING",
                (
                    session_id, name, offset, patient_index, raw.get("at"),
                    json.dumps(raw, ensure_ascii=False, default=str), dedup, received_at,
                ),
            )
            if cursor.rowcount:
                stored += 1
            else:
                duplicates += 1

        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                     (received_at, session_id))
        audit.log(
            "event", "received", "success", device_id=ident.device_id,
            session_id=session_id, identity=ident.device_id,
            auth_method=ident.auth_method, source_ip=ident.source_ip,
            idempotency_key=idem_key,
            detail={"submitted": len(parsed.events), "stored": stored,
                    "duplicates": duplicates,
                    "types": sorted({(e.event or "") for e in parsed.events})[:20]},
            conn=conn,
        )

    total = db.query_one(
        "SELECT COUNT(*) AS n FROM events WHERE session_id = ?", (session_id,)
    )
    if session["complete_requested"]:
        sessions.evaluate(session_id)
    body = {
        "session_id": session_id,
        "accepted": True,
        "submitted": len(parsed.events),
        "stored": stored,
        "duplicates": duplicates,
        "total_events": int(total["n"]) if total else stored,
    }
    idempotency.record(idem_key, ident.device_id, "sessions.events", request_hash,
                       200, body)
    return JSONResponse(status_code=200, content=body)


# ---------------------------------------------------------------------------
# POST /v1/sessions/{session_id}/complete
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/complete")
async def complete_session(session_id: str, request: Request) -> Response:
    ident = _identity(request)
    session = _owned_session(session_id, ident)
    idem_key = idempotency.key_of(request.headers)
    payload = await _json_body(request)
    idempotency.check_replay(idem_key, ident.device_id, "sessions.complete",
                             canonical_hash(payload), strict=False)

    try:
        parsed = CompleteRequest.model_validate(payload)
    except ValidationError as exc:
        raise ApiError("INVALID_REQUEST", "Complete payload failed validation",
                       extra={"details": _short_errors(exc)}) from exc

    status = (parsed.status or session["client_status"] or "complete").strip().lower()
    if status not in ("complete", "interrupted"):
        raise ApiError("INVALID_REQUEST", "status must be 'complete' or 'interrupted'")
    if parsed.chunk_count is not None and (
        not isinstance(parsed.chunk_count, int) or parsed.chunk_count < 0
    ):
        raise ApiError("INVALID_REQUEST", "chunk_count must be a non-negative integer")

    # `complete` is re-evaluated on every call rather than replayed: the second
    # call in a recovery flow deliberately arrives after a missing chunk was
    # finally uploaded and must report the new state.
    with db.tx() as conn:
        conn.execute(
            "UPDATE sessions SET complete_requested = 1, complete_chunk_count = ?, "
            "client_status = ?, completed_at = COALESCE(?, completed_at), updated_at = ? "
            "WHERE session_id = ?",
            (parsed.chunk_count, status, parsed.completed_at, now_iso(), session_id),
        )
        audit.log(
            "session", "complete_requested", "success", device_id=ident.device_id,
            session_id=session_id, identity=ident.device_id,
            auth_method=ident.auth_method, source_ip=ident.source_ip,
            idempotency_key=idem_key,
            detail={"declared_chunk_count": parsed.chunk_count,
                    "client_status": status,
                    "completed_at": parsed.completed_at},
            conn=conn,
        )

    result = sessions.evaluate(session_id)
    body = {
        "session_id": session_id,
        "state": result.get("state"),
        "ingest_confirmed": result.get("ingest_confirmed", False),
        "expected_chunks": result.get("expected_chunks"),
        "received_chunks": result.get("received_chunks"),
        "missing_chunks": result.get("missing_chunks", []),
        "unverified_chunks": result.get("unverified_chunks", []),
        "client_status": status,
    }
    if result.get("error"):
        body["error"] = result["error"]
    idempotency.record(idem_key, ident.device_id, "sessions.complete",
                       canonical_hash(payload), 200, body)
    if body["ingest_confirmed"]:
        audit.log("session", "ingest_confirmed", "success", device_id=ident.device_id,
                  session_id=session_id, auth_method=ident.auth_method,
                  source_ip=ident.source_ip,
                  detail={"chunks": result.get("received_chunks"),
                          "client_status": status})
    return JSONResponse(status_code=200, content=body)


# ---------------------------------------------------------------------------
# GET /v1/sessions/{session_id}/status
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/status")
async def session_status(session_id: str, request: Request) -> Response:
    ident = _identity(request)
    _owned_session(session_id, ident)
    result = sessions.evaluate(session_id)
    result["segments"] = sessions.patient_segments(session_id) if result.get(
        "ingest_confirmed"
    ) else []
    audit.log("session", "status_read", "success", device_id=ident.device_id,
              session_id=session_id, auth_method=ident.auth_method,
              source_ip=ident.source_ip,
              detail={"state": result.get("state")})
    return JSONResponse(status_code=200, content=result)


# ---------------------------------------------------------------------------
# device endpoints
# ---------------------------------------------------------------------------

@router.get("/device/config")
async def device_config(request: Request) -> Response:
    ident = _identity(request)
    overrides = ident.config
    config = {
        "config_version": int(ident.row.get("config_version") or 1),
        "upload_enabled": bool(ident.row.get("upload_enabled", 1)),
        "chunk_seconds": settings.default_chunk_seconds,
        "minimum_battery_for_upload": settings.default_min_battery,
        "allowed_modes": list(SUPPORTED_MODES),
        "max_chunk_bytes": settings.max_chunk_bytes,
        "schema_version": settings.schema_version,
        "server_time": now_iso(),
    }
    # Per-device overrides may tune operational values, never weaken the
    # security posture: encryption stays mandatory whatever an override says.
    for key, value in overrides.items():
        if key in ("encryption_required", "verify_tls", "allow_plaintext"):
            continue
        config[key] = value
    config["encryption_required"] = True
    key = crypto.active_key()
    if key:
        config["server_key"] = {
            "key_id": key["key_id"],
            "algorithm": key["algorithm"],
            "public_key_pem": key["public_pem"],
        }
    audit.log("device", "config_read", "success", device_id=ident.device_id,
              auth_method=ident.auth_method, source_ip=ident.source_ip,
              detail={"config_version": config["config_version"]})
    return JSONResponse(status_code=200, content=config)


@router.post("/device/heartbeat")
async def device_heartbeat(request: Request) -> Response:
    ident = _identity(request)
    payload = await _json_body(request, limit=64 * 1024)
    try:
        beat = HeartbeatRequest.model_validate(payload)
    except ValidationError as exc:
        raise ApiError("INVALID_REQUEST", "Heartbeat payload failed validation",
                       extra={"details": _short_errors(exc)}) from exc
    if beat.device_id and beat.device_id != ident.device_id:
        raise ApiError("INVALID_DEVICE",
                       "heartbeat device_id does not match the authenticated device")

    db.execute(
        "UPDATE devices SET software_version = COALESCE(?, software_version), "
        "battery_percent = COALESCE(?, battery_percent), "
        "queue_count = COALESCE(?, queue_count), "
        "storage_free_bytes = COALESCE(?, storage_free_bytes), "
        "last_recording_at = COALESCE(?, last_recording_at), "
        "network_state = COALESCE(?, network_state), updated_at = ? "
        "WHERE device_id = ?",
        (
            beat.software_version, beat.battery_percent, beat.queue_count,
            beat.storage_free_bytes, beat.last_recording_at, beat.network_state,
            now_iso(), ident.device_id,
        ),
    )
    audit.log("device", "heartbeat", "success", device_id=ident.device_id,
              auth_method=ident.auth_method, source_ip=ident.source_ip,
              detail={"software_version": beat.software_version,
                      "battery_percent": beat.battery_percent,
                      "queue_count": beat.queue_count,
                      "network_state": beat.network_state})
    return JSONResponse(
        status_code=200,
        content={
            "device_id": ident.device_id,
            "accepted": True,
            "server_time": now_iso(),
            "config_version": int(ident.row.get("config_version") or 1),
            "upload_enabled": bool(ident.row.get("upload_enabled", 1)),
        },
    )


@router.get("/server/public-key")
async def server_public_key(request: Request) -> Response:
    """Provisioning helper: the active RSA public key the recorder wraps with.

    Requires a valid device identity — the key itself is not secret, but there
    is no reason to publish server metadata to unauthenticated callers.
    """
    ident = _identity(request)
    key = crypto.active_key()
    if key is None:
        raise ApiError("NO_SERVER_KEY", "No server key is configured")
    audit.log("device", "public_key_read", "success", device_id=ident.device_id,
              auth_method=ident.auth_method, source_ip=ident.source_ip)
    return JSONResponse(
        status_code=200,
        content={
            "key_id": key["key_id"],
            "algorithm": key["algorithm"],
            "public_key_pem": key["public_pem"],
            "wrap": {"scheme": "RSA-OAEP", "hash": "SHA-256", "mgf1": "SHA-256",
                     "label": None, "plaintext_bytes": crypto.SESSION_KEY_BYTES},
        },
    )
