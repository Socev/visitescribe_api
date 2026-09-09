"""Client-certificate plumbing for the optional direct-TLS ingest listener.

Uvicorn does not surface the peer certificate in the ASGI scope, so the HTTP
protocol class records it against the connection's peer address the moment the
TLS handshake completes, and the request layer looks it up again by
``scope["client"]``. An entry is dropped as soon as the connection closes, so a
later connection reusing the same ephemeral port cannot inherit an identity.
"""
from __future__ import annotations

import ssl
import threading
from typing import Any

_lock = threading.Lock()
_by_peer: dict[tuple[str, int], bytes] = {}


def remember(peer: tuple[str, int] | None, der: bytes | None) -> None:
    if not peer or not der:
        return
    with _lock:
        _by_peer[peer] = der


def forget(peer: tuple[str, int] | None) -> None:
    if not peer:
        return
    with _lock:
        _by_peer.pop(peer, None)


def lookup(peer: Any) -> bytes | None:
    if not peer or not isinstance(peer, (tuple, list)) or len(peer) < 2:
        return None
    key = (str(peer[0]), int(peer[1]))
    with _lock:
        return _by_peer.get(key)


def make_protocol_class(base: type) -> type:
    """Wrap a uvicorn HTTP protocol class so it records peer certificates."""

    class MtlsProtocol(base):  # type: ignore[misc, valid-type]
        def connection_made(self, transport):  # noqa: ANN001, ANN201
            try:
                ssl_object = transport.get_extra_info("ssl_object")
                peer = transport.get_extra_info("peername")
                if ssl_object is not None and peer:
                    der = ssl_object.getpeercert(binary_form=True)
                    remember((str(peer[0]), int(peer[1])), der)
                    self._vs_peer = (str(peer[0]), int(peer[1]))
            except Exception:  # noqa: BLE001 - never break a connection over this
                self._vs_peer = None
            super().connection_made(transport)

        def connection_lost(self, exc):  # noqa: ANN001, ANN201
            forget(getattr(self, "_vs_peer", None))
            super().connection_lost(exc)

    MtlsProtocol.__name__ = f"Mtls{base.__name__}"
    return MtlsProtocol


def build_ssl_context(cert_file: str, key_file: str, ca_file: str) -> ssl.SSLContext:
    """A TLS context that will not complete a handshake without a client cert
    issued by the device CA."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    ctx.load_verify_locations(cafile=ca_file)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
