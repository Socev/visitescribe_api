"""Audio leaves the server once it has become a report (1.11.0).

And transcribers other than OurMind are a per-user switch the admin turns on.
"""
from __future__ import annotations

import pytest

from conftest import Recorder


class StubProvider:
    """Behaves like a provider that transcribes and writes a report."""

    upload_formats = staticmethod(lambda: ("flac",))

    def __init__(self, name: str, fail_note: bool = False):
        self.name = name
        self.fail_note = fail_note
        self.calls: list[str] = []

    def configured(self):
        return True, ""

    def transcribe(self, audio, *, language, context):
        from app.providers.base import TranscriptResult

        assert audio.exists()            # the audio is really there to send
        self.calls.append("transcribe")
        return TranscriptResult(text="hallo dokter", language="nl", model="stub",
                                provider_ref="ref-1")

    def make_note(self, transcript, *, context):
        from app.providers.base import NoteResult, ProviderError

        self.calls.append("note")
        if self.fail_note:
            raise ProviderError("kapot", retryable=False)
        return NoteResult(body="Verslag", title="Consult", model="stub")


@pytest.fixture
def stub(server, monkeypatch):
    from app import processing

    made: dict[str, StubProvider] = {}

    def fake_get(route, token=None):
        return made.setdefault(route, StubProvider(route))

    monkeypatch.setattr(processing, "get_provider", fake_get)
    return made


def _recording(server, device_id="visitescribe-001"):
    rec = Recorder(server, device_id=device_id)
    rec.add_chunk(seconds=1.0, seed=1)
    rec.add_chunk(seconds=1.0, seed=2)
    rec.create(); rec.upload_all(); rec.complete()
    return rec


def _drain():
    from app import processing

    for _ in range(10):
        if not processing.run_once():
            break


def _session_dir(server, session_id):
    from app import storage

    return storage.session_dir(session_id)


def test_audio_is_removed_once_processed_and_recorder_still_sees_confirmed(server, stub):
    from app import processing, sessions

    server.register_device("visitescribe-001")
    rec = _recording(server)
    assert _session_dir(server, rec.session_id).exists()

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()

    s = sessions.get(rec.session_id)
    assert s["state"] == "REVIEW_REQUIRED"
    assert s["audio_purged_at"]
    assert not s["wrap_ciphertext_b64"]
    assert not _session_dir(server, rec.session_id).exists()
    # transcript and report stay
    assert server.db.query_one("SELECT text FROM transcripts WHERE session_id = ?",
                               (rec.session_id,))["text"] == "hallo dokter"
    assert server.db.query_one("SELECT body FROM notes WHERE session_id = ?",
                               (rec.session_id,))["body"] == "Verslag"
    purge = server.db.query_one("SELECT * FROM purges WHERE session_id = ?",
                                (rec.session_id,))
    assert purge["scope"] == "audio" and purge["actor"] == "auto"

    # A recorder that asks again is told "done" and must not upload again.
    status = rec.status().json()
    assert status["ingest_confirmed"] is True
    again = rec.complete().json()
    assert again["ingest_confirmed"] is True
    # A re-sent identical chunk is acknowledged but nothing is stored.
    rec.put_chunk(rec.chunks[0]["sequence"])
    assert not _session_dir(server, rec.session_id).exists()

    # Re-processing is refused with a clear reason.
    from app.errors import ApiError

    with pytest.raises(ApiError) as exc:
        processing.enqueue(rec.session_id, "ourmind", actor="test")
    assert exc.value.code == "AUDIO_PURGED"


def test_admin_cannot_download_removed_audio(server, stub):
    from app import processing

    server.register_device("visitescribe-001")
    rec = _recording(server)
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()
    server.admin_login()
    r = server.admin.get(f"/admin/api/sessions/{rec.session_id}/audio.wav")
    assert r.status_code == 410
    r = server.admin.get(f"/admin/api/sessions/{rec.session_id}/chunks/1/download")
    assert r.status_code == 410
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    assert "Audio verwijderd op" in page


