"""Encryption at rest for the secrets we hold on someone else's behalf.

The database now stores provider API keys and, once users sign in, a bearer
token per user for their own OurMind account. A token is not a password, but
it is enough to read and write that doctor's consultations, so a copy of the
database -- a backup, a snapshot, a disk pulled out of a machine -- must not
hand them over in the clear.

The key lives beside the RSA server keys, in the 0700 key directory, and never
goes in the database. That is deliberately a modest guarantee: anything that
can read the whole appData directory can read both. What it does buy is that
the database alone is worthless, which covers the realistic case -- a copied
db file, an export, a support dump.

Values are stored as "v1:<nonce>:<ciphertext>", base64 per part. Anything not
carrying that prefix is returned unchanged, so a database written before this
existed keeps working and is upgraded on the next write.
"""
from __future__ import annotations

import base64
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

PREFIX = "v1"
_lock = threading.Lock()
_key: bytes | None = None


def _key_path() -> Path:
    return settings.keys_dir / "secretbox.key"


def _load_key() -> bytes:
    global _key
    with _lock:
        if _key is not None:
            return _key
        path = _key_path()
        if path.exists():
            raw = base64.b64decode(path.read_text().strip())
            if len(raw) != 32:
                raise ValueError(f"{path} does not hold a 32-byte key")
        else:
            raw = os.urandom(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write with the restrictive mode from the start: creating it
            # world-readable and fixing it afterwards leaves a window.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(base64.b64encode(raw).decode())
        _key = raw
        return raw


def reset_for_tests() -> None:
    global _key
    with _lock:
        _key = None


def seal(plaintext: str) -> str:
    if plaintext == "":
        return ""
    nonce = os.urandom(12)
    box = AESGCM(_load_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{PREFIX}:{base64.b64encode(nonce).decode()}:{base64.b64encode(box).decode()}"


def open_(stored: str | None) -> str:
    """Decrypt, or pass through a value written before encryption existed."""
    if not stored:
        return ""
    parts = stored.split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        return stored
    try:
        nonce = base64.b64decode(parts[1])
        box = base64.b64decode(parts[2])
        return AESGCM(_load_key()).decrypt(nonce, box, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("stored secret could not be decrypted; wrong key file?") from exc


def is_sealed(stored: str | None) -> bool:
    return bool(stored) and stored.split(":", 1)[0] == PREFIX
