"""Request/response schemas.

Unknown fields are accepted everywhere (``extra="allow"``): the spec asks the
server not to reject forward-compatible additions as long as
``schema_version`` matches and the core fields are valid.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_LOOSE = ConfigDict(extra="allow", populate_by_name=True)


class ServerKeyWrap(BaseModel):
    model_config = _LOOSE
    algorithm: str | None = None
    ciphertext_b64: str | None = None
    key_id: str | None = None


class Encryption(BaseModel):
    model_config = _LOOSE
    algorithm: str | None = None
    local_key_wrap: Any | None = None
    server_key_wrap: ServerKeyWrap | None = None


class AudioSpec(BaseModel):
    model_config = _LOOSE
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sample_format: str | None = None
    chunk_seconds: float | None = None


class ManifestChunk(BaseModel):
    model_config = _LOOSE
    sequence: int
    file: str | None = None
    nonce_b64: str | None = None
    aad: str | None = None
    plaintext_sha256: str | None = None
    ciphertext_sha256: str | None = None
    plaintext_size: int | None = None
    ciphertext_size: int | None = None


class SessionManifest(BaseModel):
    model_config = _LOOSE
    schema_version: int
    session_id: str
    device_id: str | None = None
    mode: str = "single_patient"
    status: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    audio: AudioSpec = Field(default_factory=AudioSpec)
    encryption: Encryption = Field(default_factory=Encryption)
    chunks: list[ManifestChunk] = Field(default_factory=list)
    processing: dict[str, Any] | None = None


class SessionEvent(BaseModel):
    model_config = _LOOSE
    event: str
    offset_ms: int | None = None
    patient_index: int | None = None
    at: str | None = None


class EventsRequest(BaseModel):
    model_config = _LOOSE
    events: list[SessionEvent] = Field(default_factory=list)


class CompleteRequest(BaseModel):
    model_config = _LOOSE
    chunk_count: int | None = None
    completed_at: str | None = None
    status: str | None = None


class HeartbeatRequest(BaseModel):
    model_config = _LOOSE
    device_id: str | None = None
    software_version: str | None = None
    battery_percent: int | None = None
    queue_count: int | None = None
    storage_free_bytes: int | None = None
    last_recording_at: str | None = None
    network_state: str | None = None
