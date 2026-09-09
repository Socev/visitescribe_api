"""The user-facing site, end to end over real sockets.

Signing in, seeing only your own recordings, choosing what happens to each
kind of recording, and having a job actually go out under that user's own
OurMind token with the template they picked.

Both fakes enforce their own rules rather than accepting anything: the
Supabase stand-in refuses a wrong code and only mints tokens for addresses it
knows, and the OurMind stand-in refuses a token it did not issue. A fake that
validates nothing has already cost us two rounds in this project.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from conftest import Recorder

VERSION = "2025-05-07"
CODE = "424242"


def _jwt(email: str, ttl: int = 3600) -> str:
    """A token shaped like Supabase's: we only ever read `exp` out of it."""
    head = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(
        {"sub": email, "email": email, "exp": int(time.time()) + ttl}
    ).encode()).decode().rstrip("=")
    return f"{head}.{body}.signature-not-checked-here"


class Fake(BaseHTTPRequestHandler):
    known_emails: set[str] = set()
    issued: set[str] = set()
    mailed: list[str] = []
    generate_bodies: list[dict] = []

    def _json(self, payload, code=200):
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        if raw:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def _bearer(self) -> str:
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()

    def _needs_token(self) -> bool:
        """OurMind refuses a token it never issued -- so must the stand-in."""
        if self._bearer() not in Fake.issued:
            self._json({"errors": [{"detail": "invalid token"}]}, code=401)
            return True
        return False

    # -- Supabase ---------------------------------------------------------
    def do_POST(self):
        body = self._body()
        payload = json.loads(body or b"{}") if body[:1] in (b"{",) else {}
        path = self.path.split("?")[0]

        if path == "/auth/v1/otp":
            email = (payload.get("email") or "").lower()
            assert payload.get("create_user") is False, "must not create accounts"
            if email in Fake.known_emails:
                Fake.mailed.append(email)
            # Same answer either way: an unknown address must not be detectable.
            return self._json({})

        if path == "/auth/v1/verify":
            email = (payload.get("email") or "").lower()
            if email not in Fake.known_emails or payload.get("token") != CODE:
                return self._json({"error_description": "Token has expired or is invalid"},
                                  code=403)
            access, refresh = _jwt(email), f"refresh-for-{email}"
            Fake.issued.add(access)
            return self._json({"access_token": access, "refresh_token": refresh,
                               "token_type": "bearer", "expires_in": 3600})

        if path == "/auth/v1/token":
            token = _jwt("refreshed@example.nl")
            Fake.issued.add(token)
            return self._json({"access_token": token, "refresh_token": "next",
                               "expires_in": 3600})

        # -- OurMind ------------------------------------------------------
        if self._needs_token():
            return
        if path.endswith(f"/{VERSION}/consultations"):
            return self._json({"data": {"id": "c-1", "type": "consultation"}})
        if path.endswith("/files"):
            return self._json({"data": {"id": "f-1", "type": "file"}})
        if path.endswith("/seal"):
            return self._json(None, code=204)
        if path.endswith("/reports/generate"):
            Fake.generate_bodies.append(json.loads(body or b"{}"))
            return self._json({"data": {"id": "r-1", "type": "report"}})
        return self._json({"errors": [{"detail": path}]}, code=404)

    def do_PATCH(self):
        self._body()
        if self._needs_token():
            return
        self._json(None, code=204)

    def do_DELETE(self):
        if self._needs_token():
            return
        self._json(None, code=204)

    def do_GET(self):
        if self._needs_token():
            return
        path = self.path.split("?")[0]
        if path.endswith(f"/{VERSION}/me"):
            return self._json({"data": {"id": "dokter@praktijk.nl", "type": "doctor",
                "attributes": {"email": "dokter@praktijk.nl", "name": "D. Schaap",
                               "org": {"id": 1, "name": "Praktijk Groenhouten"},
                               "plan": "pro", "monthly_reports": 300,
                               "reports_left": 288, "language": "nl-NL"}}})
        if path.endswith("/me/templates/all"):
            return self._json({"data": [
                {"id": "13", "type": "template", "attributes": {
                    "available": True, "title": "SOEP consult", "language": "nl-NL",
                    "slug": "soep", "has_codes": True}},
                {"id": "77", "type": "doctor_template", "attributes": {
                    "available": True, "title": "Vergaderverslag", "language": "nl-NL",
                    "slug": "vergadering", "has_codes": False}},
                {"id": "99", "type": "template", "attributes": {
                    "available": False, "title": "Niet beschikbaar", "language": "nl-NL",
                    "slug": "x", "has_codes": False}},
            ]})
        if path.endswith("/transcripts"):
            return self._json({"data": [{"id": "t-1", "attributes": {
                "status": "done",
                "segments": [{"text": "Patiente meldt hoofdpijn.", "start": 0.0,
                              "end": 2.0, "speaker_id": "speaker_1"}]}}]})
        if path.endswith("/reports"):
            return self._json({"data": [{"id": "r-1", "attributes": {
                "status": "done", "generation": 1, "title": "Verslag",
                "template_id": 77}}]})
        if "/report/" in path and path.endswith("/sections"):
            return self._json({"data": [{"attributes": {
                "text": "besproken punten", "section_template":
                {"position": 1, "title": "Verslag"}}}]})
        return self._json({"errors": [{"detail": path}]}, code=404)

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_ourmind(monkeypatch):
    Fake.known_emails = {"dokter@praktijk.nl"}
    Fake.issued, Fake.mailed, Fake.generate_bodies = set(), [], []
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("VS_OURMIND_AUTH_URL", base)
    monkeypatch.setenv("VS_OURMIND_BASE_URL", base)
    monkeypatch.setenv("VS_OURMIND_POLL_SECONDS", "0")
    monkeypatch.setenv("VS_OURMIND_POLL_BUDGET", "5")
    yield base
    srv.shutdown()


