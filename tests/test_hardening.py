"""Regression tests for issues found in review — each one failed before its fix."""
from __future__ import annotations

import base64
import io
import secrets

import numpy as np
import soundfile as sf

from conftest import Recorder, make_flac, sha256_hex


def _silence_flac(seconds: float, rate: int = 48000) -> bytes:
    """Digital silence compresses ~4000:1 — the decompression-bomb shape."""
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(seconds * rate), dtype="int16"), rate,
             format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def test_flac_decompression_bomb_is_refused(server):
    """25 minutes of silence is ~220 KiB of FLAC but ~288 MB decoded."""
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    bomb = _silence_flac(25 * 60)
    assert len(bomb) < 1_000_000, "the bomb should be small on the wire"
    rec.add_chunk(flac=bomb)
    assert rec.create().status_code == 201
    resp = rec.put_chunk(1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FLAC"
    assert "limit" in resp.json()["error"]["message"]


def test_ordinary_silence_still_ingests(server):
    """The bomb limit must not reject a normal quiet chunk."""
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(flac=_silence_flac(30))
    assert rec.create().status_code == 201
    assert rec.put_chunk(1).status_code == 200
    assert rec.complete().json()["ingest_confirmed"] is True


def test_chunked_body_over_the_limit_is_cut_off(server, override_settings):
    """Content-Length is absent on a chunked request, so the cap must be
    enforced while streaming rather than after buffering."""
    rec_setup = Recorder(server)
    server.register_device("visitescribe-001")
    rec_setup.add_chunk(seconds=0.3)
    rec_setup.create()

    def generate():
        for _ in range(64):
            yield b"x" * 65536      # 4 MiB total, no Content-Length

    with override_settings(max_chunk_bytes=4096):
        resp = server.client.put(
            f"/v1/sessions/{rec_setup.session_id}/chunks/1",
            content=generate(),
            headers={"X-Device-ID": "visitescribe-001",
                     "X-Chunk-SHA256": "0" * 64, "X-Chunk-Nonce": "AAAAAAAAAAAAAAAA",
                     "X-Chunk-AAD": "x", "X-Plaintext-SHA256": "0" * 64})
    assert resp.status_code == 413


def test_nonce_reuse_within_a_session_is_refused(server):
    """Repeating a GCM nonce under one key is a total break of AES-GCM."""
    from app.crypto import encrypt_chunk_for_test

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=1)
    # Second chunk, deliberately reusing the first chunk's nonce.
    nonce_b64 = rec.chunks[0]["nonce_b64"]
    nonce = base64.b64decode(nonce_b64)
    plaintext = make_flac(0.3, seed=2)
    aad = f"{rec.session_id}|2|{rec.device_id}"
    ciphertext = encrypt_chunk_for_test(rec.session_key, nonce, aad.encode(), plaintext)
    rec.chunks.append({
        "sequence": 2, "file": "audio/chunk-000002.flac.enc",
        "nonce_b64": nonce_b64, "aad": aad,
        "plaintext_sha256": sha256_hex(plaintext),
        "ciphertext_sha256": sha256_hex(ciphertext),
        "plaintext_size": len(plaintext), "ciphertext_size": len(ciphertext),
        "_plaintext": plaintext, "_ciphertext": ciphertext,
    })
    # The manifest itself already exposes the reuse.
    resp = rec.create()
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NONCE_REUSE"


