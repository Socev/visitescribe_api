"""Fleet management: self-enrolment with a pairing code, Wi-Fi delivered to the
recorder, and firmware over the air.

Everything goes through the real /v1 endpoints the recorder calls, the real
admin endpoints and the real user site, with the same token a recorder would
hold -- nothing is set up by writing to the database behind the API's back
unless the test is about what the database keeps.
"""
from __future__ import annotations

import hashlib

import pytest

from conftest import Recorder
from test_user_site import _sign_in, fake_ourmind, site  # noqa: F401  (fixtures)

BRIAN = "brian-a1b2c3d4e5f6"


class TokenRecorder(Recorder):
    def __init__(self, harness, token: str, **kw):
        super().__init__(harness, **kw)
        self.token = token

    def headers(self, extra=None):
        h = super().headers(extra)
        h["Authorization"] = f"Bearer {self.token}"
        return h


def _enroll(server, device_id=BRIAN, **extra):
    body = {"device_id": device_id, "software_version": "0.8.0",
            "hardware": {"mac": "a1:b2:c3:d4:e5:f6", "board": "cores3-lite"}}
    body.update(extra)
    return server.client.post("/v1/device/enroll", json=body)


def _auth(device_id, token):
    return {"X-Device-ID": device_id, "Authorization": f"Bearer {token}"}


def _config(server, device_id, token):
    r = server.client.get("/v1/device/config", headers=_auth(device_id, token))
    assert r.status_code == 200, r.text
    return r.json()


def _link(server, code, user_id=None, name="Brian test"):
    server.admin_login()
    return server.admin.post("/admin/api/pairing",
                             json={"code": code, "user_id": user_id or "",
                                   "display_name": name})


@pytest.fixture()
def enrolled(server):
    r = _enroll(server)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# enrolment and pairing
# ---------------------------------------------------------------------------

def test_a_new_recorder_enrols_itself_and_waits(server, enrolled):
    assert enrolled["device_id"] == BRIAN
    assert enrolled["token"]
    code = enrolled["enrolment"]["pairing_code"]
    assert enrolled["enrolment"]["state"] == "pending"
    assert len(code) == 6 and code.isdigit()

    # The token works, and config keeps handing back the same code.
    cfg = _config(server, BRIAN, enrolled["token"])
    assert cfg["enrolment"] == {**cfg["enrolment"], "state": "pending",
                                "pairing_code": code}
    assert cfg["upload_enabled"] is False

    # ...but a pending recorder cannot upload a thing.
    rec = TokenRecorder(server, enrolled["token"], device_id=BRIAN)
    rec.add_chunk(seconds=1.0)
    resp = rec.create()
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_PENDING"

    # The code is not stored in the clear.
    raw = server.db.query_one("SELECT pairing_code FROM devices WHERE device_id = ?",
                              (BRIAN,))["pairing_code"]
    assert code not in raw


def test_a_token_is_required_once_enrolled(server, enrolled):
    r = server.client.get("/v1/device/config", headers={"X-Device-ID": BRIAN})
    assert r.status_code == 401


def test_linking_with_the_code_activates_and_binds(server, enrolled):
    from app import users

    doctor = users.create("dokter@praktijk.nl")
    wrong = "000000" if enrolled["enrolment"]["pairing_code"] != "000000" else "111111"
    bad = _link(server, wrong, doctor["user_id"])
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_PAIRING_CODE"

    code = enrolled["enrolment"]["pairing_code"]
    ok = _link(server, f"{code[:3]} {code[3:]}", doctor["user_id"], "Brian van X")
    assert ok.status_code == 200, ok.text

    cfg = _config(server, BRIAN, enrolled["token"])
    assert cfg["enrolment"]["state"] == "active"
    assert cfg["enrolment"]["linked"] is True
    assert cfg["upload_enabled"] is True
    assert users.owner_of_device(BRIAN)["user_id"] == doctor["user_id"]

    rec = TokenRecorder(server, enrolled["token"], device_id=BRIAN)
    rec.add_chunk(seconds=1.0)
    assert rec.create().status_code in (200, 201)

    # The code is spent.
    assert _link(server, code).status_code == 400


def test_a_pending_recorder_that_lost_its_token_gets_a_new_one(server, enrolled):
    again = _enroll(server).json()
    assert again["token"] != enrolled["token"]
    old = server.client.get("/v1/device/config", headers=_auth(BRIAN, enrolled["token"]))
    assert old.status_code == 401
    assert _config(server, BRIAN, again["token"])["enrolment"]["state"] == "pending"