@pytest.fixture()
def site(server):
    from app.user_app import create_user_app

    with TestClient(create_user_app(), base_url="https://site.local") as client:
        yield client


def _sign_in(site, email="dokter@praktijk.nl"):
    assert site.post("/inloggen", data={"email": email}).status_code == 200
    resp = site.post("/inloggen/code", data={"email": email, "code": CODE},
                     follow_redirects=False)
    return resp


# ---------------------------------------------------------------------------

def test_a_user_must_exist_before_they_can_sign_in(site, fake_ourmind):
    """A valid OurMind account is not by itself an account here."""
    resp = _sign_in(site)
    assert resp.status_code == 200
    assert "vraag de beheerder" in resp.text.lower()


def test_sign_in_says_nothing_about_unknown_addresses(site, fake_ourmind):
    """The first step must not become a way to find out who has an account."""
    known = site.post("/inloggen", data={"email": "dokter@praktijk.nl"}).text
    unknown = site.post("/inloggen", data={"email": "niemand@nergens.nl"}).text
    # The page echoes back the address you typed, so compare with it removed:
    # what must not differ is anything that says whether the account exists.
    assert (known.replace("dokter@praktijk.nl", "X")
            == unknown.replace("niemand@nergens.nl", "X"))
    assert "code" in known.lower()
    # ...while behind the scenes only the real account was actually mailed.
    assert Fake.mailed == ["dokter@praktijk.nl"]


def test_a_wrong_code_does_not_sign_you_in(site, fake_ourmind, server):
    from app import users

    users.create("dokter@praktijk.nl")
    site.post("/inloggen", data={"email": "dokter@praktijk.nl"})
    resp = site.post("/inloggen/code",
                     data={"email": "dokter@praktijk.nl", "code": "000000"})
    assert resp.status_code == 200
    assert "invalid" in resp.text.lower() or "expired" in resp.text.lower()
    assert site.get("/", follow_redirects=False).status_code == 303


def test_signing_in_stores_the_token_and_fills_in_the_profile(site, fake_ourmind,
                                                              server):
    from app import db, users

    user = users.create("dokter@praktijk.nl")
    assert _sign_in(site).status_code == 303

    fresh = users.get(user["user_id"])
    assert fresh["display_name"] == "D. Schaap"
    assert fresh["org_name"] == "Praktijk Groenhouten"

    # stored, and not in the clear
    raw = db.query_one("SELECT access_token FROM user_tokens WHERE user_id = ?",
                       (user["user_id"],))["access_token"]
    assert raw.startswith("v1:")
    assert "eyJ" not in raw
    assert users.access_token(user["user_id"]).startswith("eyJ")


