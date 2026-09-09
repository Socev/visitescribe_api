"""Runtime configuration, all overridable by environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _s(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_s("VS_DATA_DIR", "/data")))

    # --- network -------------------------------------------------------
    api_port: int = field(default_factory=lambda: _i("VS_API_PORT", 8080))
    admin_port: int = field(default_factory=lambda: _i("VS_ADMIN_PORT", 8081))
    bind_host: str = field(default_factory=lambda: _s("VS_BIND_HOST", "0.0.0.0"))

    # Optional direct-TLS ingest listener with real mTLS (for LAN / exposePort
    # use, where no reverse proxy terminates TLS). Disabled when port == 0.
    mtls_port: int = field(default_factory=lambda: _i("VS_MTLS_PORT", 0))
    mtls_cert_file: str = field(default_factory=lambda: _s("VS_MTLS_CERT_FILE", ""))
    mtls_key_file: str = field(default_factory=lambda: _s("VS_MTLS_KEY_FILE", ""))
    mtls_ca_file: str = field(default_factory=lambda: _s("VS_MTLS_CA_FILE", ""))

    # Header carrying a client certificate forwarded by a trusting proxy.
    # Empty disables header-based mTLS entirely (the safe default: an
    # attacker must never be able to forge identity with a plain header).
    mtls_header: str = field(default_factory=lambda: _s("VS_MTLS_HEADER", "").lower())
    mtls_header_format: str = field(
        default_factory=lambda: _s("VS_MTLS_HEADER_FORMAT", "pem_urlencoded")
    )

    # --- protocol ------------------------------------------------------
    schema_version: int = field(default_factory=lambda: _i("VS_SCHEMA_VERSION", 2))
    max_chunk_bytes: int = field(default_factory=lambda: _i("VS_MAX_CHUNK_BYTES", 64 * 1024 * 1024))
    max_json_bytes: int = field(default_factory=lambda: _i("VS_MAX_JSON_BYTES", 8 * 1024 * 1024))
    # Upper bound on how large one chunk may decode to. A FLAC stream of
    # silence compresses ~4000x, so without this a few hundred KiB of
    # ciphertext could allocate gigabytes.
    max_decoded_bytes: int = field(default_factory=lambda: _i("VS_MAX_DECODED_BYTES", 64 * 1024 * 1024))
    max_chunks_per_session: int = field(default_factory=lambda: _i("VS_MAX_CHUNKS_PER_SESSION", 20000))
    max_events_per_request: int = field(default_factory=lambda: _i("VS_MAX_EVENTS_PER_REQUEST", 2000))

    # --- security ------------------------------------------------------
    # When true a newly created device defaults to requiring a token or a
    # pinned client certificate; when false it may authenticate with its
    # X-Device-ID alone (exactly what the stock v0.2 recorder sends).
    require_device_auth: bool = field(default_factory=lambda: _b("VS_REQUIRE_DEVICE_AUTH", False))
    # Unknown device IDs are never auto-registered unless an admin opened an
    # enrolment window; this is a hard default.
    rsa_key_bits: int = field(default_factory=lambda: _i("VS_RSA_KEY_BITS", 4096))
    session_key_cache_seconds: int = field(default_factory=lambda: _i("VS_SESSION_KEY_CACHE_SECONDS", 900))

    # Persist the decrypted FLAC alongside the ciphertext. Off by default:
    # the plaintext is always reproducible from the ciphertext plus the
    # wrapped session key, so storing it only widens the PHI footprint.
    store_plaintext: bool = field(default_factory=lambda: _b("VS_STORE_PLAINTEXT", False))

    # Deep FLAC verification decodes every chunk with libsndfile. Falls back
    # to structural validation automatically when libsndfile is unavailable.
    flac_deep_verify: bool = field(default_factory=lambda: _b("VS_FLAC_DEEP_VERIFY", True))

    admin_password: str = field(default_factory=lambda: _s("VS_ADMIN_PASSWORD", ""))
    admin_session_hours: int = field(default_factory=lambda: _i("VS_ADMIN_SESSION_HOURS", 12))

    # --- rate limiting (per device, token bucket) -----------------------
    rate_limit_per_minute: int = field(default_factory=lambda: _i("VS_RATE_LIMIT_PER_MINUTE", 600))
    rate_limit_burst: int = field(default_factory=lambda: _i("VS_RATE_LIMIT_BURST", 240))

    # --- device config served at GET /v1/device/config -------------------
    default_chunk_seconds: int = field(default_factory=lambda: _i("VS_DEFAULT_CHUNK_SECONDS", 30))
    default_min_battery: int = field(default_factory=lambda: _i("VS_DEFAULT_MIN_BATTERY", 15))

    # --- retention (days; 0 = keep forever) ------------------------------
    retention_source_audio_days: int = field(default_factory=lambda: _i("VS_RETENTION_SOURCE_AUDIO_DAYS", 0))
    retention_audit_days: int = field(default_factory=lambda: _i("VS_RETENTION_AUDIT_DAYS", 0))

    # --- processing ------------------------------------------------------
    processing_enabled: bool = field(default_factory=lambda: _b("VS_PROCESSING_ENABLED", True))
    processing_poll_seconds: float = field(
        default_factory=lambda: float(_i("VS_PROCESSING_POLL_SECONDS", 5)))
    default_route: str = field(default_factory=lambda: _s("VS_DEFAULT_ROUTE", ""))
    auto_process: bool = field(default_factory=lambda: _b("VS_AUTO_PROCESS", False))

    public_base_url: str = field(default_factory=lambda: _s("VS_PUBLIC_BASE_URL", ""))
    log_level: str = field(default_factory=lambda: _s("VS_LOG_LEVEL", "info"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "visitescribe.db"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.blob_dir, self.keys_dir):
            p.mkdir(parents=True, exist_ok=True)
        # Key material must never be world-readable.
        try:
            self.keys_dir.chmod(0o700)
        except OSError:
            pass


SUPPORTED_MODES = ("single_patient", "multi_patient", "meeting")

# States the recorder treats as proof of durable ingest.
DURABLE_STATES = (
    "INGESTED",
    "READY_FOR_PROCESSING",
    "TRANSCRIBING",
    "PROCESSING",
    "REVIEW_REQUIRED",
    "APPROVED",
)

ALL_STATES = (
    "CREATED",
    "RECEIVING",
    "VALIDATING",
    *DURABLE_STATES,
    "BOUNDARY_REVIEW_REQUIRED",
    "TRANSCRIPTION_FAILED",
    "PROCESSING_FAILED",
    "POLICY_BLOCKED",
    "ERROR",
    "PURGED",
)

KNOWN_EVENTS = (
    "session_started",
    "session_completed",
    "recording_started",
    "recording_stopped",
    "privacy_pause_started",
    "privacy_pause_ended",
    "patient_boundary",
    "marker",
    "recording_interrupted",
    "capture_error",
    "microphone_disconnect",
    "microphone_reconnect",
)

settings = Settings()