def test_an_active_recorder_cannot_be_taken_over_by_enrolling(server, enrolled):
    from app import users

    doctor = users.create("dokter@praktijk.nl")
    _link(server, enrolled["enrolment"]["pairing_code"], doctor["user_id"])

    hijack = _enroll(server)
    assert hijack.status_code == 409
    assert hijack.json()["error"]["code"] == "DEVICE_EXISTS"
    # The original token still works.
    assert _config(server, BRIAN, enrolled["token"])["enrolment"]["state"] == "active"

    # Once an admin allows it, the same recorder re-keys and keeps its owner.
    assert server.admin.post(f"/admin/api/devices/{BRIAN}/reenrol",
                             json={"minutes": 5}).status_code == 200
    rekey = _enroll(server)
    assert rekey.status_code == 201
    assert rekey.json()["enrolment"]["state"] == "active"
    assert users.owner_of_device(BRIAN)["user_id"] == doctor["user_id"]
    # ...exactly once.
    assert _enroll(server).status_code == 409


def test_an_enrolment_window_counts_as_prior_approval(server):
    server.admin_login()
    server.admin.post("/admin/api/devices/brian-000000000001/enrolment", json={"minutes": 5})
    r = _enroll(server, "brian-000000000001")
    assert r.status_code == 201
    assert r.json()["enrolment"]["state"] == "active"


def test_enrolment_can_be_switched_off(server, override_settings):
    with override_settings(self_enrolment=False):
        r = _enroll(server)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ENROLMENT_CLOSED"


def test_pending_devices_are_capped(server, override_settings):
    with override_settings(max_pending_devices=2, enrol_rate_per_minute=100):
        assert _enroll(server, "brian-000000000001").status_code == 201
        assert _enroll(server, "brian-000000000002").status_code == 201
        r = _enroll(server, "brian-000000000003")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ENROLMENT_LIMIT"


def test_enrolment_is_rate_limited_per_address(server, override_settings):
    with override_settings(enrol_rate_per_minute=2):
        codes = [_enroll(server, f"brian-00000000001{i}").status_code for i in range(4)]
    assert codes[:2] == [201, 201]
    assert 429 in codes[2:]


def test_enrolment_rejects_a_malformed_id(server):
    assert _enroll(server, "../../etc").status_code == 400


def test_a_pending_device_is_not_offered_for_binding(server, enrolled):
    from app import users

    assert BRIAN not in [d["device_id"] for d in users.unbound_devices()]


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------

@pytest.fixture()
def linked(server, enrolled):
    from app import users

    doctor = users.create("dokter@praktijk.nl")
    assert _link(server, enrolled["enrolment"]["pairing_code"],
                 doctor["user_id"]).status_code == 200
    return {"token": enrolled["token"], "user": doctor}


def test_a_user_adds_a_network_and_the_password_is_wiped_after_delivery(
        server, linked, site, fake_ourmind):
    assert _sign_in(site).status_code == 303
    r = site.post(f"/apparaten/{BRIAN}/wifi",
                  data={"ssid": "Praktijk", "password": "geheim-wachtwoord"},
                  follow_redirects=False)
    assert r.status_code == 303

    ops = _config(server, BRIAN, linked["token"])["wifi_ops"]
    assert ops == [{"id": ops[0]["id"], "op": "add", "ssid": "Praktijk",
                    "password": "geheim-wachtwoord"}]
    stored = server.db.query_one("SELECT secret FROM device_wifi_ops")["secret"]
    assert stored.startswith("v1:") and "geheim" not in stored

    beat = server.client.post(
        "/v1/device/heartbeat", headers=_auth(BRIAN, linked["token"]),
        json={"software_version": "0.8.0", "wifi_networks": ["Thuis", "Praktijk"],
              "wifi_ops_applied": ops[0]["id"], "charging": True})
    assert beat.status_code == 200, beat.text

    assert _config(server, BRIAN, linked["token"])["wifi_ops"] == []
    assert server.db.query_one("SELECT secret FROM device_wifi_ops")["secret"] == ""
    page = site.get("/apparaten").text
    assert "Praktijk" in page and "Thuis" in page
    assert "geheim" not in page