def test_nonce_reuse_at_upload_time_is_refused(server):
    """Even when the manifest hides it, the second upload is caught."""
    from app.crypto import encrypt_chunk_for_test

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=1)
    rec.add_chunk(seconds=0.3, seed=2)
    assert rec.create().status_code == 201
    assert rec.put_chunk(1).status_code == 200

    # Re-encrypt chunk 2 with chunk 1's nonce after the manifest was accepted.
    nonce = base64.b64decode(rec.chunks[0]["nonce_b64"])
    plaintext = rec.chunks[1]["_plaintext"]
    aad = rec.chunks[1]["aad"]
    ciphertext = encrypt_chunk_for_test(rec.session_key, nonce, aad.encode(), plaintext)
    resp = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/2", content=ciphertext,
        headers={"X-Device-ID": rec.device_id,
                 "X-Chunk-SHA256": sha256_hex(ciphertext),
                 "X-Chunk-Nonce": rec.chunks[0]["nonce_b64"],
                 "X-Chunk-AAD": aad,
                 "X-Plaintext-SHA256": sha256_hex(plaintext)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NONCE_REUSE"
    assert server.db.query(
        "SELECT * FROM audit WHERE action = 'nonce_reuse' AND outcome = 'failure'")


def test_non_ascii_aad_authenticates(server):
    """Header values arrive latin-1 decoded; the AAD must be the wire bytes."""
    from app.crypto import encrypt_chunk_for_test

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    plaintext = make_flac(0.3, seed=7)
    nonce = secrets.token_bytes(12)
    aad = f"{rec.session_id}|1|café–Ünïcode"          # genuinely non-ASCII
    aad_bytes = aad.encode("utf-8")
    ciphertext = encrypt_chunk_for_test(rec.session_key, nonce, aad_bytes, plaintext)
    rec.chunks.append({
        "sequence": 1, "file": "audio/chunk-000001.flac.enc",
        "nonce_b64": base64.b64encode(nonce).decode(), "aad": aad,
        "plaintext_sha256": sha256_hex(plaintext),
        "ciphertext_sha256": sha256_hex(ciphertext),
        "plaintext_size": len(plaintext), "ciphertext_size": len(ciphertext),
        "_plaintext": plaintext, "_ciphertext": ciphertext,
    })
    assert rec.create().status_code == 201
    resp = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/1", content=ciphertext,
        headers={"X-Device-ID": rec.device_id,
                 "X-Chunk-SHA256": sha256_hex(ciphertext),
                 "X-Chunk-Nonce": base64.b64encode(nonce).decode(),
                 "X-Chunk-AAD": aad_bytes,      # exact bytes, as the Pi sends them
                 "X-Plaintext-SHA256": sha256_hex(plaintext)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["decrypt_verified"] is True
    # ...and the retry of the same chunk is still recognised as a duplicate
    resp2 = server.client.put(
        f"/v1/sessions/{rec.session_id}/chunks/1", content=ciphertext,
        headers={"X-Device-ID": rec.device_id,
                 "X-Chunk-SHA256": sha256_hex(ciphertext),
                 "X-Chunk-Nonce": base64.b64encode(nonce).decode(),
                 "X-Chunk-AAD": aad_bytes,
                 "X-Plaintext-SHA256": sha256_hex(plaintext)})
    assert resp2.status_code == 200
    assert resp2.json()["duplicate"] is True


def test_paused_uploads_are_actually_refused(server):
    server.register_device("visitescribe-001")
    server.admin_login()
    server.admin.post("/admin/api/devices/visitescribe-001",
                      json={"upload_enabled": False})
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    resp = rec.create()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_UPLOADS_PAUSED"
    # the device can still learn that it has been paused
    cfg = server.client.get("/v1/device/config", headers=rec.headers())
    assert cfg.status_code == 200
    assert cfg.json()["upload_enabled"] is False

    server.admin.post("/admin/api/devices/visitescribe-001",
                      json={"upload_enabled": True})
    assert rec.create().status_code == 201


def test_purging_source_audio_clears_ingest_confirmed(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.create()
    rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True

    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/purge",
                      json={"scope": "source_audio"})
    # The promise cannot survive the audio it was about.
    row = server.db.query_one(
        "SELECT ingest_confirmed FROM sessions WHERE session_id = ?", (rec.session_id,))
    assert row["ingest_confirmed"] == 0
    assert server.admin.get(
        f"/admin/api/sessions/{rec.session_id}").json()["session"]["ingest_confirmed"] is False


def test_admin_state_is_not_reverted_by_the_device(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.create()
    rec.upload_all()
    rec.complete()

    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/state",
                      json={"state": "POLICY_BLOCKED"})
    body = rec.status().json()
    assert body["state"] == "POLICY_BLOCKED"
    # the data really is durably stored, so the recorder is still told so
    assert body["ingest_confirmed"] is True


def test_out_of_range_patient_boundary_cannot_invert_a_segment(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient")
    rec.add_chunk(seconds=1.0)
    rec.create()
    rec.upload_all()
    rec.send_events([
        {"event": "patient_boundary", "offset_ms": 999999999},
        {"event": "patient_boundary", "offset_ms": 500},
    ])
    rec.complete()
    segments = rec.status().json()["segments"]
    for seg in segments:
        assert seg["start_ms"] >= 0
        if seg["duration_ms"] is not None:
            assert seg["duration_ms"] > 0, seg
        if seg["end_ms"] is not None:
            assert seg["end_ms"] > seg["start_ms"], seg
    assert [s["start_ms"] for s in segments] == [0, 500]


def test_absurd_manifest_sequence_is_a_client_error(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    manifest = rec.manifest()
    manifest["chunks"][0]["sequence"] = 2**70
    resp = rec.create(manifest)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_MANIFEST"


def test_forwarded_ip_is_not_trusted_from_an_untrusted_peer(server):
    """The audit trail must not record an address the caller chose."""
    from app.auth import _is_trusted_peer

    assert _is_trusted_peer("10.42.0.1") is True
    assert _is_trusted_peer("127.0.0.1") is True
    assert _is_trusted_peer("192.168.1.9") is True
    assert _is_trusted_peer("8.8.8.8") is False
    assert _is_trusted_peer("not-an-ip") is False


def test_copy_button_markup_is_well_formed(server):
    server.admin_login()
    page = server.admin.get("/admin/keys").text
    assert 'onclick="copy("' not in page, "attribute terminated early"
    assert "BEGIN PUBLIC KEY" in page
    assert page.count("<button") == page.count("</button>")


def test_health_stays_up_and_readyz_reports_startup_problems(server):
    """A broken data directory must be diagnosable, not a silent crash loop.

    If the container exits, the pod never turns Ready and the Olares installer
    sits on "Installing" with nothing to show. So /healthz answers as soon as
    the process is serving, and /readyz carries the diagnosis at 200 so the
    probe does not hold the install open.
    """
    import app.bootstrap as bootstrap

    assert server.client.get("/healthz").json()["status"] == "ok"
    ready = server.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert ready.json()["startup"]["data_dir"]
    assert "uid" in ready.json()["startup"]["running_as"]

    bootstrap.STARTUP_PROBLEMS.append("/data is not writable by uid 1000:gid 1000")
    try:
        assert server.client.get("/healthz").status_code == 200
        ready = server.client.get("/readyz")
        assert ready.status_code == 200, "a probe failure would hide the diagnosis"
        assert ready.json()["status"] == "degraded"
        assert any("not writable" in p for p in ready.json()["problems"])

        server.admin_login()
        page = server.admin.get("/admin/")
        assert page.status_code == 200
        assert "Startup problem" in page.text
        assert "not writable" in page.text
    finally:
        bootstrap.STARTUP_PROBLEMS.clear()
