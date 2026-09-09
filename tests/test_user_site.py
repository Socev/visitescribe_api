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
    # "test-token" stands in for a pod-wide credential; a per-user token is
    # minted by verify and added to this set when someone signs in.
    Fake.issued, Fake.mailed, Fake.generate_bodies = {"test-token"}, [], []
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


# ---------------------------------------------------------------------------
# the admin side of the same feature
#
# These exist because they did not, and the "Toevoegen" button shipped calling
# a guard that was never defined. It failed silently in the browser: the
# handler had no .catch(), so a rejected promise showed nothing at all. Both
# halves are now covered -- the endpoints here, and every button routed
# through act(), which reports the error instead of swallowing it.
# ---------------------------------------------------------------------------

def test_admin_can_add_a_user_and_bind_a_device(server):
    from app import users

    server.register_device("visitescribe-001")
    server.admin_login()

    created = server.admin.post("/admin/api/users",
                                json={"email": "D.Schaap@Gmail.com "})
    assert created.status_code == 201, created.text
    user_id = created.json()["user"]["user_id"]
    assert created.json()["user"]["email"] == "d.schaap@gmail.com"

    bound = server.admin.post("/admin/api/devices/visitescribe-001/owner",
                              json={"user_id": user_id})
    assert bound.status_code == 200, bound.text
    assert [d["device_id"] for d in users.devices_of(user_id)] == ["visitescribe-001"]
    assert users.unbound_devices() == []

    page = server.admin.get("/admin/users").text
    assert "d.schaap@gmail.com" in page and "visitescribe-001" in page

    # unbinding puts it back on the unattached list
    assert server.admin.post("/admin/api/devices/visitescribe-001/owner",
                             json={"user_id": ""}).status_code == 200
    assert [d["device_id"] for d in users.unbound_devices()] == ["visitescribe-001"]


def test_adding_a_user_reports_real_errors(server):
    server.admin_login()
    server.admin.post("/admin/api/users", json={"email": "dokter@praktijk.nl"})

    again = server.admin.post("/admin/api/users", json={"email": "dokter@praktijk.nl"})
    assert again.status_code >= 400
    assert "bestaat al" in again.json()["error"]["message"]

    bad = server.admin.post("/admin/api/users", json={"email": "geen-adres"})
    assert bad.status_code >= 400
    assert "e-mailadres" in bad.json()["error"]["message"]


def test_user_admin_endpoints_require_a_signed_in_admin(server):
    for method, url, body in (
        ("post", "/admin/api/users", {"email": "x@y.nl"}),
        ("post", "/admin/api/users/whatever/enabled", {"enabled": False}),
        ("post", "/admin/api/devices/visitescribe-001/owner", {"user_id": ""}),
    ):
        resp = getattr(server.admin, method)(url, json=body)
        assert resp.status_code in (401, 403), f"{url} -> {resp.status_code}"


def test_a_disabled_user_cannot_sign_in(site, fake_ourmind, server):
    from app import users

    user = users.create("dokter@praktijk.nl")
    server.admin_login()
    server.admin.post(f"/admin/api/users/{user['user_id']}/enabled",
                      json={"enabled": False})

    resp = _sign_in(site)
    assert resp.status_code == 200
    assert "uitgeschakeld" in resp.text.lower()


def test_admin_can_pick_a_template_for_one_run(site, fake_ourmind, server):
    """Choosing OurMind by hand must also let you choose the template.

    It did not: the panel had a route picker and nothing else, so a manual run
    silently used whatever standing rule existed -- or none.
    """
    from app import processing, users

    user = users.create("dokter@praktijk.nl")
    server.register_device("visitescribe-001")
    users.bind_device("visitescribe-001", user["user_id"])
    _sign_in(site)

    # a standing rule that the one-off choice must be able to override
    users.set_rule(user["user_id"], "single_patient", route="ourmind",
                   template_id="13", template_type="template")

    rec = Recorder(server, device_id="visitescribe-001")
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()

    server.admin_login()
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    assert 'id="template"' in page
    assert "SOEP consult" in page and "Vergaderverslag" in page
    assert 'value="13:template" selected' in page      # the standing rule
    # ...and the panel opens on the route that rule names, not on whichever
    # provider happens to sort first. Opening on a route nobody chose is what
    # makes the template beside it look empty for the wrong reason.
    assert '<option value="ourmind" selected>' in page

    server.admin.post(f"/admin/api/sessions/{rec.session_id}/processing",
                      json={"route": "ourmind", "template_id": "77",
                            "template_type": "doctor_template"})
    assert processing.run_once() is True     # transcribe
    assert processing.run_once() is True     # note

    used = Fake.generate_bodies[-1]["data"]["attributes"]["template"]
    assert used == {"id": 77, "type": "doctor_template"}, "one-off choice ignored"