def test_a_user_sees_only_their_own_recordings(site, fake_ourmind, server):
    from app import users

    mine = users.create("dokter@praktijk.nl")
    theirs = users.create("collega@praktijk.nl")

    server.register_device("visitescribe-001")
    server.register_device("visitescribe-002")
    users.bind_device("visitescribe-001", mine["user_id"])
    users.bind_device("visitescribe-002", theirs["user_id"])

    ours = Recorder(server, device_id="visitescribe-001")
    ours.add_chunk(seconds=1.0)
    ours.create(); ours.upload_all(); ours.complete()

    other = Recorder(server, device_id="visitescribe-002")
    other.add_chunk(seconds=1.0)
    other.create(); other.upload_all(); other.complete()

    _sign_in(site)
    page = site.get("/").text
    assert ours.session_id in page
    assert other.session_id not in page

    # and not by guessing the address either
    assert site.get(f"/opname/{other.session_id}").status_code == 404


def test_settings_offer_the_users_own_templates(site, fake_ourmind, server):
    from app import users

    users.create("dokter@praktijk.nl")
    _sign_in(site)
    page = site.get("/instellingen").text
    assert "SOEP consult" in page
    assert "Vergaderverslag" in page
    assert "Niet beschikbaar" not in page          # available: false is filtered
    assert "288" in page and "300" in page          # the report allowance
    assert "Vergadering" in page and "Consult" in page


def test_a_recording_goes_out_under_the_users_own_token_and_template(
        site, fake_ourmind, server):
    """The whole point of the feature, checked end to end."""
    from app import processing, users

    user = users.create("dokter@praktijk.nl")
    server.register_device("visitescribe-001")
    users.bind_device("visitescribe-001", user["user_id"])
    _sign_in(site)

    # meeting -> OurMind with the meeting template, sent automatically
    site.post("/instellingen", data={
        "route__meeting": "ourmind",
        "template__meeting": "77:doctor_template",
        "auto__meeting": "on",
        "route__single_patient": "",
        "template__single_patient": "",
        "route__multi_patient": "",
        "template__multi_patient": "",
    })
    rule = users.rule(user["user_id"], "meeting")
    assert rule["route"] == "ourmind" and rule["template_id"] == "77"
    assert rule["auto"] == 1

    rec = Recorder(server, device_id="visitescribe-001", mode="meeting")
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all()
    assert rec.complete().json()["ingest_confirmed"] is True

    # queued by itself, because the user asked for that
    jobs = processing.jobs_for(rec.session_id)
    assert jobs and jobs[0]["route"] == "ourmind"

    assert processing.run_once() is True     # transcribe
    assert processing.run_once() is True     # note
    for job in processing.jobs_for(rec.session_id):
        assert job.get("error") in (None, ""), job.get("error")
        assert job["state"] == "done"

    # it really carried the chosen template, in the shape their API wants:
    # an INTEGER id, though the listing gives it as a string
    body = Fake.generate_bodies[-1]["data"]["attributes"]["template"]
    assert body == {"id": 77, "type": "doctor_template"}

    page = site.get(f"/opname/{rec.session_id}").text
    assert "besproken punten" in page


def test_a_shared_template_is_refused_with_an_explanation(site, fake_ourmind, server):
    """Their generate endpoint only accepts template|doctor_template.

    Rather than guess at an id that means something else, say so.
    """
    from app.providers import ProviderError, get as get_provider
    from app.providers.base import TranscriptResult, Usage

    client = get_provider("ourmind", token="whatever")
    transcript = TranscriptResult(text="x", language="nl", model="ourmind",
                                  segments=[], usage=Usage(), provider_ref="c-1")
    with pytest.raises(ProviderError) as caught:
        client.make_note(transcript, context={"template_id": "5",
                                              "template_type": "shared_template"})
    assert "kloon" in str(caught.value).lower()
