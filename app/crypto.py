"""Server key management, RSA-OAEP key unwrapping and AES-256-GCM decryption.

Design notes
------------
* Session keys are never persisted in plaintext. Only the client's wrapped
  blob is stored; the 32-byte key is unwrapped on demand and held in a
  bounded, TTL'd in-memory cache so a burst of chunk uploads does not do one
  RSA operation per chunk.
* Multiple server keys can exist at once (spec section 9). One is active for
  new sessions; retired keys stay usable so older sessions keep decrypting
  for as long as their retention requires.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .util import now_iso, sha256_hex

WRAP_ALGORITHM = "RSA-OAEP-SHA256"
AES_ALGORITHM = "AES-256-GCM"
SESSION_KEY_BYTES = 32

_lock = threading.RLock()
_private_cache: dict[str, rsa.RSAPrivateKey] = {}
_session_key_cache: dict[str, tuple[bytes, float]] = {}


class KeyWrapError(Exception):
    """The wrapped session key could not be unwrapped into a valid AES key."""


class DecryptError(Exception):
    """AES-256-GCM authentication or decryption failed."""


# --------------------------------------------------------------------------
# server key store
# --------------------------------------------------------------------------

def _key_path(keys_dir: Path, key_id: str) -> Path:
    return keys_dir / f"{key_id}.pem"


def generate_key(keys_dir: Path, bits: int = 4096, make_active: bool = True) -> str:
    """Create a new RSA key pair and register it. Returns the key_id."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    key_id = "srv-" + sha256_hex(public_pem.encode("ascii"))[:16]

    path = _key_path(keys_dir, key_id)
    if not path.exists():
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        tmp = path.with_suffix(".pem.tmp")
        tmp.write_bytes(pem)
        tmp.chmod(0o600)
        tmp.replace(path)

    ts = now_iso()
    with db.tx() as conn:
        if make_active:
            conn.execute(
                "UPDATE server_keys SET active = 0, retired_at = COALESCE(retired_at, ?) "
                "WHERE active = 1",
                (ts,),
            )
        conn.execute(
            "INSERT INTO server_keys(key_id, algorithm, public_pem, private_file, active, created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(key_id) DO UPDATE SET active = excluded.active, "
            "retired_at = NULL",
            (key_id, WRAP_ALGORITHM, public_pem, str(path), 1 if make_active else 0, ts),
        )
    with _lock:
        _private_cache[key_id] = private
    return key_id


def ensure_active_key(keys_dir: Path, bits: int = 4096) -> str:
    row = db.query_one("SELECT key_id FROM server_keys WHERE active = 1 LIMIT 1")
    if row is not None:
        return row["key_id"]
    return generate_key(keys_dir, bits=bits, make_active=True)


def active_key() -> dict | None:
    return db.row_to_dict(
        db.query_one("SELECT * FROM server_keys WHERE active = 1 LIMIT 1")
    )


def all_keys() -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM server_keys ORDER BY active DESC, created_at DESC"
    )]


def _load_private(key_id: str) -> rsa.RSAPrivateKey | None:
    with _lock:
        cached = _private_cache.get(key_id)
    if cached is not None:
        return cached
    row = db.query_one("SELECT private_file FROM server_keys WHERE key_id = ?", (key_id,))
    if row is None:
        return None
    path = Path(row["private_file"])
    if not path.exists():
        return None
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        return None
    with _lock:
        _private_cache[key_id] = key
    return key


def forget_private_keys() -> None:
    with _lock:
        _private_cache.clear()
        _session_key_cache.clear()


# --------------------------------------------------------------------------
# key wrapping
# --------------------------------------------------------------------------

_OAEP = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()),
    algorithm=hashes.SHA256(),
    label=None,
)


def unwrap_session_key(ciphertext: bytes, key_id: str | None = None) -> tuple[bytes, str]:
    """Unwrap a client-supplied session key.

    Tries the named key first, then every other registered key (the v0.2
    client sends no key_id, and retired keys must keep working). Returns the
    32-byte AES key and the key_id that opened it.
    """
    candidates: list[str] = []
    if key_id:
        candidates.append(key_id)
    for row in db.query("SELECT key_id FROM server_keys ORDER BY active DESC, created_at DESC"):
        if row["key_id"] not in candidates:
            candidates.append(row["key_id"])
    if not candidates:
        raise KeyWrapError("no server key is registered")

    last_reason = "no server key could unwrap the ciphertext"
    for kid in candidates:
        private = _load_private(kid)
        if private is None:
            continue
        try:
            plain = private.decrypt(ciphertext, _OAEP)
        except Exception:  # noqa: BLE001 - any RSA failure is just "wrong key"
            continue
        if len(plain) != SESSION_KEY_BYTES:
            last_reason = (
                f"unwrapped session key is {len(plain)} bytes, expected {SESSION_KEY_BYTES}"
            )
            continue
        return plain, kid
    raise KeyWrapError(last_reason)


def wrap_session_key_for_test(session_key: bytes, public_pem: str) -> bytes:
    """Only used by the test suite and the recorder simulator."""
    public = serialization.load_pem_public_key(public_pem.encode("ascii"))
    return public.encrypt(session_key, _OAEP)  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# session key cache
# --------------------------------------------------------------------------

def cache_session_key(session_id: str, key: bytes, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    with _lock:
        if len(_session_key_cache) > 512:
            cutoff = time.monotonic()
            for k, (_, exp) in list(_session_key_cache.items()):
                if exp <= cutoff:
                    _session_key_cache.pop(k, None)
            if len(_session_key_cache) > 512:
                _session_key_cache.clear()
        _session_key_cache[session_id] = (key, time.monotonic() + ttl_seconds)


def cached_session_key(session_id: str) -> bytes | None:
    with _lock:
        entry = _session_key_cache.get(session_id)
        if entry is None:
            return None
        key, expires = entry
        if expires <= time.monotonic():
            _session_key_cache.pop(session_id, None)
            return None
        return key


def drop_session_key(session_id: str) -> None:
    with _lock:
        _session_key_cache.pop(session_id, None)


# --------------------------------------------------------------------------
# chunk decryption
# --------------------------------------------------------------------------

def decrypt_chunk(key: bytes, nonce: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM open. The AAD is used byte-for-byte as supplied."""
    if len(key) != SESSION_KEY_BYTES:
        raise DecryptError("session key must be 32 bytes")
    if not nonce:
        raise DecryptError("empty nonce")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptError("AES-GCM authentication failed") from exc
    except Exception as exc:  # noqa: BLE001
        raise DecryptError(f"AES-GCM decryption failed: {exc}") from exc


def encrypt_chunk_for_test(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    return AESGCM(key).encrypt(nonce, plaintext, aad)
