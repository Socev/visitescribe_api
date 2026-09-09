"""The public ingest application (default port 8080).

This app exposes only /v1 and health probes. The admin interface lives in a
separate ASGI application on its own port so that a public entrance can never
route to it, whatever the gateway configuration happens to be.
"""
from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, crypto, db, flacinfo
from .bootstrap import STARTUP_INFO, STARTUP_PROBLEMS, initialise
from .config import settings
from .errors import ApiError, api_error_handler, unhandled_handler
from .util import now_iso
from .v1 import router as v1_router

log = logging.getLogger("visitescribe")


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    initialise()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        title="VisiteScribe Ingest API",
        version=__version__,
        description=(
            "Encrypted session mailbox for the VisiteScribe medical audio "
            "recorder. Ingest is fully independent of any downstream "
            "transcription or processing provider."
        ),
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):  # noqa: ANN202, ARG001
        details = [
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}"
            for e in exc.errors()[:8]
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request failed validation",
                    "details": details,
                }
            },
        )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # noqa: ANN202
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(v1_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:  # noqa: ANN202
        """Liveness: true as soon as the process is serving.

        Deliberately does not touch storage. A pod that never turns Ready is
        invisible to the Olares installer, so a broken data directory has to
        surface as a readable diagnosis rather than a crash loop.
        """
        return {"status": "ok", "time": now_iso()}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():  # noqa: ANN202
        problems = list(STARTUP_PROBLEMS)
        try:
            db.query_one("SELECT 1 AS x")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"database: {exc}")
        else:
            if crypto.active_key() is None:
                problems.append("no active server key")
        # The worker lives in this process but off the request path, so its
        # health was invisible from outside. A dead worker looked exactly like
        # a slow provider: everything queued, nothing said why.
        worker = {}
        try:
            from . import processing

            worker = processing.worker_health()
            if worker.get("stalled"):
                problems.append(
                    f"processing worker last seen {worker['seconds_since']}s ago "
                    f"with {worker['queued_due']} job(s) due")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"worker health unavailable: {exc}")
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok" if not problems else "degraded",
                "version": __version__,
                "flac_decoder": ("libsndfile" if flacinfo.decoder_available()
                                 else "structural-only"),
                "problems": problems,
                "worker": worker,
                "startup": STARTUP_INFO,
                "time": now_iso(),
            },
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict:  # noqa: ANN202
        return {
            "service": "visitescribe-ingest",
            "version": __version__,
            "api": "/v1",
            "schema_version": settings.schema_version,
        }

    return app


app = create_app()
