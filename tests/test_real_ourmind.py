"""Drive the REAL OurMindProvider over a real socket.

Same reason as tests/test_real_mistral.py: a provider client that has never
had its request written to a socket does not work yet. The Mistral client
looked fine for 112 tests and did not send the audio at all.

This cannot verify OurMind's real API -- we have no integration token and
their limits (accepted codecs, maximum size and duration) are documented only
as `audio/*`. It verifies OUR half: the nine-step flow in the documented
order, the headers, and that the audio is uploaded as a body with a real
Content-Length rather than read into memory whole.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import Recorder

VERSION = "2025-05-07"


class Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    # Generation is asynchronous, and while it runs OurMind answers the
    # reports list with 404 "no reports found; you can generate one
    # (report-404)". Not an error -- their way of saying "not yet". The fake
    # does the same for the first few polls, because a fake that hands the
    # report over immediately never exercises the wait, and that is exactly
    # how this shipped broken.
    reports_404_for: int = 2
    transcripts_404_for: int = 0

    # -- plumbing --------------------------------------------------------
    def _record(self, body: bytes = b""):
        Handler.calls.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
            "lang": self.headers.get("Accept-Language"),
            "ctype": self.headers.get("Content-Type"),
            "length": self.headers.get("Content-Length"),
            "chunked": (self.headers.get("Transfer-Encoding") or "").lower(),
            "body": body,
        })

    def _reply(self, payload, code=200):
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        if raw:
            self.send_header("content-type", "application/vnd.api+json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _read(self) -> bytes:
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n)

    # -- the nine steps --------------------------------------------------
    def do_POST(self):
        body = self._read()
        self._record(body)
        p = self.path
        if p.endswith(f"/{VERSION}/consultations"):
            return self._reply({"data": {"id": "c-1", "type": "consultation"}})
        if p.endswith("/files"):
            return self._reply({"data": {"id": "f-1", "type": "file"}})
        if p.endswith("/seal"):
            return self._reply(None, code=204)
        if p.endswith("/reports/generate"):
            return self._reply({"data": {"id": "r-1", "type": "report"}})
        return self._reply({"errors": [{"detail": "unexpected"}]}, code=404)

    def do_PATCH(self):
        body = self._read()
        self._record(body)
        # The real OurMind refuses FLAC with exactly this, and documents
        # nothing beyond `audio/*`. A fake that accepts anything would have
        # let the FLAC upload look fine right up until production -- which is
        # precisely what happened.
        if (self.headers.get("Content-Type") or "") == "audio/flac":
            return self._reply({"errors": [{"code": "invalid-format",
                "detail": "the file format is not supported"}]}, code=400)
        self._reply(None, code=204)

    def do_DELETE(self):
        self._record()
        self._reply(None, code=204)

    def do_GET(self):
        self._record()
        p = self.path
        if p.endswith("/transcripts"):
            if Handler.transcripts_404_for > 0:
                Handler.transcripts_404_for -= 1
                return self._reply({"errors": [{"code": "transcript-404",
                    "detail": "no transcripts found"}]}, code=404)
            return self._reply({"data": [{"id": "t-1", "attributes": {
                "status": "done",
                "segments": [{"text": "Patiente meldt hoofdpijn.", "start": 0.0,
                              "end": 2.0, "speaker_id": "speaker_1"}]}}]})
        if p.endswith("/reports"):
            if Handler.reports_404_for > 0:
                Handler.reports_404_for -= 1
                return self._reply({"errors": [{"code": "report-404",
                    "detail": "no reports found; you can generate one"}]},
                    code=404)
            return self._reply({"data": [{"id": "r-1", "attributes": {
                "status": "done", "generation": 1, "title": "Consult",
                "template_id": 7, "codes": ["N01"]}}]})
        if "/report/" in p and p.endswith("/sections"):
            return self._reply({"data": [
                {"attributes": {"text": "hoofdpijn sinds drie dagen",
                                "section_template": {"position": 1, "title": "S"}}},
                {"attributes": {"text": "paracetamol",
                                "section_template": {"position": 4, "title": "P"}}},
            ]})
        return self._reply({"errors": [{"detail": "unexpected"}]}, code=404)

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_ourmind(monkeypatch):
    """Point the client here, from the fixture rather than from each test.

    A test that forgot to set VS_OURMIND_BASE_URL talked to the REAL
    api.ourmind.ai and got a 401 -- which looked like a bug in our code and
    was a test reaching the internet. The fixture now owns the redirection so
    no test can leak.
    """
    Handler.calls = []
    Handler.reports_404_for = 2
    Handler.transcripts_404_for = 1
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("VS_OURMIND_BASE_URL", base)
    monkeypatch.setenv("VS_OURMIND_AUTH_URL", base)
    monkeypatch.setenv("VS_OURMIND_POLL_SECONDS", "0")
    monkeypatch.setenv("VS_OURMIND_POLL_BUDGET", "5")
    yield base
    srv.shutdown()


def test_real_ourmind_client_runs_the_documented_flow(server, fake_ourmind,
                                                      monkeypatch):
    monkeypatch.setenv("VS_OURMIND_BASE_URL", fake_ourmind)
    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    monkeypatch.setenv("VS_OURMIND_POLL_SECONDS", "0")
    # a budget, so a wrong terminal state fails the test instead of hanging it
    monkeypatch.setenv("VS_OURMIND_POLL_BUDGET", "5")

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(2):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create()
    rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True

    import app.providers as providers
    from app import processing

    assert providers.get("ourmind").configured()[0]

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    assert processing.run_once() is True      # transcribe
    assert processing.run_once() is True      # note

    for job in processing.jobs_for(rec.session_id):
        assert job.get("error") in (None, ""), f"{job['stage']}: {job.get('error')}"
        assert job["state"] == "done", dict(job)

    steps = [(c["method"], c["path"].split(f"/{VERSION}/", 1)[-1])
             for c in Handler.calls]
    assert steps[0] == ("POST", "consultations")
    assert steps[1] == ("POST", "consultation/c-1/files")
    assert steps[2] == ("PATCH", "consultation/c-1/file/f-1")
    assert steps[3] == ("POST", "consultation/c-1/file/f-1/seal")
    assert ("GET", "consultation/c-1/transcripts") in steps
    assert ("POST", "consultation/c-1/reports/generate") in steps
    assert ("GET", "consultation/c-1/reports") in steps
    assert ("GET", "consultation/c-1/report/r-1/sections") in steps
    assert steps[-1][0] == "DELETE"

    # every call authenticated, and asking for Dutch -- Accept-Language is what
    # decides the report language; it is not the system default.
    for call in Handler.calls:
        assert call["auth"] == "Bearer test-token"
        assert call["lang"] == "nl-NL"

    # the audio went up as a real body, streamed, with a Content-Length
    upload = next(c for c in Handler.calls if c["method"] == "PATCH")
    assert upload["ctype"] == "audio/wav"
    assert upload["chunked"] != "chunked", "chunked upload; length unknown to the server"
    assert int(upload["length"]) == len(upload["body"]) > 2000
    assert upload["body"][:4] == b"RIFF"        # a real WAV, not a renamed FLAC

    results = processing.results_for(rec.session_id)
    assert "hoofdpijn" in results["transcripts"][0]["text"]
    note = results["notes"][0]
    assert "hoofdpijn sinds drie dagen" in note["body"] and "paracetamol" in note["body"]


def test_flac_is_refused_and_another_container_is_tried(server, fake_ourmind,
                                                        monkeypatch):
    """The failure David hit, and the recovery from it.

    OurMind answered "the file format is not supported (invalid-format)" for
    the FLAC the recorder produced. Their docs say only `audio/*`, so rather
    than guess, the provider names the containers it prefers and the worker
    works down the list, re-encoding from the stored chunks each time. What is
    accepted is remembered, so the second upload happens once and never again.
    """
    from app import db, processing, users
    from app.providers import ourmind

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    monkeypatch.setenv("VS_OURMIND_AUDIO_FORMATS", "flac,wav")

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(2):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create(); rec.upload_all(); rec.complete()

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    assert processing.run_once() is True

    uploads = [c for c in Handler.calls if c["method"] == "PATCH"]
    assert [u["ctype"] for u in uploads] == ["audio/flac", "audio/wav"]
    assert uploads[0]["body"][:4] == b"fLaC"
    assert uploads[1]["body"][:4] == b"RIFF"

    for job in processing.jobs_for(rec.session_id):
        assert job.get("error") in (None, ""), job.get("error")

    # ...and it is remembered, so the next recording goes straight to WAV.
    assert ourmind.accepted_audio_format() == "wav"
    assert ourmind.OurMindProvider().upload_formats()[0] == "wav"


def test_a_report_that_is_not_ready_yet_is_waited_for(server, fake_ourmind,
                                                      monkeypatch):
    """The failure David hit on three consecutive recordings.

        ProviderError: OurMind: no reports found; you can generate one
        (report-404)

    Report generation is asynchronous. Until the first report exists the list
    endpoint answers 404 with that message -- "not yet", not "broken". Treating
    it as an error failed every recording whose report took longer than the
    first poll, which is why it looked intermittent: a fast one succeeded, a
    slow one did not.
    """
    from app import processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    Handler.reports_404_for = 3          # three polls of "not yet"
    Handler.transcripts_404_for = 2

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    assert processing.run_once() is True      # transcribe
    assert processing.run_once() is True      # note

    for job in processing.jobs_for(rec.session_id):
        assert job.get("error") in (None, ""), f"{job['stage']}: {job.get('error')}"
        assert job["state"] == "done", dict(job)

    # it really did poll more than once rather than getting lucky
    report_polls = [c for c in Handler.calls
                    if c["method"] == "GET" and c["path"].endswith("/reports")]
    assert len(report_polls) >= 4

    results = processing.results_for(rec.session_id)
    assert "hoofdpijn sinds drie dagen" in results["notes"][0]["body"]


def test_a_report_that_never_arrives_still_gives_up(server, fake_ourmind,
                                                    monkeypatch):
    """Waiting must not become waiting forever."""
    from app import processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    monkeypatch.setenv("VS_OURMIND_POLL_BUDGET", "1")
    Handler.reports_404_for = 10_000

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    processing.run_once()
    processing.run_once()

    note = [j for j in processing.jobs_for(rec.session_id) if j["stage"] == "note"][0]
    assert "te lang" in (note["error"] or "")
    # retryable, so it waits and tries again rather than being written off
    assert note["state"] == "queued"
