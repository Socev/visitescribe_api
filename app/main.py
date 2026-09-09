"""Entrypoint: serves the ingest API and the admin interface on separate ports.

Keeping them on different ports (and different ASGI apps) is what lets the
Olares chart give the API a *public* entrance while the admin interface sits
behind a *private* one. There is no route from one port to the other.

Optionally a third listener terminates TLS itself and demands a client
certificate, for deployments that reach the pod directly over the LAN rather
than through the gateway.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from .admin_app import create_admin_app
from .api_app import create_app
from .bootstrap import initialise
from .config import settings

log = logging.getLogger("visitescribe")


def _log_config(level: str) -> dict:
    config = uvicorn.config.LOGGING_CONFIG.copy()
    config["formatters"]["default"]["fmt"] = (
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config["formatters"]["access"]["fmt"] = (
        '%(asctime)s INFO access %(client_addr)s "%(request_line)s" %(status_code)s'
    )
    return config


def _server(app, port: int, name: str, ssl_kwargs: dict | None = None) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=settings.bind_host,
        port=port,
        log_level=settings.log_level,
        log_config=_log_config(settings.log_level),
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        date_header=True,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=20,
        **(ssl_kwargs or {}),
    )
    server = uvicorn.Server(config)
    server.config.callback_notify = None
    log.info("%s listening on %s:%s", name, settings.bind_host, port)
    return server


def _mtls_server(app):
    """A listener that will not complete a handshake without a client cert."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    from .mtls import make_protocol_class

    missing = [
        name for name, value in (
            ("VS_MTLS_CERT_FILE", settings.mtls_cert_file),
            ("VS_MTLS_KEY_FILE", settings.mtls_key_file),
            ("VS_MTLS_CA_FILE", settings.mtls_ca_file),
        ) if not value
    ]
    if missing:
        log.warning("VS_MTLS_PORT is set but %s missing; skipping the mTLS listener",
                    ", ".join(missing))
        return None
    import ssl as ssl_module

    return _server(
        app, settings.mtls_port, "mtls-api",
        ssl_kwargs={
            "ssl_certfile": settings.mtls_cert_file,
            "ssl_keyfile": settings.mtls_key_file,
            "ssl_ca_certs": settings.mtls_ca_file,
            "ssl_cert_reqs": ssl_module.CERT_REQUIRED,
            "http": make_protocol_class(H11Protocol),
        },
    )


async def _run() -> None:
    api = create_app()
    admin = create_admin_app()

    servers = [
        _server(api, settings.api_port, "ingest-api"),
        _server(admin, settings.admin_port, "admin"),
    ]
    if settings.mtls_port:
        extra = _mtls_server(create_app())
        if extra is not None:
            servers.append(extra)

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    await asyncio.gather(*(server.serve() for server in servers))


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    initialise()
    log.info(
        "VisiteScribe starting: data=%s api=:%s admin=:%s mtls=%s",
        settings.data_dir, settings.api_port, settings.admin_port,
        settings.mtls_port or "off",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
