"""Drive the REAL MistralProvider over a real socket, with real recorder data.

The suite only ever ran a FakeProvider, so the request the client actually
builds was never exercised once. That is how this shipped:

    data=[("model", ...), ("diarize", "true")]

httpx only treats `data` as form fields when it is a Mapping. A list of pairs
is taken as a raw request body instead, multipart encoding is skipped, the
audio is never attached, and it dies far downstream inside h11 as
"sequence item 1: expected a bytes-like object, tuple found".

A mock transport would not have caught it either -- the encoding is chosen when
the request is built, and the failure only surfaces when the body is written to
a socket. So this test uses a real socket.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import Recorder


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        Handler.seen.append({
            "path": self.path,
            "ctype": self.headers.get("content-type") or "",
            "auth": self.headers.get("x-api-key") or self.headers.get("authorization"),
            "body": body,
        })
        if self.path.endswith("/audio/transcriptions"):
            payload = {
                "text": "Patiente meldt hoofdpijn sinds drie dagen.",
                "language": "nl",
                "model": "voxtral-mini-2602",
                "segments": [{"text": "Patiente meldt hoofdpijn.", "start": 0.0,
                              "end": 2.0, "speaker_id": "speaker_1"}],
                "usage": {"prompt_audio_seconds": 2},
            }
        else:
            payload = {
                "model": "mistral-medium-3.5",
                "choices": [{"message": {"content":
                             "S: hoofdpijn\nO: geen afwijkingen\nE: spanningshoofdpijn\nP: paracetamol"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 80,
                          "total_tokens": 580},
            }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_mistral():
    Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_real_mistral_client_runs_a_real_session(server, fake_mistral, monkeypatch):
    monkeypatch.setenv("VS_MISTRAL_BASE_URL", fake_mistral)
    monkeypatch.setenv("VS_MISTRAL_API_KEY", "test-key")

    server.register_device("visitescribe-001")
    rec = Recorder(server)
    for i in range(2):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create()
    rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True

    import app.providers as providers
    from app import processing

    assert providers.get("mistral").configured()[0]

    processing.enqueue(rec.session_id, "mistral", actor="test")
    assert processing.run_once() is True      # transcribe
    assert processing.run_once() is True      # note

    jobs = {j["stage"]: j for j in processing.jobs_for(rec.session_id)}
    for stage, job in jobs.items():
        assert job.get("error") in (None, ""), f"{stage}: {job.get('error')}"
        assert job["state"] == "done", f"{stage}: {dict(job)}"

    # --- the transcription request was really a multipart upload -----------
    asr = next(s for s in Handler.seen if s["path"].endswith("/audio/transcriptions"))
    assert asr["ctype"].startswith("multipart/form-data"), asr["ctype"]
    assert asr["auth"] == "test-key"
    body = asr["body"]
    assert b'name="model"' in body and b"voxtral-mini-2602" in body
    assert b'name="diarize"' in body
    assert b'name="language"' in body and b"nl" in body
    # the audio itself, not just its name: two 1-second chunks of real FLAC
    assert b'name="file"' in body and b"filename=" in body
    assert b"fLaC" in body
    assert len(body) > 2000, f"body is only {len(body)} bytes; no audio attached"

    # --- and the note request carried the transcript ------------------------
    note = next(s for s in Handler.seen if s["path"].endswith("/chat/completions"))
    assert note["ctype"].startswith("application/json")
    assert note["auth"] == "Bearer test-key"
    assert b"hoofdpijn" in note["body"]

    # --- results and cost landed -------------------------------------------
    results = processing.results_for(rec.session_id)
    assert results["transcripts"] and "hoofdpijn" in results["transcripts"][0]["text"]
    assert results["notes"] and "S:" in results["notes"][0]["body"]

    usage = processing.usage_for(rec.session_id)
    ops = {u["operation"]: u for u in usage}
    assert ops["transcribe"]["priced"] == 1
    assert ops["transcribe"]["cost_usd"] > 0
    assert ops["note"]["priced"] == 1
