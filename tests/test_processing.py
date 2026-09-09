"""The processing layer: routing policy, the job queue, and cost accounting."""
from __future__ import annotations

import json

import pytest

from conftest import Recorder


# ---------------------------------------------------------------------------
# a provider that costs nothing and always works, so the machinery can be
# tested without touching a real API
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"
    eu_endpoint = False
    calls: list[tuple[str, object]] = []
    fail_times = 0
    retryable = True

    def configured(self):
        return (True, "")

    def transcribe(self, audio, *, language, context):
        from app.providers.base import ProviderError, TranscriptResult, Usage

        FakeProvider.calls.append(("transcribe", context.get("segment_index")))
        if FakeProvider.fail_times > 0:
            FakeProvider.fail_times -= 1
            raise ProviderError("tijdelijke storing", retryable=FakeProvider.retryable)
        return TranscriptResult(
            text=f"transcript voor segment {context.get('segment_index')}",
            language="nl", model="voxtral-mini-2602",
            segments=[{"text": "hallo", "start": 0.0, "end": 1.0,
                       "speaker_id": "speaker_1"}],
            usage=Usage(audio_seconds=context.get("audio_seconds") or 120.0,
                        raw={"prompt_audio_seconds": 120}),
        )

    def make_note(self, transcript, *, context):
        from app.providers.base import NoteResult, Usage

        FakeProvider.calls.append(("note", context.get("segment_index")))
        return NoteResult(
            body=f"S: klacht\nO: onderzoek\nE: beoordeling\nP: beleid\n"
                 f"[{transcript.text}]",
            model="mistral-medium-3.5", template="SOEP",
            usage=Usage(prompt_tokens=5000, completion_tokens=800,
                        total_tokens=5800),
        )


@pytest.fixture()
def fake_route(server):
    """Serve the `mistral` route from the fake provider."""
    import app.providers as providers

    FakeProvider.calls = []
    FakeProvider.fail_times = 0
    FakeProvider.retryable = True
    original = providers._REGISTRY["mistral"]
    providers._REGISTRY["mistral"] = FakeProvider
    yield FakeProvider
    providers._REGISTRY["mistral"] = original


def _ingested(server, mode="single_patient", chunks=2, boundaries=None):
    server.register_device("visitescribe-001")
    rec = Recorder(server, mode=mode)
    for i in range(chunks):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create()
    rec.upload_all()
    if boundaries:
        rec.send_events([{"event": "patient_boundary", "offset_ms": b}
                         for b in boundaries])
    assert rec.complete().json()["ingest_confirmed"] is True
    return rec


# ---------------------------------------------------------------------------
# routing policy
# ---------------------------------------------------------------------------

def test_meeting_audio_may_not_go_to_ourmind(server):
    rec = _ingested(server, mode="meeting")
    server.admin_login()
    resp = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                             json={"route": "ourmind"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ROUTE_NOT_ALLOWED"


def test_patient_audio_may_go_to_ourmind(server):
    """Allowed by policy — it fails later, on credentials, not on the rule."""
    rec = _ingested(server, mode="single_patient")
    server.admin_login()
    resp = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                             json={"route": "ourmind"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"


@pytest.mark.parametrize("route", ["plaud", "local"])
def test_retired_routes_explain_themselves(server, route):
    rec = _ingested(server)
    server.admin_login()
    resp = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                             json={"route": route})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ROUTE_NOT_AVAILABLE"
    assert len(resp.json()["error"]["message"]) > 20


def test_policy_is_rechecked_in_the_worker(server, fake_route):
    """Even if a job somehow names a forbidden route, nothing leaves the box."""
    import app.processing as processing

    rec = _ingested(server, mode="meeting")
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})
    # Rewrite the queued job to a route the policy forbids for a meeting.
    server.db.execute("UPDATE processing_jobs SET route = 'ourmind' WHERE session_id = ?",
                      (rec.session_id,))
    processing.run_once()
    job = processing.jobs_for(rec.session_id)[0]
    assert job["state"] == "failed"
    assert "mag niet naar" in (job["error"] or "")
    assert ("transcribe", None) not in FakeProvider.calls