def test_the_template_picker_explains_itself_when_empty(server):
    """An empty picker with no reason gets reported as "it shows nothing"."""
    server.register_device("visitescribe-001")
    rec = Recorder(server, device_id="visitescribe-001")
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()

    server.admin_login()
    page = server.admin.get(f"/admin/sessions/{rec.session_id}").text
    assert "geen gebruiker aan dit device gekoppeld" in page


def test_the_global_kill_switch_announces_itself(site, fake_ourmind, server,
                                                 override_settings):
    """A server switch that disables a user's checkbox must say so.

    The chart shipped with VS_AUTO_PROCESS=false, which turned every user's
    "meteen versturen" into a checkbox that saved fine and did nothing.
    """
    from app import users

    users.create("dokter@praktijk.nl")
    _sign_in(site)

    assert "staat op deze server uitgeschakeld" not in site.get("/instellingen").text

    with override_settings(auto_process=False):
        page = site.get("/instellingen").text
    assert "staat op deze server uitgeschakeld" in page


# ---------------------------------------------------------------------------
# what David hit on a real patient round
# ---------------------------------------------------------------------------

def test_patients_are_numbered_as_the_recorder_numbered_them(site, fake_ourmind,
                                                             server):
    """The first consultation of a round was labelled "Patiënt 2".

    patient_segments already numbers from 1; the page added one on top, so
    every note carried the NEXT patient's number. In a consulting room that is
    not a cosmetic bug.
    """
    from app import processing, users

    user = users.create("dokter@praktijk.nl")
    server.register_device("visitescribe-001")
    users.bind_device("visitescribe-001", user["user_id"])
    _sign_in(site)

    rec = Recorder(server, device_id="visitescribe-001", mode="multi_patient")
    for i in range(4):
        rec.add_chunk(seconds=1.0, seed=i)
    rec.create(); rec.upload_all()
    rec.send_events([{"event": "patient_boundary", "offset_ms": 1000},
                     {"event": "patient_boundary", "offset_ms": 2000}])
    rec.complete()

    processing.enqueue(rec.session_id, "ourmind", actor="test")
    for _ in range(8):
        processing.run_once()

    page = site.get(f"/opname/{rec.session_id}").text
    assert "Patiënt 1" in page
    assert "Patiënt 2" in page
    assert "Patiënt 3" in page
    assert "Patiënt 4" not in page          # three segments, not four

    # the first block on the page really is patient 1
    assert page.index("Patiënt 1") < page.index("Patiënt 2") < page.index("Patiënt 3")


def test_a_job_left_running_by_a_restart_is_picked_up_again(server, fake_ourmind,
                                                           monkeypatch):
    """A pod restart mid-job stranded the recording on "running" for ever.

    Nothing ever touched that row again: no error, no retry, no explanation.
    This is the case David hit by upgrading while a note was being generated.
    """
    from app import processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()
    processing.enqueue(rec.session_id, "ourmind", actor="test")

    # claim it and then "die"
    server.db.execute("UPDATE processing_jobs SET state = 'running' "
                      "WHERE session_id = ?", (rec.session_id,))
    assert processing.run_once() is False          # nothing is claimable

    assert processing.requeue_orphans(reason="test") == 1
    job = processing.jobs_for(rec.session_id)[0]
    assert job["state"] == "queued"
    assert "Onderbroken" in (job["error"] or "")
    assert processing.run_once() is True           # and it runs


def test_a_job_running_far_too_long_is_reclaimed(server, fake_ourmind,
                                                 monkeypatch):
    """The other net: the thread died but the process lived."""
    from app import processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()
    processing.enqueue(rec.session_id, "ourmind", actor="test")

    server.db.execute(
        "UPDATE processing_jobs SET state = 'running', started_at = ? "
        "WHERE session_id = ?", ("2020-01-01T00:00:00Z", rec.session_id))

    assert processing.reclaim_stale(max_age_seconds=1800) == 1
    job = processing.jobs_for(rec.session_id)[0]
    assert job["state"] == "queued"
    assert "langer dan 30 minuten" in (job["error"] or "")

    # a job that has only just started is left alone
    server.db.execute("UPDATE processing_jobs SET state = 'running', started_at = ? "
                      "WHERE session_id = ?", (__import__("app.util", fromlist=["x"])
                                               .now_iso(), rec.session_id))
    assert processing.reclaim_stale(max_age_seconds=1800) == 0


