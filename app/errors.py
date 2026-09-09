"""Consistent JSON error envelope, per spec section 11."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

# Payload problems are 4xx; 5xx is reserved for genuine server faults.
ERROR_STATUS: dict[str, int] = {
    "INVALID_DEVICE": 401,
    "DEVICE_DISABLED": 403,
    "DEVICE_UPLOADS_PAUSED": 403,
    "DEVICE_CERT_MISMATCH": 403,
    "DEVICE_NOT_OWNER": 403,
    "UNKNOWN_SESSION": 404,
    "INVALID_SCHEMA_VERSION": 422,
    "INVALID_MANIFEST": 422,
    "INVALID_KEY_WRAP": 422,
    "CHUNK_NOT_IN_MANIFEST": 422,
    "CHUNK_HASH_MISMATCH": 422,
    "CHUNK_DECRYPT_FAILED": 422,
    "PLAINTEXT_HASH_MISMATCH": 422,
    "INVALID_FLAC": 422,
    "NONCE_REUSE": 422,
    "INVALID_REQUEST": 400,
    "MISSING_IDEMPOTENCY_KEY": 400,
    "IDEMPOTENCY_CONFLICT": 409,
    "SESSION_DEVICE_CONFLICT": 409,
    "CHUNK_CONFLICT": 409,
    "MISSING_CHUNKS": 409,
    "SESSION_ALREADY_FINALIZED": 409,
    "SESSION_PURGED": 410,
    "PAYLOAD_TOO_LARGE": 413,
    "RATE_LIMITED": 429,
    "NO_SERVER_KEY": 503,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code or ERROR_STATUS.get(code, 400)
        self.extra = extra or {}

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"].update(self.extra)
        return body


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    import logging

    logging.getLogger("visitescribe").exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
    )