def test_failed_processing_keeps_the_audio(server, stub):
    from app import processing, sessions

    stub["ourmind"] = StubProvider("ourmind", fail_note=True)
    server.register_device("visitescribe-001")
    rec = _recording(server)
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()
    assert not sessions.get(rec.session_id)["audio_purged_at"]
    assert _session_dir(server, rec.session_id).exists()

    # ...and once it succeeds after all, the audio goes.
    stub["ourmind"].fail_note = False
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()
    assert sessions.get(rec.session_id)["audio_purged_at"]


def test_diagnostic_mode_keeps_audio_for_new_recordings_only(server, stub):
    from app import processing, retention, sessions

    server.register_device("visitescribe-001")
    before = _recording(server)
    server.admin_login()
    r = server.admin.post("/admin/api/devices/visitescribe-001",
                          json={"diagnostic_mode": True})
    assert r.status_code == 200, r.text
    during = _recording(server)
    assert not sessions.get(before.session_id)["keep_audio"]
    assert sessions.get(during.session_id)["keep_audio"]

    for rec in (before, during):
        processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()
    assert sessions.get(before.session_id)["audio_purged_at"]
    assert not sessions.get(during.session_id)["audio_purged_at"]
    assert _session_dir(server, during.session_id).exists()

    # Switching off does not touch what is already kept.
    server.admin.post("/admin/api/devices/visitescribe-001",
                      json={"diagnostic_mode": False})
    retention.sweep()
    assert not sessions.get(during.session_id)["audio_purged_at"]

    # ...but the 30-day ceiling does.
    server.db.execute("UPDATE sessions SET created_at = '2020-01-01T00:00:00Z' "
                      "WHERE session_id = ?", (during.session_id,))
    out = retention.sweep()
    assert out["diagnostic_expired"] == 1
    assert sessions.get(during.session_id)["audio_purged_at"]

    page = server.admin.get("/admin/devices/visitescribe-001").text
    assert "Diagnostische modus" in page


def test_recordings_from_before_the_upgrade_are_left_to_the_admin(server, stub):
    from app import db, processing, retention, sessions

    server.register_device("visitescribe-001")
    rec = _recording(server)
    # pretend automatic removal started after this recording arrived
    db.set_meta(retention.SINCE_KEY, "2999-01-01T00:00:00Z")
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    _drain()
    assert not sessions.get(rec.session_id)["audio_purged_at"]
    assert retention.backlog() == [rec.session_id]

    server.admin_login()
    assert "Oude audio opruimen" in server.admin.get("/admin/sessions").text
    r = server.admin.post("/admin/api/retention/backlog", json={})
    assert r.json()["sessions"] == 1
    assert sessions.get(rec.session_id)["audio_purged_at"]


def test_forced_processing_of_an_incomplete_recording_keeps_the_audio(server, stub):
    """The recorder deletes its copy once confirmed; the server copy must stay."""
    from app import processing, sessions

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0, seed=1)
    rec.add_chunk(seconds=1.0, seed=2)
    rec.create()
    rec.put_chunk(rec.chunks[0]["sequence"])          # second chunk never arrives
    processing.enqueue(rec.session_id, "ourmind", actor="admin", force=True,
                       by_admin=True)
    _drain()
    assert server.db.query_one("SELECT 1 FROM notes WHERE session_id = ?",
                               (rec.session_id,))
    assert not sessions.get(rec.session_id)["audio_purged_at"]


def test_purge_backs_off_when_a_job_got_queued_first(server, stub):
    from app import processing, retention, sessions

    server.register_device("visitescribe-001")
    rec = _recording(server)
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    assert retention.purge_audio(rec.session_id, who="t", reason="race") is None
    assert not sessions.get(rec.session_id)["audio_purged_at"]
    assert _session_dir(server, rec.session_id).exists()


