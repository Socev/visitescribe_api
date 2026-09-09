"""The 20-step acceptance test from section 25 of the specification."""
from __future__ import annotations

import secrets

from conftest import Recorder, make_flac, sha256_hex


def test_full_acceptance_flow(server):
    """Steps 1-20, in order, in one session."""
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient")
    for i in range(3):
        rec.add_chunk(seconds=0.4, seed=i)

    # 1. a registered device creates a new session
    first = rec.create()
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    assert first.json()["state"] == "RECEIVING"
    assert first.json()["ingest_confirmed"] is False

    # 2-3. the same create is sent again and only one session exists
    again = rec.create()
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?", (rec.session_id,)
    )["n"] == 1

    # 4-8. an encrypted chunk is uploaded and every check passes
    put = rec.put_chunk(1)
    assert put.status_code == 200, put.text
    payload = put.json()
    assert payload["ciphertext_sha256_verified"] is True   # 5
    assert payload["decrypt_verified"] is True             # 6
    assert payload["plaintext_sha256_verified"] is True    # 7
    assert payload["flac_valid"] is True                   # 8
    assert payload["flac_deep_verified"] is True

    # 9-10. the identical chunk again produces no duplicate
    repeat = rec.put_chunk(1)
    assert repeat.status_code == 200
    assert repeat.json()["duplicate"] is True
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,)
    )["n"] == 1

    # 11. a different chunk under the same sequence/idempotency key is refused
    tampered = bytearray(rec.chunks[0]["_ciphertext"])
    tampered[-1] ^= 0xFF
    tampered = bytes(tampered)
    conflict = rec.put_chunk(1, body=tampered, ct_sha=sha256_hex(tampered))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CHUNK_CONFLICT"

    # 12. events are ingested
    events = [
        {"event": "session_started", "offset_ms": 0, "at": "2026-09-09T06:00:00+02:00"},
        {"event": "patient_boundary", "offset_ms": 420, "patient_index": 2,
         "at": "2026-09-09T06:04:12+02:00"},
    ]
    ev = rec.send_events(events)
    assert ev.status_code == 200
    assert ev.json()["stored"] == 2

    # 13. the same events again do not duplicate
    ev2 = rec.send_events(events)
    assert ev2.status_code == 200
    assert ev2.json()["stored"] == 0
    assert ev2.json()["duplicates"] == 2
    assert ev2.json()["total_events"] == 2

    # 14-15. complete arrives while chunk 3 is still missing
    rec.put_chunk(2)
    incomplete = rec.complete()
    assert incomplete.status_code == 200
    assert incomplete.json()["ingest_confirmed"] is False
    assert incomplete.json()["missing_chunks"] == [3]
    assert incomplete.json()["state"] == "RECEIVING"

    # 16. the missing chunk is finally uploaded
    assert rec.put_chunk(3).status_code == 200

    # 17-18. complete is retried and the server confirms durable ingest
    done = rec.complete()
    assert done.status_code == 200
    assert done.json()["ingest_confirmed"] is True
    assert done.json()["state"] == "INGESTED"
    assert done.json()["missing_chunks"] == []

    # 19. the status endpoint reports the same
    st = rec.status()
    assert st.status_code == 200
    body = st.json()
    assert body["state"] == "INGESTED"
    assert body["ingest_confirmed"] is True
    assert body["received_chunks"] == 3
    assert body["verified_chunks"] == 3

    # 20. everything the recorder needs is durably on the server
    import app.storage as storage

    for chunk in rec.chunks:
        path = storage.chunk_path(rec.session_id, chunk["sequence"])
        assert path.exists()
        assert sha256_hex(path.read_bytes()) == chunk["ciphertext_sha256"]
    assert server.db.query_one(
        "SELECT manifest_json FROM sessions WHERE session_id = ?", (rec.session_id,)
    )["manifest_json"]
    assert body["segments"], "multi_patient session should expose patient segments"