def test_cannot_process_before_ingest_is_confirmed(server, fake_route):
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=0.5)
    rec.create()
    server.admin_login()
    resp = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                             json={"route": "mistral"})
    assert resp.status_code == 400
    assert "bevestigde ingest" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_full_processing_run(server, fake_route):
    import app.processing as processing

    rec = _ingested(server)
    server.admin_login()
    queued = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                               json={"route": "mistral"})
    assert queued.status_code == 200
    assert queued.json()["jobs"] == 1
    assert rec.status().json()["state"] == "READY_FOR_PROCESSING"

    assert processing.run_once() is True      # transcribe
    assert processing.run_once() is True      # note
    assert processing.run_once() is False     # nothing left

    data = server.admin.get(f"/admin/api/sessions/{rec.session_id}/results").json()
    assert len(data["transcripts"]) == 1
    assert data["transcripts"][0]["provider"] == "mistral"
    assert data["transcripts"][0]["model"] == "voxtral-mini-2602"
    assert len(data["notes"]) == 1
    assert data["notes"][0]["body"].startswith("S:")
    assert data["notes"][0]["status"] == "draft"
    assert all(j["state"] == "done" for j in data["jobs"])

    # the recorder still sees durable ingest throughout
    status = rec.status().json()
    assert status["state"] == "REVIEW_REQUIRED"
    assert status["ingest_confirmed"] is True


def test_multi_patient_segments_are_processed_separately(server, fake_route):
    import app.processing as processing

    rec = _ingested(server, mode="multi_patient", chunks=3, boundaries=[1000, 2000])
    server.admin_login()
    queued = server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                               json={"route": "mistral"})
    assert queued.json()["jobs"] == 3
    for _ in range(20):
        if not processing.run_once():
            break

    data = server.admin.get(f"/admin/api/sessions/{rec.session_id}/results").json()
    indices = sorted(t["segment_index"] for t in data["transcripts"])
    assert indices == [1, 2, 3]
    # Three separate notes: two patients' audio is never merged into one.
    assert sorted(n["segment_index"] for n in data["notes"]) == [1, 2, 3]
    bodies = {n["body"] for n in data["notes"]}
    assert len(bodies) == 3


def test_audio_slices_match_the_segment_boundaries(server, tmp_path):
    import app.audio as audio

    rec = _ingested(server, mode="multi_patient", chunks=3, boundaries=[1000, 2000])
    slices = audio.slices_for(rec.session_id, tmp_path)
    assert [s.segment_index for s in slices] == [1, 2, 3]
    for item in slices:
        assert 0.9 < item.seconds < 1.1, item.seconds
        assert item.path.exists()
        assert item.path.read_bytes()[:4] == b"fLaC"
        assert item.sample_rate == 48000


# ---------------------------------------------------------------------------
# cost accounting
# ---------------------------------------------------------------------------

def test_cost_is_recorded_per_call(server, fake_route):
    import app.processing as processing

    rec = _ingested(server)
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})
    processing.run_once()
    processing.run_once()

    usage = processing.usage_for(rec.session_id)
    assert [u["operation"] for u in usage] == ["transcribe", "note"]

    # The duration billed is the real length of the reassembled audio — two
    # one-second chunks — not anything the provider made up.
    transcribe = usage[0]
    assert transcribe["audio_seconds"] == pytest.approx(2.0, abs=0.05)
    assert transcribe["cost_usd"] == pytest.approx(2.0 / 60 * 0.003, rel=1e-3)
    assert transcribe["priced"] == 1
    assert "0.003/min" in transcribe["price_note"]

    # 5000 in + 800 out on mistral-medium-3.5 at $1.5/M in, $7.5/M out
    note = usage[1]
    assert note["cost_usd"] == pytest.approx(5000 * 1.5e-6 + 800 * 7.5e-6, rel=1e-6)

    summary = server.admin.get("/admin/api/costs").json()
    assert summary["total_cost_usd"] == pytest.approx(
        transcribe["cost_usd"] + note["cost_usd"], rel=1e-6)
    assert summary["unpriced_calls"] == 0
    assert summary["total_audio_seconds"] == pytest.approx(2.0, abs=0.05)


def test_an_unknown_price_is_never_counted_as_free(server):
    import app.processing as processing
    from app.providers.base import Usage

    processing.record_usage("s1", None, "mistral", "some-unreleased-model",
                            "transcribe", Usage(audio_seconds=600))
    summary = processing.cost_summary()
    assert summary["unpriced_calls"] == 1
    assert summary["total_cost_usd"] == 0.0
    row = processing.usage_for("s1")[0]
    assert row["priced"] == 0
    assert row["cost_usd"] is None
    assert "no published rate" in row["price_note"]


def test_ourmind_is_priced_as_included(server):
    import app.processing as processing
    from app.providers.base import Usage

    processing.record_usage("s2", None, "ourmind", "ourmind", "transcribe",
                            Usage(audio_seconds=3600))
    row = processing.usage_for("s2")[0]
    assert row["cost_usd"] == 0.0
    assert row["priced"] == 1
    assert "subscription" in row["price_note"]


