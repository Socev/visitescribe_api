"""SQLite storage layer.

Durability matters more than throughput here: `ingest_confirmed=true` is a
contractual promise to the recorder that it may delete its local copy, so the
database runs in WAL mode with `synchronous=FULL` and every ingest write is
committed (and the matching blob fsynced) before the response is produced.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False
_db_path: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id            TEXT PRIMARY KEY,
    display_name         TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    allow_header_only    INTEGER NOT NULL DEFAULT 1,
    token_hash           TEXT,
    token_hint           TEXT,
    cert_fingerprint     TEXT,
    cert_subject         TEXT,
    revoked_at           TEXT,
    config_json          TEXT NOT NULL DEFAULT '{}',
    config_version       INTEGER NOT NULL DEFAULT 1,
    upload_enabled       INTEGER NOT NULL DEFAULT 1,
    notes                TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    last_seen_at         TEXT,
    last_source_ip       TEXT,
    last_auth_method     TEXT,
    software_version     TEXT,
    battery_percent      INTEGER,
    queue_count          INTEGER,
    storage_free_bytes   INTEGER,
    last_recording_at    TEXT,
    network_state        TEXT
);

CREATE TABLE IF NOT EXISTS enrolment_windows (
    device_id   TEXT PRIMARY KEY,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL DEFAULT 'admin'
);

CREATE TABLE IF NOT EXISTS server_keys (
    key_id       TEXT PRIMARY KEY,
    algorithm    TEXT NOT NULL,
    public_pem   TEXT NOT NULL,
    private_file TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    retired_at   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id            TEXT PRIMARY KEY,
    device_id             TEXT NOT NULL,
    schema_version        INTEGER NOT NULL,
    mode                  TEXT NOT NULL,
    client_status         TEXT,
    started_at            TEXT,
    completed_at          TEXT,
    audio_json            TEXT NOT NULL DEFAULT '{}',
    encryption_json       TEXT NOT NULL DEFAULT '{}',
    wrap_algorithm        TEXT,
    wrap_ciphertext_b64   TEXT,
    wrap_key_id           TEXT,
    manifest_json         TEXT NOT NULL,
    manifest_fingerprint  TEXT NOT NULL DEFAULT '',
    expected_chunks       INTEGER NOT NULL DEFAULT 0,
    state                 TEXT NOT NULL,
    ingest_confirmed      INTEGER NOT NULL DEFAULT 0,
    complete_requested    INTEGER NOT NULL DEFAULT 0,
    complete_chunk_count  INTEGER,
    processing_json       TEXT NOT NULL DEFAULT '{}',
    error_code            TEXT,
    error_message         TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    ingested_at           TEXT,
    purged_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);

CREATE TABLE IF NOT EXISTS manifest_chunks (
    session_id       TEXT NOT NULL,
    sequence         INTEGER NOT NULL,
    file             TEXT,
    nonce_b64        TEXT,
    aad              TEXT,
    plaintext_sha256 TEXT,
    ciphertext_sha256 TEXT,
    plaintext_size   INTEGER,
    ciphertext_size  INTEGER,
    PRIMARY KEY (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS chunks (
    session_id          TEXT NOT NULL,
    sequence            INTEGER NOT NULL,
    ciphertext_sha256   TEXT NOT NULL,
    plaintext_sha256    TEXT NOT NULL,
    nonce_b64           TEXT NOT NULL,
    aad                 TEXT NOT NULL,
    ciphertext_size     INTEGER NOT NULL,
    plaintext_size      INTEGER NOT NULL,
    blob_path           TEXT NOT NULL,
    plaintext_blob_path TEXT,
    ciphertext_verified INTEGER NOT NULL DEFAULT 0,
    decrypt_verified    INTEGER NOT NULL DEFAULT 0,
    plaintext_verified  INTEGER NOT NULL DEFAULT 0,
    flac_valid          INTEGER NOT NULL DEFAULT 0,
    flac_deep_verified  INTEGER NOT NULL DEFAULT 0,
    flac_json           TEXT NOT NULL DEFAULT '{}',
    received_at         TEXT NOT NULL,
    PRIMARY KEY (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    event        TEXT NOT NULL,
    offset_ms    INTEGER,
    patient_index INTEGER,
    at           TEXT,
    payload_json TEXT NOT NULL,
    dedup_hash   TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup ON events(session_id, dedup_hash);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, offset_ms);

CREATE TABLE IF NOT EXISTS idempotency (
    idem_key      TEXT NOT NULL,
    device_id     TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (idem_key, device_id)
);

CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    category        TEXT NOT NULL,
    action          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    device_id       TEXT,
    session_id      TEXT,
    sequence        INTEGER,
    identity        TEXT,
    auth_method     TEXT,
    source_ip       TEXT,
    idempotency_key TEXT,
    request_id      TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_device ON audit(device_id, id DESC);

CREATE TABLE IF NOT EXISTS purges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    scope       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    ts          TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def configure(db_path: Path) -> None:
    """Point the layer at a database file. Safe to call again in tests."""
    global _db_path, _initialised
    with _init_lock:
        _db_path = db_path
        _initialised = False
    # Drop any connection this thread holds to the previous file.
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("db.configure() was never called")
    conn = sqlite3.connect(_db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # FULL, not NORMAL: a confirmed ingest must survive a power cut.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    global _initialised
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    if not _initialised:
        with _init_lock:
            if not _initialised:
                conn.executescript(SCHEMA)
                _initialised = True
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """One immediate (write-locking) transaction, committed or rolled back."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, params).fetchall())


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    return get_conn().execute(sql, params)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def get_meta(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return default if row is None else row["value"]


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
