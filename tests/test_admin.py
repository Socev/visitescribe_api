"""Admin interface: access control, rendering and the operations it offers."""
from __future__ import annotations

import io
import zipfile

from conftest import Recorder


def _ingested(server, mode: str = "single_patient") -> Recorder:
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode=mode)
    rec.add_chunk(seconds=0.5, seed=1)
    rec.add_chunk(seconds=0.5, seed=2)
    rec.create()
    rec.upload_all()
    rec.send_events([{"event": "patient_boundary", "offset_ms": 500}])
    assert rec.complete().json()["ingest_confirmed"] is True
    return rec


def test_admin_requires_login(server):
    resp = server.admin.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert server.admin.get("/admin/api/overview").status_code == 401


def test_bad_password_is_refused_and_audited(server):
    assert server.admin.post("/admin/api/login",
                             json={"password": "nope"}).status_code == 401
    assert server.db.query(
        "SELECT * FROM audit WHERE action = 'login_failed'")


def test_login_then_dashboard_renders(server):
    _ingested(server)
    assert server.admin_login().status_code == 200
    page = server.admin.get("/admin/")
    assert page.status_code == 200
    assert "VisiteScribe" in page.text
    assert "Overview" in page.text
    assert "visitescribe-001" in page.text
    data = server.admin.get("/admin/api/overview").json()
    assert data["sessions_total"] == 1
    assert data["sessions_confirmed"] == 1
    assert data["chunks_total"] == 2
    assert data["flac_decoder"] == "libsndfile"


def test_every_page_renders(server):
    rec = _ingested(server, mode="multi_patient")
    server.admin_login()
    for path in ("/admin/", "/admin/sessions", "/admin/devices", "/admin/keys",
                 "/admin/audit", f"/admin/sessions/{rec.session_id}",
                 "/admin/devices/visitescribe-001"):
        resp = server.admin.get(path)
        assert resp.status_code == 200, path
        assert "<html" in resp.text.lower(), path
        assert "Traceback" not in resp.text, path


def test_session_detail_shows_integrity_and_segments(server):
    rec = _ingested(server, mode="multi_patient")
    server.admin_login()
    page = server.admin.get(f"/admin/sessions/{rec.session_id}")
    assert "verified" in page.text
    assert "patient_boundary" in page.text
    assert "Patient segments" in page.text
    data = server.admin.get(f"/admin/api/sessions/{rec.session_id}").json()
    assert len(data["chunks"]) == 2
    assert all(c["decrypt_verified"] for c in data["chunks"])
    assert data["missing_chunks"] == []
    assert len(data["segments"]) == 2


def test_admin_never_exposes_the_wrapped_key(server):
    rec = _ingested(server)
    server.admin_login()
    data = server.admin.get(f"/admin/api/sessions/{rec.session_id}").json()
    assert "wrap_ciphertext_b64" not in data["raw"]
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    assert rec.wrapped_key_b64()[:40] not in page


def test_device_lifecycle(server):
    server.admin_login()
    created = server.admin.post("/admin/api/devices",
                                json={"device_id": "visitescribe-007",
                                      "display_name": "Spare", "issue_token": True})
    assert created.status_code == 200
    token = created.json()["token"]
    assert token

    # the issued token now actually works, and header-only no longer does
    rec = Recorder(server, device_id="visitescribe-007")
    rec.add_chunk(seconds=0.2)
    assert rec.create().status_code == 401
    ok = server.client.post(
        "/v1/sessions", json=rec.manifest(),
        headers={"X-Device-ID": "visitescribe-007",
                 "Idempotency-Key": f"{rec.session_id}:create",
                 "Authorization": f"Bearer {token}"})
    assert ok.status_code == 201

    # revoking it closes the door again
    server.admin.post("/admin/api/devices/visitescribe-007/token", json={"revoke": True})
    server.admin.post("/admin/api/devices/visitescribe-007",
                      json={"allow_header_only": False})
    rec2 = Recorder(server, device_id="visitescribe-007")
    rec2.add_chunk(seconds=0.2)
    assert server.client.post(
        "/v1/sessions", json=rec2.manifest(),
        headers={"X-Device-ID": "visitescribe-007",
                 "Idempotency-Key": f"{rec2.session_id}:create",
                 "Authorization": f"Bearer {token}"}).status_code == 401