def test_a_half_finished_purge_is_completed_by_the_sweep(server, stub):
    from app import retention, sessions

    server.register_device("visitescribe-001")
    rec = _recording(server)
    server.db.execute("UPDATE sessions SET audio_purged_at = '2026-01-01T00:00:00Z' "
                      "WHERE session_id = ?", (rec.session_id,))
    retention.sweep()
    assert not _session_dir(server, rec.session_id).exists()
    assert not sessions.get(rec.session_id)["wrap_ciphertext_b64"]


def test_unprocessed_recordings_keep_their_audio(server, stub):
    from app import retention, sessions

    server.register_device("visitescribe-001")
    rec = _recording(server)
    retention.sweep()
    assert not sessions.get(rec.session_id)["audio_purged_at"]


# ---------------------------------------------------------------------------
# other transcribers are a per-user switch
# ---------------------------------------------------------------------------

def _owned(server):
    from app import users

    user = users.create("dokter@praktijk.nl")
    server.register_device("visitescribe-001")
    users.bind_device("visitescribe-001", user["user_id"])
    return user


def test_users_get_only_ourmind_unless_switched_on(server, stub):
    from app import processing, routing, users
    from app.errors import ApiError

    user = _owned(server)
    assert routing.allowed_for_user("single_patient", users.get(user["user_id"])) \
        == frozenset({"ourmind"})
    rec = _recording(server)

    with pytest.raises(ApiError) as exc:
        processing.enqueue(rec.session_id, "mistral", actor=user["email"])
    assert exc.value.code == "ROUTE_NOT_ALLOWED"
    with pytest.raises(ApiError):
        users.set_rule(user["user_id"], "single_patient", route="mistral", auto=True)

    # The admin can still send one recording elsewhere by hand.
    processing.enqueue(rec.session_id, "mistral", actor="admin", by_admin=True)
    _drain()
    assert stub["mistral"].calls == ["transcribe", "note"]

    users.set_allow_other_providers(user["user_id"], True, actor="admin")
    assert "mistral" in routing.allowed_for_user(
        "single_patient", users.get(user["user_id"]))
    users.set_rule(user["user_id"], "single_patient", route="mistral", auto=True)

    # Switching off moves the standing rule back to OurMind.
    moved = users.set_allow_other_providers(user["user_id"], False, actor="admin")
    assert moved == 1
    assert users.rule(user["user_id"], "single_patient")["route"] == "ourmind"


def test_worker_refuses_a_user_job_after_the_switch_went_off(server, stub):
    from app import processing, users

    user = _owned(server)
    users.set_allow_other_providers(user["user_id"], True, actor="admin")
    rec = _recording(server)
    processing.enqueue(rec.session_id, "mistral", actor=user["email"])
    users.set_allow_other_providers(user["user_id"], False, actor="admin")
    _drain()
    assert "mistral" not in stub or not stub["mistral"].calls
    job = server.db.query_one("SELECT state, error_code FROM processing_jobs "
                              "WHERE session_id = ?", (rec.session_id,))
    assert job["state"] == "failed" and job["error_code"] == "ROUTE_NOT_ALLOWED"


def test_admin_checkbox_switches_other_providers(server):
    from app import users

    user = users.create("dokter@praktijk.nl")
    server.admin_login()
    page = server.admin.get("/admin/users").text
    assert "Andere transcribers dan OurMind" in page
    r = server.admin.post(f"/admin/api/users/{user['user_id']}/providers",
                          json={"allow_other_providers": True})
    assert r.status_code == 200
    assert users.get(user["user_id"])["allow_other_providers"] == 1


def test_old_mistral_rules_move_to_ourmind_at_start(server):
    from app import db, users

    user = users.create("dokter@praktijk.nl")
    db.execute("INSERT INTO routing_rules(user_id, mode, route, auto, updated_at) "
               "VALUES(?, 'single_patient', 'mistral', 1, '2026-01-01T00:00:00Z')",
               (user["user_id"],))
    db._restrict_routes(db.get_conn())
    assert users.rule(user["user_id"], "single_patient")["route"] == "ourmind"
