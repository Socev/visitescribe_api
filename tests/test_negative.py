"""The negative tests from section 25, plus the security posture."""
from __future__ import annotations

import base64
import secrets

import pytest

from conftest import Recorder, make_flac, sha256_hex


def _prepared(server, **kw) -> Recorder:
    server.register_device("visitescribe-001")
    rec = Recorder(server, **kw)
    rec.add_chunk(seconds=0.3, seed=1)
    rec.add_chunk(seconds=0.3, seed=2)
    assert rec.create().status_code == 201
    return rec


# --------------------------------------------------------------------- device
def test_unknown_device_is_rejected(server):
    rec = Recorder(server, device_id="ghost-999")
    rec.add_chunk(seconds=0.2)
    resp = rec.create()
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_DEVICE"


def test_missing_device_header(server):
    resp = server.client.post("/v1/sessions", json={"schema_version": 2})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_DEVICE"


def test_disabled_device_cannot_upload(server):
    server.register_device("visitescribe-001")
    server.db.execute("UPDATE devices SET enabled = 0 WHERE device_id = ?",
                      ("visitescribe-001",))
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    resp = rec.create()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_DISABLED"


def test_device_requiring_token_refuses_header_only(server):
    from app.util import token_hash

    server.register_device("visitescribe-001", allow_header_only=False,
                           token_hash_value=token_hash("s3cret"), token_hint="s3cre…")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    assert rec.create().status_code == 401

    ok = server.client.post(
        "/v1/sessions", json=rec.manifest(),
        headers={"X-Device-ID": "visitescribe-001",
                 "Idempotency-Key": f"{rec.session_id}:create",
                 "Authorization": "Bearer s3cret"})
    assert ok.status_code == 201


