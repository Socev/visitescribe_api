"""Test harness: a real server, a real recorder, real FLAC and real crypto."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A fully isolated API instance rooted at a temp directory."""
    monkeypatch.setenv("VS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VS_RSA_KEY_BITS", "2048")  # keeps the suite fast
    monkeypatch.setenv("VS_ADMIN_PASSWORD", "test-admin-pw")
    monkeypatch.setenv("VS_RATE_LIMIT_PER_MINUTE", "100000")

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app import config as config_module

    config_module.settings = config_module.Settings()
    import app.audit, app.auth, app.bootstrap, app.crypto, app.db  # noqa: F401
    import app.flacinfo, app.idempotency, app.ratelimit, app.sessions  # noqa: F401
    import app.storage, app.v1  # noqa: F401

    # Every module that captured `settings` at import time gets the fresh one.
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(module, "settings"):
            module.settings = config_module.settings

    from fastapi.testclient import TestClient

    from app.admin_app import create_admin_app
    from app.api_app import create_app

    api = create_app()
    admin = create_admin_app()
    with TestClient(api) as client, TestClient(admin) as admin_client:
        import app.crypto as crypto
        import app.db as db

        key = crypto.active_key()
        yield Harness(client, admin_client, key["public_pem"], db, crypto,
                      config_module.settings)
    import app.ratelimit as ratelimit

    ratelimit.reset()


class Harness:
    def __init__(self, client, admin_client, public_pem, db, crypto, settings):
        self.client = client
        self.admin = admin_client
        self.public_pem = public_pem
        self.db = db
        self.crypto = crypto
        self.settings = settings

    def register_device(self, device_id="visitescribe-001", **kwargs):
        import app.auth as auth

        return auth.create_device(device_id, **kwargs)

    def admin_login(self, password="test-admin-pw"):
        resp = self.admin.post("/admin/api/login", json={"password": password})
        return resp


# ---------------------------------------------------------------------------
# audio + crypto helpers that behave exactly like the recorder
# ---------------------------------------------------------------------------