def test_the_page_offers_to_copy_and_watches_for_changes(site, fake_ourmind,
                                                         server):
    from app import processing, users

    user = users.create("dokter@praktijk.nl")
    server.register_device("visitescribe-001")
    users.bind_device("visitescribe-001", user["user_id"])
    _sign_in(site)

    rec = Recorder(server, device_id="visitescribe-001")
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()
    processing.enqueue(rec.session_id, "ourmind", actor="test")
    processing.run_once(); processing.run_once()

    page = site.get(f"/opname/{rec.session_id}").text
    assert page.count("kopieer") >= 2          # note and transcript
    assert "watch('/api/stand?opname=" in page
    # The helpers must be defined BEFORE the inline call that uses them. They
    # were not, and the browser said "watch is not defined" while every test
    # still passed -- HTML order is not something an assertion on content sees.
    assert page.index("function watch(") < page.index("watch('/api/stand")
    assert page.index("function copyBlock(") < page.index('onclick="copyBlock(')

    first = site.get("/api/stand").json()["stand"]
    assert first == site.get("/api/stand").json()["stand"]      # stable

    detail = site.get(f"/api/stand?opname={rec.session_id}").json()["stand"]
    server.db.execute("UPDATE processing_jobs SET updated_at = ? "
                      "WHERE session_id = ?", ("2030-01-01T00:00:00Z", rec.session_id))
    assert site.get(f"/api/stand?opname={rec.session_id}").json()["stand"] != detail


def test_the_status_endpoint_is_scoped_to_the_signed_in_user(site, fake_ourmind,
                                                             server):
    from app import users

    mine = users.create("dokter@praktijk.nl")
    theirs = users.create("collega@praktijk.nl")
    server.register_device("visitescribe-001")
    server.register_device("visitescribe-002")
    users.bind_device("visitescribe-001", mine["user_id"])
    users.bind_device("visitescribe-002", theirs["user_id"])
    _sign_in(site)

    other = Recorder(server, device_id="visitescribe-002")
    other.add_chunk(seconds=1.0)
    other.create(); other.upload_all(); other.complete()

    assert site.get(f"/api/stand?opname={other.session_id}").status_code == 404


# ---------------------------------------------------------------------------
# the worker itself
#
# Every test so far called processing.run_once() directly, so the loop that
# actually drains the queue in production had never been run by a test at all.
# That is the same gap that let the Mistral client ship without ever sending
# its audio.
# ---------------------------------------------------------------------------

def test_the_worker_loop_really_drains_the_queue(server, fake_ourmind,
                                                 monkeypatch):
    import asyncio

    from app import main, processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()
    processing.enqueue(rec.session_id, "ourmind", actor="test")

    async def drive():
        task = asyncio.create_task(main._worker_loop())
        for _ in range(100):
            await asyncio.sleep(0.05)
            jobs = processing.jobs_for(rec.session_id)
            if jobs and all(j["state"] in ("done", "failed") for j in jobs):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    jobs = processing.jobs_for(rec.session_id)
    assert jobs, "the worker created no jobs"
    for job in jobs:
        assert job["state"] == "done", f"{job['stage']}: {job.get('error')}"


def test_the_worker_reports_that_it_is_alive(server):
    from app import processing

    health = processing.worker_health()
    assert health["never_started"] is True
    assert health["stalled"] is False        # nothing queued, so not stalled

    processing.beat()
    health = processing.worker_health()
    assert health["never_started"] is False
    assert health["seconds_since"] < 5


def test_a_silent_worker_with_work_waiting_is_reported_as_stalled(server,
                                                                  fake_ourmind,
                                                                  monkeypatch):
    """A dead worker used to look exactly like a slow provider."""
    from app import db, processing

    monkeypatch.setenv("VS_OURMIND_TOKEN", "test-token")
    server.register_device("visitescribe-001")
    rec = Recorder(server)
    rec.add_chunk(seconds=1.0)
    rec.create(); rec.upload_all(); rec.complete()

    processing.enqueue(rec.session_id, "ourmind", actor="test", force=True)

    # a heartbeat from long ago, with a job due
    db.set_meta(processing.HEARTBEAT_KEY, "2020-01-01T00:00:00Z")
    health = processing.worker_health()
    assert health["queued_due"] >= 1
    assert health["stalled"] is True

    server.admin_login()
    page = server.admin.get("/admin/costs").text
    assert "niet gemeld" in page

    body = server.client.get("/readyz").json()
    assert body["status"] == "degraded"
    assert any("worker last seen" in p for p in body["problems"])