def test_wrong_token_is_rejected(server):
    from app.util import token_hash

    server.register_device("visitescribe-001", allow_header_only=False,
                           token_hash_value=token_hash("right"), token_hint="right")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    resp = server.client.post(
        "/v1/sessions", json=rec.manifest(),
        headers={"X-Device-ID": "visitescribe-001",
                 "Idempotency-Key": "x:create",
                 "Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_pinned_certificate_required_when_set(server):
    server.register_device("visitescribe-001")
    server.db.execute("UPDATE devices SET cert_fingerprint = ? WHERE device_id = ?",
                      ("ab" * 32, "visitescribe-001"))
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    resp = rec.create()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_CERT_MISMATCH"


def test_session_from_another_device_is_refused(server):
    rec = _prepared(server)
    server.register_device("visitescribe-002")
    other = Recorder(server, device_id="visitescribe-002", session_id=rec.session_id)
    other.session_key = rec.session_key
    other.chunks = rec.chunks
    resp = other.create()
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SESSION_DEVICE_CONFLICT"


def test_other_device_cannot_read_or_write_a_session(server):
    rec = _prepared(server)
    server.register_device("visitescribe-002")
    headers = {"X-Device-ID": "visitescribe-002"}
    st = server.client.get(f"/v1/sessions/{rec.session_id}/status", headers=headers)
    assert st.status_code == 403
    assert st.json()["error"]["code"] == "DEVICE_NOT_OWNER"

    chunk = rec.chunks[0]
    put = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/1", content=chunk["_ciphertext"],
        headers={**headers, "X-Chunk-SHA256": chunk["ciphertext_sha256"],
                 "X-Chunk-Nonce": chunk["nonce_b64"], "X-Chunk-AAD": chunk["aad"],
                 "X-Plaintext-SHA256": chunk["plaintext_sha256"]})
    assert put.status_code == 403


def test_manifest_device_id_must_match_authenticated_device(server):
    server.register_device("visitescribe-001")
    server.register_device("visitescribe-002")
    rec = Recorder(server, device_id="visitescribe-001")
    rec.add_chunk(seconds=0.2)
    manifest = rec.manifest()
    manifest["device_id"] = "visitescribe-002"
    resp = rec.create(manifest)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_MANIFEST"


# ------------------------------------------------------------------ manifest
def test_wrong_schema_version(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    resp = rec.create(rec.manifest(schema_version=1))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_SCHEMA_VERSION"


def test_a_malformed_mode_is_still_rejected(server):
    server.register_device("visitescribe-001")
    for bad in ("Karaoke", "ka", "with spaces", "x" * 40, "../etc", ""):
        rec = Recorder(server, mode=bad)
        rec.add_chunk(seconds=0.2)
        resp = rec.create()
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "INVALID_MANIFEST"


def test_a_new_recording_type_is_accepted_and_registered(server):
    """Room for a future button on the recorder.

    A mode the server has never heard of is not a client error -- the hardware
    is allowed to grow without waiting for a server release. It is registered
    as a recording type, and registered as CARRYING PATIENT AUDIO, so the
    routing policy treats the unknown case in the strictest way rather than
    the loosest.
    """
    from app import routing, users

    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="mdo")
    rec.add_chunk(seconds=0.2)
    assert rec.create().status_code == 201

    types = {t["mode"]: t for t in users.recording_types()}
    assert "mdo" in types
    assert types["mdo"]["builtin"] == 0
    assert types["mdo"]["patient_audio"] == 1
    assert routing.carries_patient_audio("mdo") is True


def test_bad_rsa_key_wrap(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    manifest = rec.manifest()
    manifest["encryption"]["server_key_wrap"]["ciphertext_b64"] = base64.b64encode(
        b"not really an rsa ciphertext" * 8).decode()
    resp = rec.create(manifest)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_KEY_WRAP"


def test_key_wrap_that_yields_wrong_length(server):
    """A syntactically valid wrap of something that is not a 256-bit key."""
    from app.crypto import wrap_session_key_for_test

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    short = base64.b64encode(
        wrap_session_key_for_test(secrets.token_bytes(16), server.public_pem)
    ).decode()
    resp = rec.create(rec.manifest(wrapped_b64=short))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_KEY_WRAP"
    assert "32" in resp.json()["error"]["message"]


def test_wrap_with_a_foreign_public_key(server):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.crypto import wrap_session_key_for_test

    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = foreign.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    resp = rec.create(rec.manifest(wrapped_b64=base64.b64encode(
        wrap_session_key_for_test(rec.session_key, pem)).decode()))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_KEY_WRAP"


def test_wrong_encryption_algorithm(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    manifest = rec.manifest()
    manifest["encryption"]["algorithm"] = "AES-128-CBC"
    resp = rec.create(manifest)
    assert resp.status_code == 422


def test_duplicate_sequence_in_manifest(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    manifest = rec.manifest()
    manifest["chunks"].append(dict(manifest["chunks"][0]))
    resp = rec.create(manifest)
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["error"]["message"]


# -------------------------------------------------------------------- chunks
def test_ciphertext_hash_mismatch(server):
    rec = _prepared(server)
    resp = rec.put_chunk(1, ct_sha="0" * 64)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_HASH_MISMATCH"
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,))["n"] == 0


def test_corrupted_ciphertext_body(server):
    rec = _prepared(server)
    body = bytearray(rec.chunks[0]["_ciphertext"])
    body[5] ^= 0x01
    resp = rec.put_chunk(1, body=bytes(body))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_HASH_MISMATCH"


def test_tampered_ciphertext_caught_against_the_manifest(server):
    """The manifest declares the ciphertext hash, so tampering is caught early."""
    rec = _prepared(server)
    body = bytearray(rec.chunks[0]["_ciphertext"])
    body[10] ^= 0xFF
    body = bytes(body)
    resp = rec.put_chunk(1, body=body, ct_sha=sha256_hex(body))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_HASH_MISMATCH"


def test_gcm_authentication_failure(server):
    """With no hash in the manifest, the GCM tag is what rejects the forgery."""
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=4)
    manifest = rec.manifest()
    manifest["chunks"][0]["ciphertext_sha256"] = None
    assert rec.create(manifest).status_code == 201
    body = bytearray(rec.chunks[0]["_ciphertext"])
    body[10] ^= 0xFF
    body = bytes(body)
    resp = rec.put_chunk(1, body=body, ct_sha=sha256_hex(body))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_DECRYPT_FAILED"
    failures = server.db.query(
        "SELECT * FROM audit WHERE category = 'security' AND action = 'chunk_decrypt_failed'")
    assert failures, "a decrypt failure must leave a security audit record"


def test_wrong_nonce(server):
    rec = _prepared(server)
    wrong = base64.b64encode(secrets.token_bytes(12)).decode()
    resp = rec.put_chunk(1, nonce_b64=wrong)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_DECRYPT_FAILED"


def test_wrong_aad(server):
    rec = _prepared(server)
    resp = rec.put_chunk(1, aad=rec.chunks[0]["aad"] + "x")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_DECRYPT_FAILED"


def test_aad_is_used_byte_for_byte(server):
    """Whitespace is significant: a trimmed AAD must not authenticate."""
    rec = _prepared(server)
    resp = rec.put_chunk(1, aad=" " + rec.chunks[0]["aad"])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_DECRYPT_FAILED"


def test_plaintext_hash_mismatch(server):
    rec = _prepared(server)
    manifest_free = Recorder(server)
    # Build a session whose manifest carries a deliberately wrong plaintext hash
    # so the header can disagree with the real decrypted content.
    manifest_free.add_chunk(seconds=0.3, seed=9)
    manifest = manifest_free.manifest()
    manifest["chunks"][0]["plaintext_sha256"] = None
    assert manifest_free.create(manifest).status_code == 201
    resp = manifest_free.put_chunk(1, pt_sha="1" * 64)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PLAINTEXT_HASH_MISMATCH"


def test_chunk_not_in_manifest(server):
    rec = _prepared(server)
    chunk = rec.chunks[0]
    resp = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/99", content=chunk["_ciphertext"],
        headers={"X-Device-ID": rec.device_id,
                 "X-Chunk-SHA256": chunk["ciphertext_sha256"],
                 "X-Chunk-Nonce": chunk["nonce_b64"], "X-Chunk-AAD": chunk["aad"],
                 "X-Plaintext-SHA256": chunk["plaintext_sha256"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CHUNK_NOT_IN_MANIFEST"


def test_invalid_flac_is_rejected(server):
    """Encryption is perfect, hashes agree, but the payload is not FLAC."""
    from app.crypto import encrypt_chunk_for_test

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    junk = b"ID3\x04\x00\x00" + secrets.token_bytes(4000)
    rec.add_chunk(flac=junk)
    assert rec.create().status_code == 201
    resp = rec.put_chunk(1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FLAC"
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,))["n"] == 0


def test_truncated_flac_is_rejected(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    good = make_flac(0.5)
    rec.add_chunk(flac=good[: len(good) // 2])
    assert rec.create().status_code == 201
    resp = rec.put_chunk(1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FLAC"


def test_audio_contradicting_the_manifest_is_rejected(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, sample_rate=48000)
    rec.add_chunk(flac=make_flac(0.3, sample_rate=16000))
    assert rec.create().status_code == 201
    resp = rec.put_chunk(1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FLAC"
    assert "sample_rate" in resp.json()["error"]["message"]


def test_missing_chunk_headers(server):
    rec = _prepared(server)
    resp = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/1",
        content=rec.chunks[0]["_ciphertext"],
        headers={"X-Device-ID": rec.device_id})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_oversized_chunk_is_refused(server, override_settings):
    rec = _prepared(server)
    with override_settings(max_chunk_bytes=16):
        resp = rec.put_chunk(1)
    assert resp.status_code == 413


# ------------------------------------------------------------------ sessions
def test_unknown_session(server):
    server.register_device("visitescribe-001")
    resp = server.client.get(
        "/v1/sessions/11111111-1111-1111-1111-111111111111/status",
        headers={"X-Device-ID": "visitescribe-001"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UNKNOWN_SESSION"


def test_idempotency_key_reused_with_different_payload(server):
    rec = _prepared(server)
    other = Recorder(server)
    other.add_chunk(seconds=0.2)
    resp = other.create(idem=f"{rec.session_id}:create")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_key_reused_across_endpoints(server):
    rec = _prepared(server)
    resp = rec.send_events([{"event": "marker", "offset_ms": 1}],
                           idem=f"{rec.session_id}:create")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_chunk_after_confirmed_ingest(server):
    rec = _prepared(server)
    rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True
    # An identical retry still succeeds — the recorder may not have seen the reply.
    assert rec.put_chunk(1).status_code == 200
    # But genuinely new content cannot slip in afterwards.
    new = Recorder(server)
    new.session_key = rec.session_key
    fresh = new.add_chunk(seconds=0.3, seed=77)
    resp = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/2", content=fresh["_ciphertext"],
        headers={"X-Device-ID": rec.device_id,
                 "X-Chunk-SHA256": fresh["ciphertext_sha256"],
                 "X-Chunk-Nonce": fresh["nonce_b64"], "X-Chunk-AAD": fresh["aad"],
                 "X-Plaintext-SHA256": fresh["plaintext_sha256"]})
    assert resp.status_code == 409


def test_malformed_json_body(server):
    server.register_device("visitescribe-001")
    resp = server.client.post("/v1/sessions", content=b"{not json",
                              headers={"X-Device-ID": "visitescribe-001",
                                       "Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_rate_limit(server, override_settings):
    import app.ratelimit as ratelimit

    ratelimit.reset()
    server.register_device("visitescribe-001")
    with override_settings(rate_limit_per_minute=1, rate_limit_burst=2):
        codes = [server.client.get("/v1/device/config",
                                   headers={"X-Device-ID": "visitescribe-001"}).status_code
                 for _ in range(6)]
    assert 429 in codes
    ratelimit.reset()


def test_errors_never_leak_key_material(server):
    rec = _prepared(server)
    rec.put_chunk(1, nonce_b64=base64.b64encode(b"0" * 12).decode())
    rows = server.db.query("SELECT detail_json FROM audit")
    blob = " ".join(r["detail_json"] for r in rows)
    assert rec.session_key.hex() not in blob
    assert base64.b64encode(rec.session_key).decode() not in blob
    assert "BEGIN PRIVATE KEY" not in blob