def test_interrupted_session_can_still_confirm(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.add_chunk(seconds=0.3)
    assert rec.create(rec.manifest(status="interrupted")).status_code == 201
    rec.upload_all()
    done = rec.complete(status="interrupted")
    assert done.status_code == 200
    assert done.json()["ingest_confirmed"] is True
    # ...but downstream must still be able to see that it was interrupted.
    assert done.json()["client_status"] == "interrupted"
    assert rec.status().json()["client_status"] == "interrupted"


def test_ingest_not_confirmed_before_complete(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.create()
    rec.upload_all()
    body = rec.status().json()
    assert body["ingest_confirmed"] is False
    assert body["state"] == "RECEIVING"
    assert body["received_chunks"] == 1


def test_chunk_count_mismatch_blocks_confirmation(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    rec.create()
    rec.upload_all()
    body = rec.complete(chunk_count=5).json()
    assert body["ingest_confirmed"] is False
    assert "chunk_count" in (body.get("error", {}) or {}).get("message", "")
    # correcting the count on a retry lets it through
    assert rec.complete(chunk_count=1).json()["ingest_confirmed"] is True


def test_patient_segments_from_boundaries(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode="multi_patient")
    for i in range(3):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create()
    rec.upload_all()
    rec.send_events([
        {"event": "patient_boundary", "offset_ms": 1000},
        {"event": "patient_boundary", "offset_ms": 2000},
    ])
    rec.complete()
    segments = rec.status().json()["segments"]
    assert [s["start_ms"] for s in segments] == [0, 1000, 2000]
    assert segments[0]["end_ms"] == 1000
    assert segments[-1]["end_ms"] == 3000  # total decoded duration


def test_privacy_pause_is_a_timeline_gap(server):
    import app.sessions as sessions

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.4)
    rec.create()
    rec.upload_all()
    rec.send_events([
        {"event": "privacy_pause_started", "offset_ms": 240000},
        {"event": "privacy_pause_ended", "offset_ms": 260000},
    ])
    rec.complete()
    gaps = sessions.privacy_gaps(rec.session_id)
    assert gaps == [{"start_ms": 240000, "end_ms": 260000, "closed": True}]


def test_device_config_and_heartbeat(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    cfg = server.client.get("/v1/device/config", headers=rec.headers())
    assert cfg.status_code == 200
    body = cfg.json()
    assert body["upload_enabled"] is True
    assert set(body["allowed_modes"]) == {"single_patient", "multi_patient", "meeting"}
    assert body["encryption_required"] is True
    assert "PUBLIC KEY" in body["server_key"]["public_key_pem"]

    beat = server.client.post(
        "/v1/device/heartbeat",
        json={"device_id": "visitescribe-001", "software_version": "0.2",
              "battery_percent": 82, "queue_count": 3,
              "storage_free_bytes": 10324598423, "network_state": "online"},
        headers=rec.headers(),
    )
    assert beat.status_code == 200
    row = server.db.query_one(
        "SELECT battery_percent, queue_count, network_state FROM devices WHERE device_id = ?",
        ("visitescribe-001",))
    assert row["battery_percent"] == 82
    assert row["queue_count"] == 3
    assert row["network_state"] == "online"


def test_unknown_future_fields_are_accepted(server):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    manifest = rec.manifest()
    manifest["future_field"] = {"anything": [1, 2, 3]}
    manifest["chunks"][0]["extra_hint"] = "reserved"
    manifest["audio"]["loudness_lufs"] = -23.0
    assert rec.create(manifest).status_code == 201


def test_full_flow_without_optional_headers_matches_v02_client(server):
    """The stock client sends no Authorization header at all."""
    server.register_device("visitescribe-001", allow_header_only=True)
    rec = Recorder(server)
    rec.add_chunk(seconds=0.3)
    assert rec.create().status_code == 201
    assert rec.put_chunk(1).status_code == 200
    assert rec.send_events([{"event": "session_completed", "offset_ms": 300}]).status_code == 200
    assert rec.complete().json()["ingest_confirmed"] is True
    assert rec.status().json()["state"] in (
        "INGESTED", "READY_FOR_PROCESSING", "TRANSCRIBING", "PROCESSING",
        "REVIEW_REQUIRED", "APPROVED",
    )
