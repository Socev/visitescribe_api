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
    aad_sha256          TEXT NOT NULL DEFAULT '',
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
-- A repeated GCM nonce under one session key is a total break of AES-GCM.
-- The database refuses to store one even if a check above is ever bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_nonce ON chunks(session_id, nonce_b64);

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

-- Processing layer -------------------------------------------------------
CREATE TABLE IF NOT EXISTS processing_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    segment_index  INTEGER,            -- NULL = the whole session
    route          TEXT NOT NULL,      -- mistral | ourmind
    stage          TEXT NOT NULL,      -- transcribe | note
    state          TEXT NOT NULL,      -- queued | running | done | failed | cancelled
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error_code     TEXT,
    error          TEXT,
    detail_json    TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_unique
    ON processing_jobs(session_id, IFNULL(segment_index, -1), stage);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON processing_jobs(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS transcripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    segment_index INTEGER,
    provider      TEXT NOT NULL,
    model         TEXT,
    language      TEXT,
    text          TEXT NOT NULL,
    segments_json TEXT NOT NULL DEFAULT '[]',
    audio_seconds REAL,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_unique
    ON transcripts(session_id, IFNULL(segment_index, -1));

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    segment_index INTEGER,
    provider      TEXT NOT NULL,
    model         TEXT,
    template      TEXT,
    title         TEXT,
    body          TEXT NOT NULL,
    codes_json    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft | approved
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_unique
    ON notes(session_id, IFNULL(segment_index, -1));

-- Every billable call is recorded here, whether or not a price is known.
CREATE TABLE IF NOT EXISTS usage_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT,
    segment_index     INTEGER,
    provider          TEXT NOT NULL,
    model             TEXT,
    operation         TEXT NOT NULL,   -- transcribe | note
    audio_seconds     REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cost_usd          REAL,
    priced            INTEGER NOT NULL DEFAULT 0,
    price_note        TEXT,
    raw_usage_json    TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_records(created_at);

CREATE TABLE IF NOT EXISTS provider_credentials (
    provider   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- api_key | bearer
    secret     TEXT NOT NULL,
    meta_json  TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

-- A person who owns recordings. The key is their OurMind e-mail address,
-- because that is literally the doctor's id in OurMind's API (`data.id` on
-- /me is the address), so there is nothing to map between the two systems.
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    org_name      TEXT NOT NULL DEFAULT '',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- The user's own OurMind credentials, so their audio goes to their own
-- account and counts against their own report quota. Encrypted at rest.
CREATE TABLE IF NOT EXISTS user_tokens (
    user_id        TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT NOT NULL DEFAULT '',
    expires_at     TEXT,
    updated_at     TEXT NOT NULL
);

-- Sign-in sessions for the user-facing site. Separate from admin_sessions:
-- a user must never be able to reach the admin interface with their cookie.
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Pending e-mail codes. We never see the code itself -- Supabase mails it --
-- this only remembers that a login for this address is in progress, so the
-- verify step cannot be used to fish for which addresses exist.
CREATE TABLE IF NOT EXISTS login_attempts (
    email      TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

-- What kinds of recording exist. A TABLE and not an enum on purpose: the
-- recorder decides what it sends, and a future button on the hardware (an
-- "MDO", say) must show up here and in every settings page without a code
-- change. Unknown modes arriving from a recorder are added automatically.
CREATE TABLE IF NOT EXISTS recording_types (
    mode        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    patient_audio INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 100,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- What happens to a recording of a given kind, per user. Absent = ask.
CREATE TABLE IF NOT EXISTS routing_rules (
    user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    mode          TEXT NOT NULL,
    route         TEXT NOT NULL DEFAULT '',
    template_id   TEXT NOT NULL DEFAULT '',
    template_type TEXT NOT NULL DEFAULT '',
    template_title TEXT NOT NULL DEFAULT '',
    auto          INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, mode)
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
                _add_missing_columns(conn)
                _seed_recording_types(conn)
                _initialised = True
    return conn


# Columns added to a table that already exists in someone's database.
# CREATE TABLE IF NOT EXISTS cannot do this, and there is a live installation
# with real recordings in it, so the schema has to grow without a rebuild.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("devices", "user_id", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


# The modes the recorder ships with today. Seeded rather than hardcoded, so a
# new one can arrive from a recorder -- or be added by hand -- without a
# release. `patient_audio` marks the kinds that carry patient voices, which is
# what the provider policy is allowed to reason about.
BUILTIN_RECORDING_TYPES: tuple[tuple[str, str, str, int, int], ...] = (
    ("single_patient", "Consult", "Eén patiënt, één opname.", 1, 10),
    ("multi_patient", "Spreekuur", "Meerdere patiënten achter elkaar; "
     "wordt per patiënt gesplitst en nooit samengevoegd.", 1, 20),
    ("meeting", "Vergadering", "Geen patiëntencontact.", 0, 30),
)


def _seed_recording_types(conn: sqlite3.Connection) -> None:
    from .util import now_iso

    for mode, title, description, patient_audio, position in BUILTIN_RECORDING_TYPES:
        conn.execute(
            "INSERT INTO recording_types(mode, title, description, patient_audio, "
            "position, builtin, created_at) VALUES(?,?,?,?,?,1,?) "
            "ON CONFLICT(mode) DO NOTHING",
            (mode, title, description, patient_audio, position, now_iso()),
        )
    conn.commit()


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