def test_removing_and_a_newer_change_replaces_a_waiting_one(server, linked, site,
                                                             fake_ourmind):
    _sign_in(site)
    site.post(f"/apparaten/{BRIAN}/wifi", data={"ssid": "Thuis", "password": "eerste-pw1"})
    site.post(f"/apparaten/{BRIAN}/wifi", data={"ssid": "Thuis", "password": "tweede-pw2"})
    site.post(f"/apparaten/{BRIAN}/wifi/verwijderen", data={"ssid": "Oud"})
    ops = _config(server, BRIAN, linked["token"])["wifi_ops"]
    assert [(o["op"], o["ssid"], o.get("password")) for o in ops] == [
        ("add", "Thuis", "tweede-pw2"), ("remove", "Oud", None)]
    # the superseded password is gone too
    secrets = [r["secret"] for r in server.db.query(
        "SELECT secret FROM device_wifi_ops WHERE cancelled_at IS NOT NULL")]
    assert secrets == [""]


def test_wifi_input_is_validated(server, linked, site, fake_ourmind):
    _sign_in(site)
    r = site.post(f"/apparaten/{BRIAN}/wifi", data={"ssid": "Thuis", "password": "kort"})
    assert r.status_code == 400
    assert "8 tot 63" in r.text
    r = site.post(f"/apparaten/{BRIAN}/wifi", data={"ssid": "x" * 33, "password": ""})
    assert r.status_code == 400


def test_a_user_cannot_touch_someone_elses_recorder(server, linked, site, fake_ourmind):
    from app import users

    users.create("collega@praktijk.nl")
    server.register_device("visitescribe-009")
    _sign_in(site)
    r = site.post("/apparaten/visitescribe-009/wifi",
                  data={"ssid": "Kaping", "password": "12345678"})
    assert r.status_code == 404
    assert server.db.query_one("SELECT COUNT(*) AS n FROM device_wifi_ops")["n"] == 0


def test_admin_can_queue_and_cancel_wifi(server, linked):
    r = server.admin.post(f"/admin/api/devices/{BRIAN}/wifi",
                          json={"op": "add", "ssid": "Beheer", "password": "beheer-123"})
    assert r.status_code == 200
    op_id = r.json()["pending"][0]["id"]
    r = server.admin.post(f"/admin/api/devices/{BRIAN}/wifi", json={"cancel": op_id})
    assert r.json()["pending"] == []
    assert _config(server, BRIAN, linked["token"])["wifi_ops"] == []


# ---------------------------------------------------------------------------
# firmware
# ---------------------------------------------------------------------------

def make_image(version="0.8.1", chip_id=9, size=8192, magic=0xE9,
               board="cores3-lite") -> bytes:
    head = bytearray(32)
    head[0] = magic
    head[1] = 3
    head[12:14] = chip_id.to_bytes(2, "little")
    desc = bytearray(256)
    desc[0:4] = (0xABCD5432).to_bytes(4, "little")
    desc[16:16 + len(b"esp-idf")] = b"esp-idf"
    marker = f"VSFW|version={version}|board={board}|".encode()
    body = bytes(head) + bytes(desc) + marker
    return body + bytes(size - len(body))


def _upload(server, data, version=""):
    server.admin_login()
    return server.admin.post("/admin/api/firmware", content=data,
                             headers={"Content-Type": "application/octet-stream",
                                      "X-Filename": "firmware.bin",
                                      "X-Version": version, "X-Notes": "test"})


def test_only_an_esp32s3_application_image_is_accepted(server):
    r = _upload(server, b"hello" * 100)
    assert r.status_code == 422
    boot = bytearray(make_image())
    boot[32:36] = bytes(4)                      # a bootloader has no app descriptor
    r = _upload(server, bytes(boot))
    assert r.status_code == 422 and "bootloader" in r.json()["error"]["message"]
    r = _upload(server, make_image(chip_id=0))
    assert r.status_code == 422 and "andere chip" in r.json()["error"]["message"]
    r = _upload(server, make_image(), version="9.9.9")
    assert r.status_code == 422      # the image itself says 0.8.1
    r = _upload(server, make_image())
    assert r.status_code == 201, r.text
    assert r.json()["release"]["version"] == "0.8.1"
    assert r.json()["release"]["board"] == "cores3-lite"
    assert _upload(server, make_image()).status_code == 400      # duplicate


