"""One-time startup work shared by the ingest and admin applications."""
from __future__ import annotations

import logging

from . import crypto, db
from .config import settings
from .util import now_iso

log = logging.getLogger("visitescribe")


def initialise() -> None:
    settings.ensure_dirs()
    db.configure(settings.db_path)
    db.get_conn()  # applies the schema
    key_id = crypto.ensure_active_key(settings.keys_dir, bits=settings.rsa_key_bits)
    if db.get_meta("installed_at") is None:
        db.set_meta("installed_at", now_iso())
        log.info("initialised new VisiteScribe store at %s", settings.data_dir)
    db.set_meta("last_start_at", now_iso())
    log.info("active server key: %s", key_id)