def make_flac(seconds: float = 1.0, sample_rate: int = 48000, channels: int = 1,
              seed: int = 0, subtype: str = "PCM_16") -> bytes:
    rng = np.random.default_rng(seed)
    frames = int(seconds * sample_rate)
    t = np.linspace(0, seconds, frames, endpoint=False)
    tone = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.02 * rng.standard_normal(frames)
    data = tone if channels == 1 else np.column_stack([tone] * channels)
    buf = io.BytesIO()
    sf.write(buf, data, sample_rate, format="FLAC", subtype=subtype)
    return buf.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Recorder:
    """Mirrors the VisiteScribe v0.2 client: FLAC -> AES-256-GCM -> upload."""

    def __init__(self, harness: Harness, device_id: str = "visitescribe-001",
                 mode: str = "single_patient", session_id: str | None = None,
                 chunk_seconds: int = 1, sample_rate: int = 48000, channels: int = 1):
        self.h = harness
        self.device_id = device_id
        self.mode = mode
        self.session_id = session_id or str(uuid.uuid4())
        self.session_key = secrets.token_bytes(32)
        self.chunk_seconds = chunk_seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunks: list[dict] = []

    # -- building ------------------------------------------------------
    def add_chunk(self, seconds: float | None = None, seed: int | None = None,
                  flac: bytes | None = None) -> dict:
        from app.crypto import encrypt_chunk_for_test

        seq = len(self.chunks) + 1
        plaintext = flac if flac is not None else make_flac(
            seconds if seconds is not None else self.chunk_seconds,
            self.sample_rate, self.channels, seed if seed is not None else seq,
        )
        nonce = secrets.token_bytes(12)
        aad = f"{self.session_id}|{seq}|{self.device_id}"
        ciphertext = encrypt_chunk_for_test(self.session_key, nonce, aad.encode("utf-8"),
                                            plaintext)
        chunk = {
            "sequence": seq,
            "file": f"audio/chunk-{seq:06d}.flac.enc",
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
            "aad": aad,
            "plaintext_sha256": sha256_hex(plaintext),
            "ciphertext_sha256": sha256_hex(ciphertext),
            "plaintext_size": len(plaintext),
            "ciphertext_size": len(ciphertext),
            "_plaintext": plaintext,
            "_ciphertext": ciphertext,
        }
        self.chunks.append(chunk)
        return chunk

    def wrapped_key_b64(self, public_pem: str | None = None) -> str:
        from app.crypto import wrap_session_key_for_test

        wrapped = wrap_session_key_for_test(self.session_key,
                                            public_pem or self.h.public_pem)
        return base64.b64encode(wrapped).decode("ascii")

    def manifest(self, status: str = "complete", schema_version: int = 2,
                 wrapped_b64: str | None = None) -> dict:
        return {
            "schema_version": schema_version,
            "session_id": self.session_id,
            "device_id": self.device_id,
            "mode": self.mode,
            "status": status,
            "started_at": "2026-09-09T06:00:00+02:00",
            "completed_at": "2026-09-09T06:12:00+02:00",
            "audio": {
                "codec": "flac",
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "sample_format": "S16_LE",
                "chunk_seconds": self.chunk_seconds,
            },
            "encryption": {
                "algorithm": "AES-256-GCM",
                "local_key_wrap": None,
                "server_key_wrap": {
                    "algorithm": "RSA-OAEP-SHA256",
                    "ciphertext_b64": wrapped_b64 or self.wrapped_key_b64(),
                },
            },
            "chunks": [
                {k: v for k, v in c.items() if not k.startswith("_")}
                for c in self.chunks
            ],
        }

    # -- the exact v0.2 call sequence -----------------------------------
    def headers(self, extra: dict | None = None) -> dict:
        h = {"X-Device-ID": self.device_id}
        h.update(extra or {})
        return h

    def create(self, manifest: dict | None = None, idem: str | None = None):
        body = manifest if manifest is not None else self.manifest()
        return self.h.client.post(
            "/v1/sessions",
            json=body,
            headers=self.headers({
                "Idempotency-Key": idem or f"{self.session_id}:create",
                "Content-Type": "application/json",
            }),
        )

    def put_chunk(self, sequence: int, *, body: bytes | None = None,
                  ct_sha: str | None = None, nonce_b64: str | None = None,
                  aad: str | None = None, pt_sha: str | None = None,
                  idem: str | None = None):
        chunk = next(c for c in self.chunks if c["sequence"] == sequence)
        payload = body if body is not None else chunk["_ciphertext"]
        headers = self.headers({
            "Content-Type": "application/octet-stream",
            "X-Chunk-SHA256": ct_sha if ct_sha is not None else chunk["ciphertext_sha256"],
            "X-Chunk-Nonce": nonce_b64 if nonce_b64 is not None else chunk["nonce_b64"],
            "X-Chunk-AAD": aad if aad is not None else chunk["aad"],
            "X-Plaintext-SHA256": pt_sha if pt_sha is not None else chunk["plaintext_sha256"],
            "Idempotency-Key": idem or f"{self.session_id}:chunk:{sequence}",
        })
        return self.h.client.put(
            f"/v1/sessions/{self.session_id}/chunks/{sequence}",
            content=payload,
            headers=headers,
        )

    def upload_all(self, skip: set[int] | None = None):
        skip = skip or set()
        return [self.put_chunk(c["sequence"]) for c in self.chunks
                if c["sequence"] not in skip]

    def send_events(self, events: list[dict], idem: str | None = None):
        return self.h.client.post(
            f"/v1/sessions/{self.session_id}/events",
            json={"events": events},
            headers=self.headers({
                "Idempotency-Key": idem or f"{self.session_id}:events",
                "Content-Type": "application/json",
            }),
        )

    def complete(self, chunk_count: int | None = None, status: str = "complete",
                 idem: str | None = None):
        body = {"status": status, "completed_at": "2026-09-09T06:20:00+02:00"}
        body["chunk_count"] = len(self.chunks) if chunk_count is None else chunk_count
        return self.h.client.post(
            f"/v1/sessions/{self.session_id}/complete",
            json=body,
            headers=self.headers({
                "Idempotency-Key": idem or f"{self.session_id}:complete",
                "Content-Type": "application/json",
            }),
        )

    def status(self):
        return self.h.client.get(
            f"/v1/sessions/{self.session_id}/status", headers=self.headers()
        )


@pytest.fixture()
def override_settings():
    """Temporarily change a frozen Settings field shared by every module."""
    import contextlib

    @contextlib.contextmanager
    def _apply(**kwargs):
        from app.config import settings as live

        previous = {k: getattr(live, k) for k in kwargs}
        for k, v in kwargs.items():
            object.__setattr__(live, k, v)
        try:
            yield live
        finally:
            for k, v in previous.items():
                object.__setattr__(live, k, v)

    return _apply


@pytest.fixture()
def recorder(server):
    server.register_device("visitescribe-001")
    return Recorder(server)
