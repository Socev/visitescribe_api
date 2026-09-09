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
        self._record(self._read())
        self._reply(None, code=204)

    def do_DELETE(self):
        self._record()
        self._reply(None, code=204)

    def do_GET(self):
        self._record()
        p = self.path
        if p.endswith("/transcripts"):
            return self._reply({"data": [{"id": "t-1", "attributes": {
                "status": "done",
                "segments": [{"text": "Patiente meldt hoofdpijn.", "start": 0.0,
                              "end": 2.0, "speaker_id": "speaker_1"}]}}]})
        if p.endswith("/reports"):
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
def fake_ourmind():
    Handler.calls = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
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
    assert upload["ctype"] == "audio/flac"
    assert upload["chunked"] != "chunked", "chunked upload; length unknown to the server"
    assert int(upload["length"]) == len(upload["body"]) > 2000
    assert upload["body"][:4] == b"fLaC"

    results = processing.results_for(rec.session_id)
    assert "hoofdpijn" in results["transcripts"][0]["text"]
    note = results["notes"][0]
    assert "hoofdpijn sinds drie dagen" in note["body"] and "paracetamol" in note["body"]