def test_enrolment_window_admits_exactly_one_device(server):
    server.admin_login()
    server.admin.post("/admin/api/devices/visitescribe-050/enrolment",
                      json={"minutes": 10})
    allowed = Recorder(server, device_id="visitescribe-050")
    allowed.add_chunk(seconds=0.2)
    assert allowed.create().status_code == 201

    stranger = Recorder(server, device_id="visitescribe-051")
    stranger.add_chunk(seconds=0.2)
    assert stranger.create().status_code == 401


def test_disable_device_from_admin(server):
    _ingested(server)
    server.admin_login()
    server.admin.post("/admin/api/devices/visitescribe-001", json={"enabled": False})
    rec = Recorder(server)
    rec.add_chunk(seconds=0.2)
    assert rec.create().status_code == 403


def test_wav_and_zip_export(server):
    import soundfile as sf

    rec = _ingested(server)
    server.admin_login()
    wav = server.admin.get(f"/admin/api/sessions/{rec.session_id}/audio.wav")
    assert wav.status_code == 200
    with sf.SoundFile(io.BytesIO(wav.content)) as handle:
        assert handle.samplerate == 48000
        assert handle.channels == 1
        assert len(handle) > 0

    export = server.admin.get(f"/admin/api/sessions/{rec.session_id}/export.zip")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "events.json" in names
        assert "audit.json" in names
        assert "audio/chunk-000001.flac.enc" in names

    assert server.db.query(
        "SELECT * FROM audit WHERE category = 'export'")


def test_chunk_download_encrypted_and_decrypted(server):
    rec = _ingested(server)
    server.admin_login()
    enc = server.admin.get(
        f"/admin/api/sessions/{rec.session_id}/chunks/1/download?form=encrypted")
    assert enc.content == rec.chunks[0]["_ciphertext"]
    dec = server.admin.get(
        f"/admin/api/sessions/{rec.session_id}/chunks/1/download?form=decrypted")
    assert dec.content == rec.chunks[0]["_plaintext"]
    assert dec.content[:4] == b"fLaC"


def test_state_and_processing_route(server):
    rec = _ingested(server)
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/state",
                      json={"state": "READY_FOR_PROCESSING"})
    assert rec.status().json()["state"] == "READY_FOR_PROCESSING"
    # the recorder still sees ingest as durably confirmed
    assert rec.status().json()["ingest_confirmed"] is True

    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "local"})
    data = server.admin.get(f"/admin/api/sessions/{rec.session_id}").json()
    assert data["processing"]["route"] == "local"


def test_purge_removes_audio_but_keeps_the_record(server):
    import app.storage as storage

    rec = _ingested(server)
    server.admin_login()
    assert storage.chunk_path(rec.session_id, 1).exists()

    resp = server.admin.post(f"/admin/api/sessions/{rec.session_id}/purge",
                             json={"scope": "all"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "PURGED"
    assert not storage.chunk_path(rec.session_id, 1).exists()
    assert server.db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (rec.session_id,))["n"] == 0
    # the fact that a purge happened survives
    assert server.db.query("SELECT * FROM purges WHERE session_id = ?", (rec.session_id,))
    assert server.db.query(
        "SELECT * FROM audit WHERE category = 'purge' AND session_id = ?", (rec.session_id,))
    assert rec.status().status_code == 410


def test_key_rotation_from_admin(server):
    server.admin_login()
    before = server.admin.get("/admin/api/keys").json()["keys"]
    assert len(before) == 1
    rotated = server.admin.post("/admin/api/keys/rotate", json={})
    assert rotated.status_code == 200
    keys = rotated.json()["keys"]
    assert len(keys) == 2
    assert sum(1 for k in keys if k["active"]) == 1
    assert keys[0]["key_id"] != before[0]["key_id"]


def test_audit_page_filters(server):
    _ingested(server)
    server.admin_login()
    page = server.admin.get("/admin/audit?category=chunk")
    assert page.status_code == 200
    assert "received" in page.text
    data = server.admin.get("/admin/api/audit?category=chunk").json()
    assert data["total"] >= 2
    assert all(e["category"] == "chunk" for e in data["entries"])


def test_cannot_delete_a_device_that_owns_sessions(server):
    _ingested(server)
    server.admin_login()
    resp = server.admin.request("DELETE", "/admin/api/devices/visitescribe-001")
    assert resp.status_code == 400
    assert "disable" in resp.json()["error"]["message"]


def test_logout(server):
    server.admin_login()
    assert server.admin.get("/admin/api/overview").status_code == 200
    server.admin.post("/admin/api/logout")
    assert server.admin.get("/admin/api/overview").status_code == 401