def test_an_update_travels_to_the_recorder_and_completes_on_its_return(server, linked):
    image = make_image()
    release = _upload(server, image).json()["release"]
    r = server.admin.post(f"/admin/api/devices/{BRIAN}/firmware",
                          json={"release_id": release["release_id"]})
    assert r.json()["update"]["state"] == "pending"

    upd = _config(server, BRIAN, linked["token"])["firmware_update"]
    assert upd["version"] == "0.8.1"
    assert upd["sha256"] == hashlib.sha256(image).hexdigest()
    assert upd["size"] == len(image)
    assert upd["min_battery_charging"] >= 20
    assert upd["min_battery_unplugged"] >= 20

    # Conditions not met: the recorder says so and keeps it on offer.
    server.client.post("/v1/device/firmware/report", headers=_auth(BRIAN, linked["token"]),
                       json={"release_id": release["release_id"], "state": "deferred",
                             "detail": "battery 12%, not charging"})
    assert "firmware_update" in _config(server, BRIAN, linked["token"])

    dl = server.client.get(upd["url"], headers=_auth(BRIAN, linked["token"]))
    assert dl.status_code == 200
    assert dl.content == image
    assert dl.headers["x-firmware-sha256"] == upd["sha256"]

    # "About to reboot" is not "installed".
    server.client.post("/v1/device/firmware/report", headers=_auth(BRIAN, linked["token"]),
                       json={"release_id": release["release_id"], "state": "installing"})
    server.client.post("/v1/device/heartbeat", headers=_auth(BRIAN, linked["token"]),
                       json={"software_version": "0.8.0"})
    assert "firmware_update" in _config(server, BRIAN, linked["token"])

    server.client.post("/v1/device/heartbeat", headers=_auth(BRIAN, linked["token"]),
                       json={"software_version": "0.8.1"})
    assert "firmware_update" not in _config(server, BRIAN, linked["token"])
    from app import fleet
    assert fleet.update_of(BRIAN)["state"] == "installed"


def test_a_device_can_only_download_what_was_set_out_for_it(server, linked):
    release = _upload(server, make_image()).json()["release"]
    other = _enroll(server, "brian-0000000000ff").json()
    r = server.client.get(f"/v1/device/firmware/{release['release_id']}",
                          headers=_auth("brian-0000000000ff", other["token"]))
    assert r.status_code == 404
    r = server.client.get(f"/v1/device/firmware/{release['release_id']}",
                          headers=_auth(BRIAN, linked["token"]))
    assert r.status_code == 404      # not assigned yet either


def test_failed_updates_stop_being_offered_after_the_limit(server, linked,
                                                             override_settings):
    release = _upload(server, make_image()).json()["release"]
    server.admin.post(f"/admin/api/devices/{BRIAN}/firmware",
                      json={"release_id": release["release_id"]})
    with override_settings(ota_max_attempts=2):
        for _ in range(2):
            server.client.get(f"/v1/device/firmware/{release['release_id']}",
                              headers=_auth(BRIAN, linked["token"]))
            server.client.post("/v1/device/firmware/report",
                               headers=_auth(BRIAN, linked["token"]),
                               json={"release_id": release["release_id"],
                                     "state": "failed", "detail": "sha mismatch"})
        assert "firmware_update" not in _config(server, BRIAN, linked["token"])


def test_cancel_and_delete(server, linked):
    release = _upload(server, make_image()).json()["release"]
    server.admin.post(f"/admin/api/firmware/{release['release_id']}/assign",
                      json={"device_ids": [BRIAN]})
    r = server.admin.delete(f"/admin/api/firmware/{release['release_id']}")
    assert r.status_code == 400          # still set out
    server.admin.post(f"/admin/api/devices/{BRIAN}/firmware", json={"cancel": True})
    assert "firmware_update" not in _config(server, BRIAN, linked["token"])
    assert server.admin.delete(
        f"/admin/api/firmware/{release['release_id']}").status_code == 200


def test_the_ota_policy_never_goes_below_twenty_percent(server, linked, override_settings):
    release = _upload(server, make_image()).json()["release"]
    server.admin.post(f"/admin/api/devices/{BRIAN}/firmware",
                      json={"release_id": release["release_id"]})
    with override_settings(ota_min_battery_charging=5, ota_min_battery_unplugged=10):
        upd = _config(server, BRIAN, linked["token"])["firmware_update"]
    assert upd["min_battery_charging"] == 20
    assert upd["min_battery_unplugged"] == 20


