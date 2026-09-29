"""Fleet management: how a recorder joins, gets its Wi-Fi, and gets updated.

Three things live here, all driven through the recorder's normal
``/v1/device/config`` + ``/v1/device/heartbeat`` round trip so the recorder
needs no extra connection and no push channel:

**Self-enrolment.** A new recorder announces itself (``POST /v1/device/enroll``)
and receives its own token. It is then *pending*: authenticated, but unable to
upload anything, and it shows a six-digit pairing code on its screen. An admin
types that code into the admin interface and picks the user the recorder
belongs to. Only then does it become active. The code proves the admin is
linking the box in front of them, not whichever box happened to call in.

**Wi-Fi.** A user (or admin) queues ``add``/``remove`` operations. They travel
to the recorder in the config response, the recorder applies them in order and
reports the highest id it applied. The password is sealed while in flight and
blanked as soon as the recorder confirms it: the server keeps SSIDs, never a
standing list of passwords.

**Firmware over the air.** An admin uploads a firmware image, which is checked
to really be an ESP32-S3 application image, and sets it out for one or more
recorders. The config response then carries the update and the conditions for
installing it (battery, charger). The recorder enforces those conditions --
the server cannot see the battery at the moment of installing -- and reports
progress. The update counts as installed only when the recorder comes back
reporting the new version, not when it says it is about to reboot.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import audit, db, secretbox, storage, users
from .config import settings
from .errors import ApiError
from .util import is_device_id, new_token, now, now_iso, parse_iso, sha256_hex, token_hash

PENDING = "pending"
ACTIVE = "active"

MAX_REPORTED_NETWORKS = 16
MAX_DEVICE_NETWORKS = 8          # what the recorder can hold; shown to users

UPDATE_OPEN_STATES = ("pending", "downloading", "installing", "deferred", "failed")
UPDATE_STATES = UPDATE_OPEN_STATES + ("installed", "cancelled")
REPORTABLE_STATES = ("downloading", "installing", "deferred", "failed")


def _iso_in(hours: float) -> str:
    return (now() + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _device(device_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        raise ApiError("INVALID_REQUEST", "Onbekend apparaat", status_code=404)
    return dict(row)


def is_pending(device: dict[str, Any]) -> bool:
    return (device.get("enrol_state") or "") == PENDING


# ---------------------------------------------------------------------------
# enrolment and pairing
# ---------------------------------------------------------------------------

def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hardware(raw: Any) -> str:
    """Keep what the recorder says about itself, bounded and flat."""
    if not isinstance(raw, dict):
        return "{}"
    clean: dict[str, str] = {}
    for key, value in list(raw.items())[:12]:
        if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
            clean[key[:32]] = str(value)[:80]
    return json.dumps(clean)


def _open_window(device_id: str) -> bool:
    row = db.query_one("SELECT expires_at FROM enrolment_windows WHERE device_id = ?",
                       (device_id,))
    if row is None:
        return False
    expires = parse_iso(row["expires_at"])
    return expires is not None and expires > now()


def enroll(device_id: str, *, hardware: Any = None, software_version: str | None = None,
           source_ip: str = "", previous_token: str | None = None) -> dict[str, Any]:
    """Register a recorder that announced itself, or re-key one allowed to.

    Returns the token exactly once. Three cases:

    * unknown device ID: created *pending*, with a pairing code -- unless an
      admin opened an enrolment window for that ID, which counts as prior
      approval and makes it active straight away;
    * known and still pending: the recorder evidently lost its token (reset,
      reflashed); it gets a new token and a new code, nothing else changes;
    * known and active: refused, unless an admin opened an enrolment window
      for it ("opnieuw laten aanmelden"). Then it is re-keyed and keeps its
      owner, name and history;
    * known and active, and the request carries the token it held before a
      factory reset on the recorder itself: the device proves it is the same
      box, so it is re-keyed and goes back to *pending* with a new pairing
      code. Its owner, name and history stay until an admin links it again.
    """
    if not settings.self_enrolment:
        audit.log("device", "self_enrol_refused", "failure", device_id=device_id,
                  source_ip=source_ip, detail={"reason": "disabled"})
        raise ApiError("ENROLMENT_CLOSED", "Self-enrolment is disabled on this server")
    if not is_device_id(device_id):
        raise ApiError("INVALID_REQUEST", "device_id is malformed")

    token = new_token()
    ts = now_iso()
    version = (software_version or "")[:64] or None
    window = _open_window(device_id)
    existing = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))

    if existing is not None:
        device = dict(existing)
        if not device.get("enabled") or device.get("revoked_at"):
            raise ApiError("DEVICE_DISABLED", "Device is disabled or revoked")
        proven = bool(previous_token) and bool(device.get("token_hash")) and \
            hmac.compare_digest(token_hash(previous_token), device["token_hash"])
        if not is_pending(device) and not window and proven:
            code = _new_code()
            db.execute(
                "UPDATE devices SET token_hash = ?, token_hint = ?, allow_header_only = 0, "
                "enrol_state = ?, upload_enabled = 0, pairing_code = ?, "
                "pairing_expires_at = ?, hardware_json = ?, wifi_networks_json = NULL, "
                "software_version = COALESCE(?, software_version), updated_at = ? "
                "WHERE device_id = ?",
                (token_hash(token), token[:6] + "…", PENDING, secretbox.seal(code),
                 _iso_in(settings.pairing_code_hours), _hardware(hardware), version, ts,
                 device_id))
            audit.log("device", "factory_reset_reenrol", "success", device_id=device_id,
                      source_ip=source_ip, detail={"user_id": device.get("user_id")})
            return {"device_id": device_id, "token": token,
                    "enrolment": enrolment_block(_device(device_id)),
                    "server_time": now_iso()}
        if not is_pending(device) and not window:
            audit.log("device", "self_enrol_refused", "failure", device_id=device_id,
                      source_ip=source_ip, detail={"reason": "already_registered"})
            raise ApiError(
                "DEVICE_EXISTS",
                "This device is already registered. An administrator must allow "
                "it to enrol again.")
        code = _new_code() if is_pending(device) else None
        db.execute(
            "UPDATE devices SET token_hash = ?, token_hint = ?, allow_header_only = 0, "
            "pairing_code = ?, pairing_expires_at = ?, hardware_json = ?, "
            "software_version = COALESCE(?, software_version), updated_at = ? "
            "WHERE device_id = ?",
            (token_hash(token), token[:6] + "…",
             secretbox.seal(code) if code else None,
             _iso_in(settings.pairing_code_hours) if code else None,
             _hardware(hardware), version, ts, device_id))
        if window:
            db.execute("DELETE FROM enrolment_windows WHERE device_id = ?", (device_id,))
        audit.log("device", "self_enrol_rekeyed", "success", device_id=device_id,
                  source_ip=source_ip,
                  detail={"state": device.get("enrol_state") or ACTIVE,
                          "via_window": window})
    else:
        pending = db.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE enrol_state = ?", (PENDING,))
        if not window and pending and int(pending["n"]) >= settings.max_pending_devices:
            audit.log("device", "self_enrol_refused", "failure", device_id=device_id,
                      source_ip=source_ip, detail={"reason": "too_many_pending"})
            raise ApiError("ENROLMENT_LIMIT",
                           "Too many devices are waiting to be linked; try again later")
        code = None if window else _new_code()
        db.execute(
            "INSERT INTO devices(device_id, display_name, enabled, allow_header_only, "
            "token_hash, token_hint, config_json, upload_enabled, enrol_state, "
            "pairing_code, pairing_expires_at, hardware_json, software_version, "
            "created_at, updated_at) VALUES(?,?,1,0,?,?,'{}',?,?,?,?,?,?,?,?)",
            (device_id, device_id, token_hash(token), token[:6] + "…",
             1 if window else 0, ACTIVE if window else PENDING,
             secretbox.seal(code) if code else None,
             _iso_in(settings.pairing_code_hours) if code else None,
             _hardware(hardware), version, ts, ts))
        if window:
            db.execute("DELETE FROM enrolment_windows WHERE device_id = ?", (device_id,))
        audit.log("device", "self_enrolled", "success", device_id=device_id,
                  source_ip=source_ip,
                  detail={"state": ACTIVE if window else PENDING, "via_window": window})

    return {
        "device_id": device_id,
        "token": token,
        "enrolment": enrolment_block(_device(device_id)),
        "server_time": now_iso(),
    }


def enrolment_block(device: dict[str, Any]) -> dict[str, Any]:
    """What a recorder needs to show about its own enrolment.

    A pending recorder gets its pairing code back on every config read, so it
    never has to keep it; an expired code is replaced here, lazily.
    """
    if not is_pending(device):
        return {"state": ACTIVE, "linked": bool(device.get("user_id")),
                "owner": owner_block(device)}
    code = ""
    try:
        code = secretbox.open_(device.get("pairing_code"))
    except ValueError:
        code = ""
    expires = parse_iso(device.get("pairing_expires_at"))
    if not code or expires is None or expires <= now():
        code = _new_code()
        new_expiry = _iso_in(settings.pairing_code_hours)
        db.execute("UPDATE devices SET pairing_code = ?, pairing_expires_at = ? "
                   "WHERE device_id = ?",
                   (secretbox.seal(code), new_expiry, device["device_id"]))
        device["pairing_expires_at"] = new_expiry
    return {"state": PENDING, "pairing_code": code,
            "pairing_expires_at": device.get("pairing_expires_at")}


def owner_block(device: dict[str, Any]) -> dict[str, Any] | None:
    """Who this recorder belongs to, for its own screen.

    The user id here *is* the OurMind account (users are keyed by their
    OurMind e-mail), so the address is the OurMind login the recordings go to.
    `ourmind` says whether that user is signed in to OurMind right now, i.e.
    whether a recording could actually be delivered there.
    """
    user_id = device.get("user_id")
    if not user_id:
        return None
    user = users.get(user_id)
    if user is None:
        return None
    try:
        status = users.token_status(user_id)
        connected = bool(status.get("present")) and (
            not status.get("expired") or bool(status.get("refreshable")))
    except (ValueError, TypeError):
        connected = False
    return {"email": user["email"], "name": user.get("display_name") or "",
            "ourmind": connected}


def pending_devices() -> list[dict[str, Any]]:
    rows = []
    for r in db.query("SELECT * FROM devices WHERE enrol_state = ? ORDER BY created_at DESC",
                      (PENDING,)):
        item = dict(r)
        item.pop("token_hash", None)
        item.pop("pairing_code", None)
        try:
            item["hardware"] = json.loads(item.get("hardware_json") or "{}")
        except ValueError:
            item["hardware"] = {}
        rows.append(item)
    return rows


def link(code: str, *, user_id: str | None, display_name: str = "",
         actor: str = "admin") -> dict[str, Any]:
    """Activate the pending recorder showing `code` and hand it to a user."""
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        raise ApiError("INVALID_PAIRING_CODE", "Een koppelcode bestaat uit 6 cijfers.")
    if user_id and users.get(user_id) is None:
        raise ApiError("INVALID_REQUEST", "Onbekende gebruiker")
    match: dict[str, Any] | None = None
    for r in db.query("SELECT * FROM devices WHERE enrol_state = ?", (PENDING,)):
        device = dict(r)
        try:
            stored = secretbox.open_(device.get("pairing_code"))
        except ValueError:
            continue
        expires = parse_iso(device.get("pairing_expires_at"))
        if stored and expires and expires > now() and hmac.compare_digest(stored, code):
            match = device
            break
    if match is None:
        audit.log("device", "link_failed", "failure", identity=actor,
                  detail={"reason": "no pending device with this code"})
        raise ApiError("INVALID_PAIRING_CODE",
                       "Geen wachtend apparaat met deze koppelcode. Controleer de code "
                       "op het scherm van de recorder.")
    device_id = match["device_id"]
    db.execute(
        "UPDATE devices SET enrol_state = ?, upload_enabled = 1, pairing_code = NULL, "
        "pairing_expires_at = NULL, display_name = ?, updated_at = ? WHERE device_id = ?",
        (ACTIVE, (display_name or "").strip()[:80] or match["display_name"] or device_id,
         now_iso(), device_id))
    if user_id:
        users.bind_device(device_id, user_id, actor=actor)
    audit.log("device", "linked", "success", device_id=device_id, identity=actor,
              detail={"user_id": user_id})
    return _device(device_id)


def allow_reenrol(device_id: str, *, minutes: int = 30, actor: str = "admin") -> str:
    _device(device_id)
    minutes = max(1, min(int(minutes), 1440))
    expires = (now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    db.execute(
        "INSERT INTO enrolment_windows(device_id, expires_at, created_at, created_by) "
        "VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET expires_at = excluded.expires_at",
        (device_id, expires, now_iso(), actor))
    audit.log("device", "reenrol_allowed", "success", device_id=device_id, identity=actor,
              detail={"minutes": minutes})
    return expires


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------

def _check_ssid(ssid: str) -> str:
    ssid = (ssid or "").strip()
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ApiError("INVALID_REQUEST", "Een netwerknaam is 1 tot 32 tekens lang.")
    if any(ord(c) < 32 for c in ssid):
        raise ApiError("INVALID_REQUEST", "De netwerknaam bevat ongeldige tekens.")
    return ssid


def _check_password(password: str) -> str:
    password = password or ""
    if password and not (8 <= len(password.encode("utf-8")) <= 63):
        raise ApiError("INVALID_REQUEST",
                       "Een Wi-Fi-wachtwoord is 8 tot 63 tekens (of leeg voor een "
                       "open netwerk).")
    if any(ord(c) < 32 for c in password):
        raise ApiError("INVALID_REQUEST", "Het wachtwoord bevat ongeldige tekens.")
    return password


def queue_wifi(device_id: str, op: str, ssid: str, password: str = "", *,
               actor: str) -> int:
    """Queue a change. A newer change for the same SSID replaces a waiting one."""
    _device(device_id)
    if op not in ("add", "remove"):
        raise ApiError("INVALID_REQUEST", "op must be add or remove")
    ssid = _check_ssid(ssid)
    secret = secretbox.seal(_check_password(password)) if op == "add" and password else ""
    ts = now_iso()
    with db.tx() as conn:
        conn.execute(
            "UPDATE device_wifi_ops SET cancelled_at = ?, secret = '' WHERE device_id = ? "
            "AND ssid = ? AND applied_at IS NULL AND cancelled_at IS NULL",
            (ts, device_id, ssid))
        cur = conn.execute(
            "INSERT INTO device_wifi_ops(device_id, op, ssid, secret, created_at, "
            "created_by) VALUES(?,?,?,?,?,?)", (device_id, op, ssid, secret, ts, actor))
        op_id = int(cur.lastrowid)
    audit.log("device", f"wifi_{op}_queued", "success", device_id=device_id,
              identity=actor, detail={"ssid": ssid, "op_id": op_id})
    return op_id


def cancel_wifi(device_id: str, op_id: int, *, actor: str) -> None:
    cur = db.execute(
        "UPDATE device_wifi_ops SET cancelled_at = ?, secret = '' WHERE id = ? AND "
        "device_id = ? AND applied_at IS NULL AND cancelled_at IS NULL",
        (now_iso(), int(op_id), device_id))
    if cur.rowcount:
        audit.log("device", "wifi_op_cancelled", "success", device_id=device_id,
                  identity=actor, detail={"op_id": op_id})


def pending_wifi_ops(device_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT id, op, ssid, created_at, created_by FROM device_wifi_ops "
        "WHERE device_id = ? AND applied_at IS NULL AND cancelled_at IS NULL ORDER BY id",
        (device_id,))]


def wifi_ops_for_device(device_id: str) -> list[dict[str, Any]]:
    """The waiting changes, passwords opened, for the recorder itself only."""
    ops = []
    for r in db.query(
            "SELECT id, op, ssid, secret FROM device_wifi_ops WHERE device_id = ? AND "
            "applied_at IS NULL AND cancelled_at IS NULL ORDER BY id LIMIT 16",
            (device_id,)):
        item: dict[str, Any] = {"id": int(r["id"]), "op": r["op"], "ssid": r["ssid"]}
        if r["op"] == "add":
            try:
                item["password"] = secretbox.open_(r["secret"])
            except ValueError:
                continue    # undecryptable: never send a wrong password
        ops.append(item)
    return ops


def wifi_view(device: dict[str, Any]) -> dict[str, Any]:
    try:
        reported = json.loads(device.get("wifi_networks_json") or "null")
    except ValueError:
        reported = None
    return {
        "reported": reported if isinstance(reported, list) else None,
        "pending": pending_wifi_ops(device["device_id"]),
        "max_networks": MAX_DEVICE_NETWORKS,
    }


def _record_wifi_report(device_id: str, networks: Any, applied: Any) -> None:
    if isinstance(networks, list):
        clean = [str(n)[:32] for n in networks if isinstance(n, str) and n][
            :MAX_REPORTED_NETWORKS]
        db.execute("UPDATE devices SET wifi_networks_json = ? WHERE device_id = ?",
                   (json.dumps(clean, ensure_ascii=False), device_id))
    if isinstance(applied, int) and not isinstance(applied, bool) and applied > 0:
        ts = now_iso()
        cur = db.execute(
            "UPDATE device_wifi_ops SET applied_at = ?, secret = '' WHERE device_id = ? "
            "AND id <= ? AND applied_at IS NULL AND cancelled_at IS NULL",
            (ts, device_id, applied))
        db.execute("UPDATE devices SET wifi_ops_applied = MAX(wifi_ops_applied, ?) "
                   "WHERE device_id = ?", (applied, device_id))
        if cur.rowcount:
            audit.log("device", "wifi_ops_applied", "success", device_id=device_id,
                      detail={"up_to": applied, "count": cur.rowcount})


# ---------------------------------------------------------------------------
# firmware
# ---------------------------------------------------------------------------

ESP_IMAGE_MAGIC = 0xE9
ESP_APP_DESC_MAGIC = 0xABCD5432
ESP32S3_CHIP_ID = 9
_MARKER = re.compile(rb"VSFW\|version=([0-9A-Za-z._+-]{1,40})\|board=([0-9A-Za-z._-]{1,40})\|")
_VERSION = re.compile(r"^[0-9A-Za-z._+-]{1,40}$")


def inspect_image(data: bytes) -> dict[str, Any]:
    """Refuse anything that is not an ESP32-S3 application image.

    A wrong file set out to a fleet is the expensive mistake here (a
    bootloader, a partition table, an image for another chip, a text file
    renamed .bin), so the header is checked before anything is stored. The
    version comes from the marker string the VisiteScribe firmware embeds; the
    ESP-IDF app descriptor is only a fallback.
    """
    if len(data) < 256:
        raise ApiError("INVALID_FIRMWARE", "Bestand is te klein voor een firmware-image.")
    if data[0] != ESP_IMAGE_MAGIC:
        raise ApiError("INVALID_FIRMWARE",
                       "Geen ESP32-applicatie-image (verkeerde magic). Upload het "
                       "firmware.bin uit .pio/build/<env>/, niet de bootloader of "
                       "partities.")
    chip_id = int.from_bytes(data[12:14], "little")
    if chip_id != ESP32S3_CHIP_ID:
        raise ApiError("INVALID_FIRMWARE",
                       f"Dit image is voor een andere chip (chip-id {chip_id}); "
                       "Brian is een ESP32-S3.")
    info: dict[str, Any] = {"chip": "esp32s3", "version": "", "board": "",
                            "project": "", "idf": ""}
    # Every ESP-IDF application carries its app descriptor right after the
    # first segment header; a bootloader image (same magic, same chip id)
    # does not. This is what tells them apart.
    if int.from_bytes(data[32:36], "little") != ESP_APP_DESC_MAGIC:
        raise ApiError("INVALID_FIRMWARE",
                       "Dit is geen applicatie-image (geen app-descriptor) -- "
                       "waarschijnlijk de bootloader. Upload firmware.bin.")

    def cstr(a: int, b: int) -> str:
        return data[a:b].split(b"\0", 1)[0].decode("ascii", "replace")
    info["app_desc_version"] = cstr(48, 80)
    info["project"] = cstr(80, 112)
    info["idf"] = cstr(144, 176)
    marker = _MARKER.search(data)
    if marker:
        info["version"] = marker.group(1).decode()
        info["board"] = marker.group(2).decode()
    return info


def add_release(data: bytes, *, filename: str = "", version: str = "", notes: str = "",
                actor: str = "admin") -> dict[str, Any]:
    if len(data) > settings.max_firmware_bytes:
        raise ApiError("PAYLOAD_TOO_LARGE",
                       f"Firmware groter dan {settings.max_firmware_bytes} bytes.")
    info = inspect_image(data)
    version = (version or "").strip() or info["version"]
    if not version:
        raise ApiError("INVALID_FIRMWARE",
                       "Geen versie gevonden in het image; vul de versie zelf in. "
                       "(VisiteScribe-firmware draagt een VSFW-marker met de versie.)")
    if not _VERSION.match(version):
        raise ApiError("INVALID_REQUEST", "Versie mag alleen letters, cijfers en . _ + - bevatten.")
    if info["version"] and version != info["version"]:
        raise ApiError("INVALID_FIRMWARE",
                       f"Het image zegt versie {info['version']}, niet {version}. De "
                       "recorder meldt straks de versie uit het image; die moet kloppen.")
    digest = sha256_hex(data)
    dup = db.query_one("SELECT release_id FROM firmware_releases WHERE sha256 = ?", (digest,))
    if dup is not None:
        raise ApiError("INVALID_REQUEST", "Dit image is al geüpload.",
                       extra={"release_id": dup["release_id"]})
    release_id = "fw-" + uuid.uuid4().hex[:12]
    path = settings.firmware_dir / f"{digest}.bin"
    storage.write_durable(path, data)
    db.execute(
        "INSERT INTO firmware_releases(release_id, version, board, sha256, size, filename, "
        "blob_path, notes, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (release_id, version, info["board"], digest, len(data),
         Path(filename or "firmware.bin").name[:120], str(path), (notes or "")[:500],
         now_iso(), actor))
    audit.log("firmware", "uploaded", "success", identity=actor,
              detail={"release_id": release_id, "version": version, "sha256": digest,
                      "size": len(data), "board": info["board"]})
    return get_release(release_id)  # type: ignore[return-value]


def get_release(release_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM firmware_releases WHERE release_id = ?", (release_id,))
    return dict(row) if row else None


def releases() -> list[dict[str, Any]]:
    out = []
    for r in db.query("SELECT * FROM firmware_releases ORDER BY created_at DESC"):
        item = dict(r)
        counts = {row["state"]: int(row["n"]) for row in db.query(
            "SELECT state, COUNT(*) AS n FROM device_updates WHERE release_id = ? "
            "GROUP BY state", (item["release_id"],))}
        item["assignments"] = counts
        item["running"] = int(db.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE software_version = ?",
            (item["version"],))["n"])
        out.append(item)
    return out


def delete_release(release_id: str, *, actor: str) -> None:
    release = get_release(release_id)
    if release is None:
        raise ApiError("UNKNOWN_RELEASE", "Onbekende firmware")
    busy = db.query_one(
        "SELECT COUNT(*) AS n FROM device_updates WHERE release_id = ? AND state IN "
        f"({','.join('?' * len(UPDATE_OPEN_STATES))})", (release_id, *UPDATE_OPEN_STATES))
    if busy and int(busy["n"]):
        raise ApiError("INVALID_REQUEST",
                       "Deze firmware staat nog klaar voor een apparaat; annuleer dat eerst.")
    db.execute("DELETE FROM device_updates WHERE release_id = ?", (release_id,))
    db.execute("DELETE FROM firmware_releases WHERE release_id = ?", (release_id,))
    storage.delete_blob(release["blob_path"])
    audit.log("firmware", "deleted", "success", identity=actor,
              detail={"release_id": release_id, "version": release["version"]})


def assign(device_id: str, release_id: str, *, actor: str) -> dict[str, Any]:
    device = _device(device_id)
    release = get_release(release_id)
    if release is None:
        raise ApiError("UNKNOWN_RELEASE", "Onbekende firmware")
    ts = now_iso()
    state = "installed" if device.get("software_version") == release["version"] else "pending"
    db.execute(
        "INSERT INTO device_updates(device_id, release_id, state, attempts, detail, "
        "requested_at, requested_by, updated_at) VALUES(?,?,?,0,'',?,?,?) "
        "ON CONFLICT(device_id) DO UPDATE SET release_id = excluded.release_id, "
        "state = excluded.state, attempts = 0, detail = '', "
        "requested_at = excluded.requested_at, requested_by = excluded.requested_by, "
        "updated_at = excluded.updated_at",
        (device_id, release_id, state, ts, actor, ts))
    audit.log("firmware", "assigned", "success", device_id=device_id, identity=actor,
              detail={"release_id": release_id, "version": release["version"],
                      "already_running": state == "installed"})
    return update_of(device_id) or {}


def cancel_update(device_id: str, *, actor: str) -> None:
    cur = db.execute(
        "UPDATE device_updates SET state = 'cancelled', updated_at = ? WHERE device_id = ? "
        f"AND state IN ({','.join('?' * len(UPDATE_OPEN_STATES))})",
        (now_iso(), device_id, *UPDATE_OPEN_STATES))
    if cur.rowcount:
        audit.log("firmware", "cancelled", "success", device_id=device_id, identity=actor)


def update_of(device_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT u.*, r.version, r.sha256, r.size, r.board FROM device_updates u "
        "JOIN firmware_releases r ON r.release_id = u.release_id WHERE u.device_id = ?",
        (device_id,))
    return dict(row) if row else None


def update_block(device: dict[str, Any]) -> dict[str, Any] | None:
    """The update a recorder should install now, or None."""
    upd = update_of(device["device_id"])
    if upd is None or upd["state"] not in UPDATE_OPEN_STATES:
        return None
    if device.get("software_version") == upd["version"]:
        _set_update_state(device["device_id"], "installed", "reported by heartbeat")
        return None
    if upd["state"] == "failed" and int(upd["attempts"]) >= settings.ota_max_attempts:
        return None
    return {
        "release_id": upd["release_id"],
        "version": upd["version"],
        "sha256": upd["sha256"],
        "size": int(upd["size"]),
        "url": f"/v1/device/firmware/{upd['release_id']}",
        "min_battery_charging": max(20, settings.ota_min_battery_charging),
        "min_battery_unplugged": max(20, settings.ota_min_battery_unplugged),
    }


def _set_update_state(device_id: str, state: str, detail: str = "",
                      bump_attempts: bool = False) -> None:
    db.execute(
        "UPDATE device_updates SET state = ?, detail = ?, updated_at = ?"
        + (", attempts = attempts + 1" if bump_attempts else "")
        + " WHERE device_id = ?",
        (state, detail[:300], now_iso(), device_id))
    audit.log("firmware", f"update_{state}", "failure" if state == "failed" else "success",
              device_id=device_id, detail={"detail": detail[:300]})


def firmware_for_download(device_id: str, release_id: str) -> dict[str, Any]:
    upd = update_of(device_id)
    if upd is None or upd["release_id"] != release_id or upd["state"] not in UPDATE_OPEN_STATES:
        raise ApiError("UPDATE_NOT_ASSIGNED", "This firmware is not set out for this device")
    release = get_release(release_id)
    if release is None or not Path(release["blob_path"]).is_file():
        raise ApiError("UNKNOWN_RELEASE", "Firmware image is missing on the server")
    _set_update_state(device_id, "downloading", "", bump_attempts=True)
    return release


def report_update(device_id: str, release_id: str, state: str, detail: str = "") -> None:
    upd = update_of(device_id)
    if upd is None or upd["release_id"] != release_id:
        raise ApiError("UPDATE_NOT_ASSIGNED", "This firmware is not set out for this device")
    if state not in REPORTABLE_STATES:
        raise ApiError("INVALID_REQUEST",
                       f"state must be one of {', '.join(REPORTABLE_STATES)}")
    if upd["state"] in ("installed", "cancelled"):
        return
    _set_update_state(device_id, state, detail or "")


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------

def record_heartbeat(device_id: str, beat: Any) -> None:
    """The fleet-related part of a heartbeat; the basics are stored by v1."""
    _record_wifi_report(device_id, getattr(beat, "wifi_networks", None),
                        getattr(beat, "wifi_ops_applied", None))
    charging = getattr(beat, "charging", None)
    if isinstance(charging, bool):
        db.execute("UPDATE devices SET charging = ? WHERE device_id = ?",
                   (1 if charging else 0, device_id))
    hardware = getattr(beat, "hardware", None)
    if isinstance(hardware, dict):
        db.execute("UPDATE devices SET hardware_json = ? WHERE device_id = ?",
                   (_hardware(hardware), device_id))
    version = getattr(beat, "software_version", None)
    if version:
        upd = update_of(device_id)
        if upd and upd["state"] in UPDATE_OPEN_STATES and upd["version"] == version:
            _set_update_state(device_id, "installed", f"running {version}")
