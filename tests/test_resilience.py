"""Durability, recovery and concurrency — the properties behind
`ingest_confirmed=true`."""
from __future__ import annotations

import concurrent.futures

from conftest import Recorder, make_flac, sha256_hex


def test_state_survives_a_restart(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.4, seed=1)
    rec.add_chunk(seconds=0.4, seed=2)
    rec.create()
    rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True

    # A fresh application instance over the same data directory.
    from fastapi.testclient import TestClient

    from app.api_app import create_app

    with TestClient(create_app()) as client:
        resp = client.get(f"/v1/sessions/{rec.session_id}/status",
                          headers={"X-Device-ID": "visitescribe-001"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ingest_confirmed"] is True
        assert body["received_chunks"] == 2
        assert body["verified_chunks"] == 2


def test_lost_response_is_safe_to_retry_everywhere(server):
    """Every write the recorder makes can be replayed without side effects."""
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=1)
    rec.add_chunk(seconds=0.3, seed=2)
    events = [{"event": "session_started", "offset_ms": 0},
              {"event": "marker", "offset_ms": 120}]

    for _ in range(3):
        assert rec.create().status_code in (200, 201)
        for c in rec.chunks:
            assert rec.put_chunk(c["sequence"]).status_code == 200
        assert rec.send_events(events).status_code == 200
        assert rec.complete().status_code == 200

    counts = {
        "sessions": server.db.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?",
            (rec.session_id,))["n"],
        "chunks": server.db.query_one(
            "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?",
            (rec.session_id,))["n"],
        "events": server.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE session_id = ?",
            (rec.session_id,))["n"],
    }
    assert counts == {"sessions": 1, "chunks": 2, "events": 2}
    assert rec.status().json()["ingest_confirmed"] is True


def test_concurrent_chunk_uploads(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(12):
        rec.add_chunk(seconds=0.2, seed=i)
    rec.create()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda c: rec.put_chunk(c["sequence"]).status_code,
                                rec.chunks))
    assert results == [200] * 12
    assert rec.complete().json()["ingest_confirmed"] is True
    assert rec.status().json()["verified_chunks"] == 12


def test_concurrent_duplicate_uploads_of_one_chunk(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=1)
    rec.create()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: rec.put_chunk(1).status_code, range(8)))
    assert all(c == 200 for c in codes)
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?",
        (rec.session_id,))["n"] == 1


def test_chunks_before_the_session_exists(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    assert rec.put_chunk(1).status_code == 404
    rec.create()
    assert rec.put_chunk(1).status_code == 200


def test_out_of_order_upload(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(5):
        rec.add_chunk(seconds=0.2, seed=i)
    rec.create()
    for seq in (5, 2, 4, 1, 3):
        assert rec.put_chunk(seq).status_code == 200
    assert rec.complete().json()["ingest_confirmed"] is True


def test_complete_before_any_chunk_then_recovery(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2, seed=1)
    rec.add_chunk(seconds=0.2, seed=2)
    rec.create()
    early = rec.complete()
    assert early.status_code == 200
    assert early.json()["ingest_confirmed"] is False
    assert early.json()["missing_chunks"] == [1, 2]

    rec.put_chunk(1)
    assert rec.status().json()["missing_chunks"] == [2]
    rec.put_chunk(2)
    # The late chunk alone flips the session, without another complete call.
    assert rec.status().json()["ingest_confirmed"] is True


def test_key_rotation_keeps_old_sessions_working(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3, seed=1)
    rec.add_chunk(seconds=0.3, seed=2)
    rec.create()
    rec.put_chunk(1)

    import app.crypto as crypto

    old_key_id = crypto.active_key()["key_id"]
    new_key_id = crypto.generate_key(server.settings.keys_dir, bits=2048, make_active=True)
    assert new_key_id != old_key_id
    crypto.drop_session_key(rec.session_id)  # force a real unwrap

    # the in-flight session, wrapped with the retired key, still completes
    assert rec.put_chunk(2).status_code == 200
    assert rec.complete().json()["ingest_confirmed"] is True

    # and a new session uses the new key
    fresh = Recorder(server)
    fresh.add_chunk(seconds=0.3, seed=3)
    assert fresh.create(fresh.manifest(
        wrapped_b64=fresh.wrapped_key_b64(crypto.active_key()["public_pem"]))).status_code == 201
    assert fresh.put_chunk(1).status_code == 200
    assert server.db.query_one(
        "SELECT wrap_key_id FROM sessions WHERE session_id = ?",
        (fresh.session_id,))["wrap_key_id"] == new_key_id


def test_corrupted_audio_inside_valid_encryption_is_caught(server):
    """The GCM tag proves transport integrity; deep FLAC decoding proves the
    audio itself is intact."""
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    good = make_flac(0.5, seed=5)
    damaged = bytearray(good)
    damaged[len(good) // 2] ^= 0xFF   # corrupt an audio frame, not the header
    rec.add_chunk(flac=bytes(damaged))
    assert rec.create().status_code == 201
    resp = rec.put_chunk(1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FLAC"


def test_many_chunks(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(60):
        rec.add_chunk(seconds=0.1, seed=i)
    rec.create()
    rec.upload_all()
    body = rec.complete().json()
    assert body["ingest_confirmed"] is True
    assert rec.status().json()["verified_chunks"] == 60


def test_blobs_match_what_was_uploaded(server):
    import app.storage as storage

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(4):
        rec.add_chunk(seconds=0.2, seed=i)
    rec.create()
    rec.upload_all()
    rec.complete()
    for chunk in rec.chunks:
        data = storage.chunk_path(rec.session_id, chunk["sequence"]).read_bytes()
        assert sha256_hex(data) == chunk["ciphertext_sha256"]


def test_plaintext_is_not_stored_by_default(server):
    import app.storage as storage

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.create()
    rec.upload_all()
    assert not storage.plaintext_path(rec.session_id, 1).exists()
    assert server.db.query_one(
        "SELECT plaintext_blob_path FROM chunks WHERE session_id = ?",
        (rec.session_id,))["plaintext_blob_path"] is None


def test_health_endpoints(server):
    assert server.client.get("/healthz").json()["status"] == "ok"
    ready = server.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["problems"] == []
    assert server.client.get("/").json()["schema_version"] == 2