def test_an_image_only_goes_to_a_recorder_of_its_own_board(server, linked):
    """CoreS3-Lite and StickS3 have different flash layouts (1.12.0)."""
    stick_id = "brian-0000000000aa"
    stick = _enroll(server, stick_id,
                    hardware={"mac": "00:00:00:00:00:aa", "board": "sticks3"}).json()
    from app import users
    doctor = users.get(linked["user"]["user_id"])
    assert _link(server, stick["enrolment"]["pairing_code"],
                 doctor["user_id"]).status_code == 200

    cores3 = _upload(server, make_image("0.10.1")).json()["release"]
    stick_rel = _upload(server, make_image("0.10.1s", board="sticks3")).json()["release"]
    assert stick_rel["board"] == "sticks3"

    r = server.admin.post(f"/admin/api/devices/{stick_id}/firmware",
                          json={"release_id": cores3["release_id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "WRONG_BOARD"
    r = server.admin.post(f"/admin/api/devices/{BRIAN}/firmware",
                          json={"release_id": stick_rel["release_id"]})
    assert r.status_code == 409

    # A mixed selection assigns nothing at all.
    r = server.admin.post(f"/admin/api/firmware/{cores3['release_id']}/assign",
                          json={"device_ids": [BRIAN, stick_id]})
    assert r.status_code == 409
    from app import fleet
    assert fleet.update_of(BRIAN) is None

    r = server.admin.post(f"/admin/api/devices/{stick_id}/firmware",
                          json={"release_id": stick_rel["release_id"]})
    assert r.status_code == 200
    upd = _config(server, stick_id, stick["token"])["firmware_update"]
    assert upd["board"] == "sticks3"

    # A recorder that never reported a board is a CoreS3-Lite.
    server.db.execute("UPDATE devices SET hardware_json = '{}' WHERE device_id = ?", (BRIAN,))
    r = server.admin.post(f"/admin/api/devices/{BRIAN}/firmware",
                          json={"release_id": cores3["release_id"]})
    assert r.status_code == 200
    assert "sticks3" in server.admin.get("/admin/firmware").text


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def test_admin_pages_render(server, enrolled):
    server.admin_login()
    page = server.admin.get("/admin/devices").text
    assert "wachten op koppeling" in page and BRIAN in page
    assert enrolled["enrolment"]["pairing_code"] not in page   # the code is on the device
    _upload(server, make_image())
    assert "0.8.1" in server.admin.get("/admin/firmware").text
    detail = server.admin.get(f"/admin/devices/{BRIAN}")
    assert detail.status_code == 200 and "Wi-Fi" in detail.text


def test_the_old_recorder_keeps_working_untouched(server, recorder):
    """A hand-registered device (enrol_state NULL) is active, as before."""
    recorder.add_chunk(seconds=1.0)
    assert recorder.create().status_code in (200, 201)
    cfg = server.client.get("/v1/device/config",
                            headers={"X-Device-ID": "visitescribe-001"}).json()
    assert cfg["enrolment"]["state"] == "active"
    assert cfg["upload_enabled"] is True
    assert cfg["wifi_ops"] == []


def test_the_real_release_image_is_recognised():
    """The image PlatformIO actually builds, when it is available locally."""
    import os
    from pathlib import Path

    path = Path(os.environ.get("VS_TEST_FIRMWARE_BIN", "/tmp/brian-0.8.0.bin"))
    if not path.is_file():
        pytest.skip("no locally built firmware.bin")
    from app import fleet

    info = fleet.inspect_image(path.read_bytes())
    assert info["chip"] == "esp32s3"
    assert info["version"] and info["board"] == "cores3-lite"


def test_a_factory_reset_recorder_enrols_again_with_its_old_token(server, linked):
    """Wiped on the device itself: it proves it is the same box with the token
    it held, goes back to pending with a new code, and keeps its owner."""
    from app import users

    old = linked["token"]
    wrong = server.client.post("/v1/device/enroll",
                               json={"device_id": BRIAN, "software_version": "0.8.0"},
                               headers={"Authorization": "Bearer not-the-token"})
    assert wrong.status_code == 409

    r = server.client.post("/v1/device/enroll",
                           json={"device_id": BRIAN, "software_version": "0.8.0"},
                           headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["enrolment"]["state"] == "pending"
    assert users.owner_of_device(BRIAN)["user_id"] == linked["user"]["user_id"]
    # the old token is dead, uploads wait for the admin
    assert server.client.get("/v1/device/config",
                             headers=_auth(BRIAN, old)).status_code == 401
    rec = TokenRecorder(server, body["token"], device_id=BRIAN)
    rec.add_chunk(seconds=1.0)
    assert rec.create().json()["error"]["code"] == "DEVICE_PENDING"
    # linking without choosing a user keeps the owner it had
    assert _link(server, body["enrolment"]["pairing_code"]).status_code == 200
    assert users.owner_of_device(BRIAN)["user_id"] == linked["user"]["user_id"]
    assert _config(server, BRIAN, body["token"])["upload_enabled"] is True


def test_the_recorder_learns_whose_ourmind_account_it_serves(server, linked):
    cfg = _config(server, BRIAN, linked["token"])
    owner = cfg["enrolment"]["owner"]
    assert owner["email"] == "dokter@praktijk.nl"
    assert owner["ourmind"] is False            # not signed in to OurMind yet
    from app import users
    users.store_token(linked["user"]["user_id"], "access", "refresh", None)
    assert _config(server, BRIAN, linked["token"])["enrolment"]["owner"]["ourmind"] is True
    users.bind_device(BRIAN, None)
    cfg = _config(server, BRIAN, linked["token"])
    assert cfg["enrolment"]["owner"] is None and cfg["enrolment"]["linked"] is False


# ---------------------------------------------------------------------------
# recorder logs
# ---------------------------------------------------------------------------

def _post_log(server, token, text, request_id=None):
    headers = {**_auth(BRIAN, token), "Content-Type": "text/plain"}
    if request_id:
        headers["X-Log-Request"] = str(request_id)
    return server.client.post("/v1/device/logs", content=text.encode(), headers=headers)


def test_log_lines_arrive_once_and_are_shown_to_the_admin(server, linked):
    tok = linked["token"]
    batch = ("B3.1 2026-09-29T12:00:00Z 1200 BOOT: reset_reason=3\n"
             "B3.2 - 1300 FLEET: seen SehrToll rssi=-61 dBm auth=3 ch=6\n"
             "garbage line\n")
    r = _post_log(server, tok, batch)
    assert r.status_code == 200
    assert r.json()["stored"] == 2 and r.json()["rejected"] == 1
    assert r.json()["highest"] == {"boot": 3, "line": 2}
    # resent after a dropped reply: nothing doubles
    assert _post_log(server, tok, batch).json()["stored"] == 0
    _post_log(server, tok, "B4.1 - 10 WIFI: connected\n")

    server.admin_login()
    lines = server.admin.get(f"/admin/api/devices/{BRIAN}/logs").json()["lines"]
    assert [(l["boot"], l["line"]) for l in lines] == [(3, 1), (3, 2), (4, 1)]
    only = server.admin.get(f"/admin/api/devices/{BRIAN}/logs?q=SehrToll").json()["lines"]
    assert len(only) == 1 and "SehrToll" in only[0]["text"]
    txt = server.admin.get(f"/admin/api/devices/{BRIAN}/logs.txt").text
    assert txt.splitlines()[0].startswith("B3.1 2026-09-29T12:00:00Z 1200 BOOT")
    assert "Logboek" in server.admin.get(f"/admin/devices/{BRIAN}").text


def test_a_full_log_request_travels_in_config_until_answered(server, linked):
    tok = linked["token"]
    assert "log_request" not in _config(server, BRIAN, tok)
    server.admin_login()
    rid = server.admin.post(f"/admin/api/devices/{BRIAN}/logs/request", json={}).json()["request_id"]
    assert _config(server, BRIAN, tok)["log_request"] == {"id": rid}
    _post_log(server, tok, "B1.1 - 5 old line\n", request_id=rid)
    assert "log_request" not in _config(server, BRIAN, tok)


def test_log_uploads_are_bounded_and_authenticated(server, linked, override_settings):
    r = server.client.post("/v1/device/logs", content=b"B1.1 - 1 x\n",
                           headers={"X-Device-ID": BRIAN})
    assert r.status_code == 401
    with override_settings(device_log_max_upload=100):
        r = _post_log(server, linked["token"], "B1.1 - 1 " + "x" * 200 + "\n")
    assert r.status_code == 413
    with override_settings(device_log_max_lines=3):
        _post_log(server, linked["token"], "".join(f"B1.{i} - {i} l{i}\n" for i in range(1, 6)))
    server.admin_login()
    kept = server.admin.get(f"/admin/api/devices/{BRIAN}/logs").json()["lines"]
    assert [l["line"] for l in kept] == [3, 4, 5]