def test_a_twenty_minute_consultation_costs_six_cents(server):
    from app import pricing

    cost, note = pricing.audio_cost("mistral", "voxtral-mini-2602", 20 * 60)
    assert cost == pytest.approx(0.06, rel=1e-9)
    assert "docs.mistral.ai" in note


def test_eu_endpoint_uplift_is_applied_and_flagged(server):
    from app import pricing

    plain, _ = pricing.audio_cost("mistral", "voxtral-mini-2602", 600)
    eu, note = pricing.audio_cost("mistral", "voxtral-mini-2602", 600, eu_endpoint=True)
    assert eu == pytest.approx(plain * 1.1, rel=1e-9)
    assert "niet gedocumenteerd" in note


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------

def test_a_retryable_failure_is_retried_then_given_up_on(server, fake_route):
    import app.processing as processing

    rec = _ingested(server)
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})

    FakeProvider.fail_times = 1
    processing.run_once()
    job = processing.jobs_for(rec.session_id)[0]
    assert job["state"] == "queued"          # scheduled for another attempt
    assert job["attempts"] == 1
    assert job["next_attempt_at"] is not None

    # Make it due again, then let it succeed.
    server.db.execute("UPDATE processing_jobs SET next_attempt_at = NULL WHERE id = ?",
                      (job["id"],))
    processing.run_once()
    assert processing.jobs_for(rec.session_id)[0]["state"] == "done"


def test_a_permanent_failure_stops_and_marks_the_session(server, fake_route):
    import app.processing as processing

    rec = _ingested(server)
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})
    FakeProvider.fail_times = 1
    FakeProvider.retryable = False
    processing.run_once()

    job = processing.jobs_for(rec.session_id)[0]
    assert job["state"] == "failed"
    assert rec.status().json()["state"] == "TRANSCRIPTION_FAILED"
    # ...and the recorder is still told its audio is safely stored
    assert rec.status().json()["ingest_confirmed"] is True


# ---------------------------------------------------------------------------
# review and credentials
# ---------------------------------------------------------------------------

def test_reviewing_every_note_approves_the_session(server, fake_route):
    import app.processing as processing

    rec = _ingested(server)
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})
    processing.run_once(); processing.run_once()

    resp = server.admin.post(
        f"/admin/api/sessions/{rec.session_id}/notes/full/approve",
        json={"body": "S: aangepast door de arts\nP: controle over een week"})
    assert resp.status_code == 200
    note = resp.json()["notes"][0]
    assert note["status"] == "approved"
    assert note["body"].startswith("S: aangepast")
    assert rec.status().json()["state"] == "APPROVED"


def test_credentials_are_never_echoed_back(server):
    server.admin_login()
    resp = server.admin.post("/admin/api/providers/mistral/credential",
                             json={"secret": "sk-super-secret-value"})
    assert resp.status_code == 200
    assert "sk-super-secret-value" not in resp.text

    overview = server.admin.get("/admin/api/processing").json()
    entry = next(p for p in overview["providers"] if p["provider"] == "mistral")
    assert entry["configured"] is True
    assert "sk-super-secret-value" not in json.dumps(overview)

    rows = server.db.query("SELECT detail_json FROM audit WHERE category = 'provider'")
    assert rows
    assert "sk-super-secret-value" not in " ".join(r["detail_json"] for r in rows)


def test_processing_overview_reports_the_policy(server):
    server.admin_login()
    data = server.admin.get("/admin/api/processing").json()
    policy = {p["mode"]: p["allowed"] for p in data["policy"]}
    assert policy["meeting"] == ["mistral"]
    assert policy["single_patient"] == ["mistral", "ourmind"]
    assert policy["multi_patient"] == ["mistral", "ourmind"]


def test_processing_pages_render(server, fake_route):
    import app.processing as processing

    rec = _ingested(server, mode="multi_patient", chunks=3, boundaries=[1000, 2000])
    server.admin_login()
    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "mistral"})
    for _ in range(20):
        if not processing.run_once():
            break

    costs = server.admin.get("/admin/costs")
    assert costs.status_code == 200
    assert "Verwerking" in costs.text
    assert "Toegestane routes" in costs.text
    assert "Traceback" not in costs.text

    detail = server.admin.get(f"/admin/sessions/{rec.session_id}")
    assert detail.status_code == 200
    assert "Concept-verslag" in detail.text
    assert "Patiëntsegment 1" in detail.text
    assert "Patiëntsegment 3" in detail.text
    assert "Traceback" not in detail.text


def test_a_meeting_offers_only_the_allowed_route_in_the_ui(server):
    rec = _ingested(server, mode="meeting")
    server.admin_login()
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    assert '<option value="mistral">' in page
    assert '<option value="ourmind">' not in page
