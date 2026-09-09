"""One-time startup work shared by the ingest and admin applications.

Startup is deliberately fault-tolerant. If the data directory is unwritable or
a key cannot be generated, the process still comes up and serves /healthz, and
the reason is reported by /readyz and on the admin page. A container that
exits instead would crash-loop, the pod would never turn Ready, and the Olares
installer would sit on "Installing" with nothing to show for it.
"""
from __future__ import annotations

import logging
import os

from . import crypto, db
from .config import settings
from .util import now_iso

log = logging.getLogger("visitescribe")

# Problems found at startup, readable without touching the database.
STARTUP_PROBLEMS: list[str] = []
STARTUP_INFO: dict[str, str] = {}


def _check_data_dir() -> None:
    path = settings.data_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        STARTUP_PROBLEMS.append(f"cannot create {path}: {exc}")
        return
    probe = path / ".write-test"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        try:
            stat = os.stat(path)
            owner = f"uid {stat.st_uid}:gid {stat.st_gid}, mode {oct(stat.st_mode & 0o777)}"
        except OSError:
            owner = "unknown"
        STARTUP_PROBLEMS.append(
            f"{path} is not writable by uid {os.getuid()}:gid {os.getgid()} "
            f"(directory is {owner}): {exc}"
        )
    STARTUP_INFO["data_dir"] = str(path)
    STARTUP_INFO["running_as"] = f"uid {os.getuid()}:gid {os.getgid()}"


def initialise() -> None:
    STARTUP_PROBLEMS.clear()
    _check_data_dir()
    try:
        settings.ensure_dirs()
    except OSError as exc:
        STARTUP_PROBLEMS.append(f"cannot prepare data directories: {exc}")

    try:
        db.configure(settings.db_path)
        db.get_conn()  # applies the schema
    except Exception as exc:  # noqa: BLE001
        STARTUP_PROBLEMS.append(f"database unavailable: {exc}")
        for problem in STARTUP_PROBLEMS:
            log.error("startup problem: %s", problem)
        return

    try:
        key_id = crypto.ensure_active_key(settings.keys_dir, bits=settings.rsa_key_bits)
        STARTUP_INFO["active_key"] = key_id
        log.info("active server key: %s", key_id)
    except Exception as exc:  # noqa: BLE001
        STARTUP_PROBLEMS.append(f"no server key: {exc}")

    try:
        if db.get_meta("installed_at") is None:
            db.set_meta("installed_at", now_iso())
            log.info("initialised new VisiteScribe store at %s", settings.data_dir)
        db.set_meta("last_start_at", now_iso())
    except Exception as exc:  # noqa: BLE001
        STARTUP_PROBLEMS.append(f"cannot write to the database: {exc}")

    for problem in STARTUP_PROBLEMS:
        log.error("startup problem: %s", problem)
