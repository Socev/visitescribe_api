"""Admin application (default port 8081, private Olares entrance).

Kept in a separate ASGI app from the ingest API so that the public entrance
has no route to it at all, rather than relying on path matching.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import (__version__, adminauth, audit, crypto, db, flacinfo, pricing,
               processing, routing, sessions, storage, users)
from .providers import credentials as provider_credentials
from .providers import get as get_provider
from .admin_html import (
    render_audit,
    render_costs,
    render_dashboard,
    render_device_detail,
    render_devices,
    render_keys,
    render_login,
    render_session_detail,
    render_sessions,
    render_users,
)
from .auth import create_device
from .bootstrap import STARTUP_INFO, STARTUP_PROBLEMS, initialise
from .config import ALL_STATES, settings
from .errors import ApiError, api_error_handler, unhandled_handler
from .util import b64decode_strict, is_device_id, new_token, now, now_iso, token_hash


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    initialise()
    yield


# Ceiling on an in-memory WAV export.
MAX_WAV_EXPORT_BYTES = 512 * 1024 * 1024


def create_admin_app() -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        title="VisiteScribe Admin",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_handler)

    @app.middleware("http")
    async def _headers(request: Request, call_next):  # noqa: ANN202
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'",
        )
        return response

    # ---------------------------------------------------------------- auth
    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request) -> HTMLResponse:  # noqa: ANN202
        if adminauth.is_authenticated(request):
            return RedirectResponse("/admin/", status_code=303)
        return HTMLResponse(render_login(error=None))

    @app.post("/admin/api/login", include_in_schema=False)
    async def login(request: Request) -> Response:  # noqa: ANN202
        body = await _body(request)
        password = str(body.get("password") or "")
        ip = request.client.host if request.client else ""
        if not adminauth.check_password(password):
            audit.log("admin", "login_failed", "failure", source_ip=ip)
            raise ApiError("UNAUTHORIZED", "Invalid password", status_code=401)
        token, expires = adminauth.create_session(adminauth.gateway_user(request))
        audit.log("admin", "login", "success", source_ip=ip,
                  identity=adminauth.gateway_user(request) or "admin")
        response = JSONResponse({"ok": True, "expires_at": expires})
        response.set_cookie(
            adminauth.COOKIE_NAME, token, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
            max_age=settings.admin_session_hours * 3600, path="/",
        )
        return response

    @app.post("/admin/api/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:  # noqa: ANN202
        adminauth.destroy_session(request.cookies.get(adminauth.COOKIE_NAME))
        response = JSONResponse({"ok": True})
        response.delete_cookie(adminauth.COOKIE_NAME, path="/")
        return response

    def guard(request: Request) -> str:
        return adminauth.require(request)

    def html_guard(request: Request) -> str | RedirectResponse:
        if not adminauth.is_authenticated(request):
            return RedirectResponse("/admin/login", status_code=303)
        return adminauth.gateway_user(request) or "admin"

    # ------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:  # noqa: ANN202
        return RedirectResponse("/admin/", status_code=303)

    @app.get("/admin", include_in_schema=False)
    async def admin_redirect() -> RedirectResponse:  # noqa: ANN202
        return RedirectResponse("/admin/", status_code=303)

    @app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        return HTMLResponse(render_dashboard(_dashboard_data(), who))

    @app.get("/admin/sessions", response_class=HTMLResponse, include_in_schema=False)
    async def sessions_page(request: Request, state: str = "", device: str = "",
                            q: str = "", page: int = 1):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        data = _sessions_data(state=state, device=device, q=q, page=page)
        return HTMLResponse(render_sessions(data, who))

    @app.get("/admin/sessions/{session_id}", response_class=HTMLResponse,
             include_in_schema=False)
    async def session_page(session_id: str, request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        data = _session_detail(session_id)
        if data is None:
            return HTMLResponse("<h1>Unknown session</h1>", status_code=404)
        return HTMLResponse(render_session_detail(data, who))

    @app.get("/admin/devices", response_class=HTMLResponse, include_in_schema=False)
    async def devices_page(request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        return HTMLResponse(render_devices(_devices_data(), who))

    @app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
    async def users_page(request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        return HTMLResponse(render_users(_users_data(), who))

    @app.post("/admin/api/users", include_in_schema=False)
    async def create_user(request: Request):  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        user = users.create(str(body.get("email") or ""),
                            display_name=str(body.get("display_name") or ""),
                            actor=who)
        return JSONResponse({"user": user}, status_code=201)

    @app.post("/admin/api/users/{user_id}/enabled", include_in_schema=False)
    async def set_user_enabled(user_id: str, request: Request):  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        users.set_disabled(user_id, not bool(body.get("enabled", True)), actor=who)
        return JSONResponse({"ok": True})

    @app.post("/admin/api/devices/{device_id}/owner", include_in_schema=False)
    async def bind_device(device_id: str, request: Request):  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        users.bind_device(device_id, str(body.get("user_id") or "") or None,
                          actor=who)
        return JSONResponse({"ok": True})

    @app.get("/admin/devices/{device_id}", response_class=HTMLResponse,
             include_in_schema=False)
    async def device_page(device_id: str, request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        data = _device_detail(device_id)
        if data is None:
            return HTMLResponse("<h1>Unknown device</h1>", status_code=404)
        return HTMLResponse(render_device_detail(data, who))

    @app.get("/admin/keys", response_class=HTMLResponse, include_in_schema=False)
    async def keys_page(request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        return HTMLResponse(render_keys(crypto.all_keys(), who))

    @app.get("/admin/costs", response_class=HTMLResponse, include_in_schema=False)
    async def costs_page(request: Request):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        return HTMLResponse(render_costs({
            "costs": processing.cost_summary(),
            "queue": processing.queue_overview(),
            "policy": routing.describe(),
            "providers": provider_credentials.status(),
            "enabled": settings.processing_enabled,
        }, who))

    @app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
    async def audit_page(request: Request, category: str = "", outcome: str = "",
                         device_id: str = "", session_id: str = "", q: str = "",
                         page: int = 1):  # noqa: ANN202
        who = html_guard(request)
        if isinstance(who, RedirectResponse):
            return who
        per = 100
        rows = audit.recent(limit=per, offset=(max(page, 1) - 1) * per,
                            category=category, outcome=outcome, device_id=device_id,
                            session_id=session_id, q=q)
        total = audit.count(category=category, outcome=outcome, device_id=device_id,
                            session_id=session_id)
        return HTMLResponse(render_audit(rows, total, page, per,
                                         {"category": category, "outcome": outcome,
                                          "device_id": device_id,
                                          "session_id": session_id, "q": q}, who))

    # --------------------------------------------------------------- API
    @app.get("/admin/api/overview", include_in_schema=False)
    async def api_overview(request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse(_dashboard_data())

    @app.get("/admin/api/sessions", include_in_schema=False)
    async def api_sessions(request: Request, state: str = "", device: str = "",
                           q: str = "", page: int = 1) -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse(_sessions_data(state=state, device=device, q=q, page=page))

    @app.get("/admin/api/sessions/{session_id}", include_in_schema=False)
    async def api_session(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        data = _session_detail(session_id)
        if data is None:
            raise ApiError("UNKNOWN_SESSION", "Unknown session")
        return JSONResponse(data)

    @app.post("/admin/api/sessions/{session_id}/state", include_in_schema=False)
    async def api_set_state(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        state = str(body.get("state") or "").strip().upper()
        if state not in ALL_STATES:
            raise ApiError("INVALID_REQUEST", f"Unknown state {state!r}")
        if sessions.get(session_id) is None:
            raise ApiError("UNKNOWN_SESSION", "Unknown session")
        if state == "PURGED":
            raise ApiError("INVALID_REQUEST", "Use the purge action to purge a session")
        sessions.set_state(session_id, state, actor=who)
        return JSONResponse(sessions.status_payload(session_id))

    @app.post("/admin/api/sessions/{session_id}/processing", include_in_schema=False)
    async def api_process(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        """Queue a session for processing on a route the policy allows."""
        who = guard(request)
        body = await _body(request)
        route = str(body.get("route") or "").strip().lower()
        result = processing.enqueue(
            session_id, route, actor=who, force=bool(body.get("force")),
            template_id=str(body.get("template_id") or ""),
            template_type=str(body.get("template_type") or ""))
        return JSONResponse(result)

    @app.post("/admin/api/sessions/{session_id}/processing/cancel",
              include_in_schema=False)
    async def api_process_cancel(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        return JSONResponse({"cancelled": processing.cancel(session_id, actor=who)})

    @app.get("/admin/api/sessions/{session_id}/results", include_in_schema=False)
    async def api_results(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        if sessions.get(session_id) is None:
            raise ApiError("UNKNOWN_SESSION", "Unknown session")
        return JSONResponse(processing.results_for(session_id))

    @app.post("/admin/api/sessions/{session_id}/notes/{segment}/approve",
              include_in_schema=False)
    async def api_approve_note(session_id: str, segment: str,
                               request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        index = None if segment in ("-", "full") else int(segment)
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [str(body.get("status") or "approved"), now_iso()]
        if "body" in body:
            updates.insert(0, "body = ?")
            params.insert(0, str(body["body"]))
        params.extend([session_id, index])
        cursor = db.execute(
            f"UPDATE notes SET {', '.join(updates)} WHERE session_id = ? "
            f"AND IFNULL(segment_index, -1) = IFNULL(?, -1)", tuple(params))
        if not cursor.rowcount:
            raise ApiError("UNKNOWN_SESSION", "Geen verslag voor dit segment")
        audit.log("processing", "note_reviewed", "success", session_id=session_id,
                  identity=who, detail={"segment": index,
                                        "edited": "body" in body,
                                        "status": params[0] if "body" in body else params[0]})
        if sessions.get(session_id) and not db.query_one(
            "SELECT 1 FROM notes WHERE session_id = ? AND status != 'approved'",
            (session_id,)
        ):
            sessions.set_state(session_id, "APPROVED", ingest_confirmed=True, actor=who)
        return JSONResponse(processing.results_for(session_id))

    @app.get("/admin/api/processing", include_in_schema=False)
    async def api_processing_overview(request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse({
            "queue": processing.queue_overview(),
            "policy": routing.describe(),
            "providers": provider_credentials.status(),
            "prices": pricing.known_models(),
            "enabled": settings.processing_enabled,
        })

    @app.get("/admin/api/costs", include_in_schema=False)
    async def api_costs(request: Request, since: str = "") -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse(processing.cost_summary(since or None))

    @app.post("/admin/api/providers/{provider}/credential", include_in_schema=False)
    async def api_set_credential(provider: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        if provider not in routing.PROVIDERS:
            raise ApiError("INVALID_REQUEST", f"Onbekende provider {provider!r}")
        body = await _body(request)
        if body.get("remove"):
            provider_credentials.forget(provider)
            audit.log("provider", "credential_removed", "success", identity=who,
                      detail={"provider": provider})
            return JSONResponse({"provider": provider, "configured": False})
        secret = str(body.get("secret") or "").strip()
        if not secret:
            raise ApiError("INVALID_REQUEST", "Geen sleutel of token opgegeven")
        provider_credentials.store(
            provider, secret,
            kind=str(body.get("kind") or ("bearer" if provider == "ourmind" else "api_key")),
            meta={"source": "admin", "set_by": who},
            expires_at=body.get("expires_at"),
        )
        # Never logged, never echoed back — only that one was set.
        audit.log("provider", "credential_set", "success", identity=who,
                  detail={"provider": provider})
        return JSONResponse({"provider": provider, "configured": True})

    @app.get("/admin/api/providers/{provider}/quota", include_in_schema=False)
    async def api_provider_quota(provider: str, request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        instance = get_provider(provider)
        ready, why = instance.configured()
        if not ready:
            raise ApiError("PROVIDER_NOT_CONFIGURED", why)
        if not hasattr(instance, "quota"):
            raise ApiError("INVALID_REQUEST", "Deze provider rapporteert geen verbruik")
        return JSONResponse(instance.quota())

    @app.post("/admin/api/sessions/{session_id}/purge", include_in_schema=False)
    async def api_purge(session_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in ("source_audio", "working_audio", "all"):
            raise ApiError("INVALID_REQUEST",
                           "scope must be source_audio, working_audio or all")
        return JSONResponse(_purge(session_id, scope, who))

    @app.get("/admin/api/sessions/{session_id}/chunks/{sequence}/download",
             include_in_schema=False)
    async def api_download_chunk(session_id: str, sequence: int, request: Request,
                                 form: str = "encrypted"):  # noqa: ANN202
        who = guard(request)
        row = db.row_to_dict(db.query_one(
            "SELECT * FROM chunks WHERE session_id = ? AND sequence = ?",
            (session_id, sequence)))
        if row is None:
            raise ApiError("UNKNOWN_SESSION", "Unknown chunk")
        data = storage.read_blob(row["blob_path"])
        if form == "encrypted":
            audit.log("export", "chunk_downloaded", "success", session_id=session_id,
                      sequence=sequence, identity=who, detail={"form": "encrypted"})
            return Response(
                content=data, media_type="application/octet-stream",
                headers={"Content-Disposition":
                         f'attachment; filename="{session_id}-{sequence:06d}.flac.enc"'},
            )
        plaintext = _decrypt_chunk(session_id, row)
        audit.log("export", "chunk_downloaded", "success", session_id=session_id,
                  sequence=sequence, identity=who, detail={"form": "decrypted"})
        return Response(
            content=plaintext, media_type="audio/flac",
            headers={"Content-Disposition":
                     f'attachment; filename="{session_id}-{sequence:06d}.flac"'},
        )

    @app.get("/admin/api/sessions/{session_id}/audio.wav", include_in_schema=False)
    async def api_session_wav(session_id: str, request: Request):  # noqa: ANN202
        who = guard(request)
        wav = _session_wav(session_id)
        audit.log("export", "session_audio_exported", "success", session_id=session_id,
                  identity=who, detail={"format": "wav", "bytes": len(wav)})
        return Response(
            content=wav, media_type="audio/wav",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.wav"'},
        )

    @app.get("/admin/api/sessions/{session_id}/export.zip", include_in_schema=False)
    async def api_session_export(session_id: str, request: Request):  # noqa: ANN202
        who = guard(request)
        buf = _session_zip(session_id)
        audit.log("export", "session_exported", "success", session_id=session_id,
                  identity=who, detail={"format": "zip", "bytes": buf.getbuffer().nbytes})
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.zip"'},
        )

    # ------------------------------------------------------------ devices
    @app.get("/admin/api/devices", include_in_schema=False)
    async def api_devices(request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse(_devices_data())

    @app.post("/admin/api/devices", include_in_schema=False)
    async def api_create_device(request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        device_id = str(body.get("device_id") or "").strip()
        if not is_device_id(device_id):
            raise ApiError("INVALID_REQUEST", "device_id is malformed")
        if db.query_one("SELECT device_id FROM devices WHERE device_id = ?", (device_id,)):
            raise ApiError("INVALID_REQUEST", "Device already exists")
        issue = bool(body.get("issue_token", True))
        token = new_token() if issue else None
        create_device(
            device_id,
            display_name=str(body.get("display_name") or device_id),
            allow_header_only=bool(body.get("allow_header_only", not issue)),
            token_hash_value=token_hash(token) if token else None,
            token_hint=(token[:6] + "…") if token else None,
        )
        audit.log("device", "created", "success", device_id=device_id, identity=who,
                  detail={"token_issued": bool(token),
                          "allow_header_only": bool(body.get("allow_header_only", not issue))})
        return JSONResponse({"device_id": device_id, "token": token})

    @app.post("/admin/api/devices/{device_id}", include_in_schema=False)
    async def api_update_device(device_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        device = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        if device is None:
            raise ApiError("INVALID_REQUEST", "Unknown device")
        updates: list[str] = []
        params: list[Any] = []
        changed: dict[str, Any] = {}
        for field, column in (
            ("display_name", "display_name"), ("notes", "notes"),
        ):
            if field in body:
                updates.append(f"{column} = ?")
                params.append(str(body[field] or ""))
                changed[field] = body[field]
        for field in ("enabled", "allow_header_only", "upload_enabled"):
            if field in body:
                updates.append(f"{field} = ?")
                params.append(1 if body[field] else 0)
                changed[field] = bool(body[field])
        if "cert_fingerprint" in body:
            fp = str(body["cert_fingerprint"] or "").strip().lower().replace(":", "")
            if fp and (len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp)):
                raise ApiError("INVALID_REQUEST",
                               "cert_fingerprint must be a SHA-256 hex digest")
            updates.append("cert_fingerprint = ?")
            params.append(fp or None)
            changed["cert_fingerprint"] = fp or None
        if "config" in body:
            config = body["config"]
            if isinstance(config, str):
                try:
                    config = json.loads(config or "{}")
                except ValueError as exc:
                    raise ApiError("INVALID_REQUEST", f"config is not valid JSON: {exc}") from exc
            if not isinstance(config, dict):
                raise ApiError("INVALID_REQUEST", "config must be a JSON object")
            updates.append("config_json = ?")
            params.append(json.dumps(config))
            updates.append("config_version = config_version + 1")
            changed["config"] = config
        if not updates:
            raise ApiError("INVALID_REQUEST", "Nothing to update")
        updates.append("updated_at = ?")
        params.append(now_iso())
        params.append(device_id)
        db.execute(f"UPDATE devices SET {', '.join(updates)} WHERE device_id = ?", tuple(params))
        audit.log("device", "updated", "success", device_id=device_id, identity=who,
                  detail=changed)
        return JSONResponse(_device_detail(device_id) or {})

    @app.post("/admin/api/devices/{device_id}/token", include_in_schema=False)
    async def api_device_token(device_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        if db.query_one("SELECT device_id FROM devices WHERE device_id = ?", (device_id,)) is None:
            raise ApiError("INVALID_REQUEST", "Unknown device")
        if body.get("revoke"):
            db.execute(
                "UPDATE devices SET token_hash = NULL, token_hint = NULL, updated_at = ? "
                "WHERE device_id = ?", (now_iso(), device_id))
            audit.log("device", "token_revoked", "success", device_id=device_id, identity=who)
            return JSONResponse({"device_id": device_id, "token": None})
        token = new_token()
        db.execute(
            "UPDATE devices SET token_hash = ?, token_hint = ?, allow_header_only = 0, "
            "updated_at = ? WHERE device_id = ?",
            (token_hash(token), token[:6] + "…", now_iso(), device_id))
        audit.log("device", "token_issued", "success", device_id=device_id, identity=who)
        return JSONResponse({"device_id": device_id, "token": token})

    @app.post("/admin/api/devices/{device_id}/enrolment", include_in_schema=False)
    async def api_enrolment(device_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        body = await _body(request)
        if not is_device_id(device_id):
            raise ApiError("INVALID_REQUEST", "device_id is malformed")
        if body.get("cancel"):
            db.execute("DELETE FROM enrolment_windows WHERE device_id = ?", (device_id,))
            audit.log("device", "enrolment_cancelled", "success", device_id=device_id,
                      identity=who)
            return JSONResponse({"device_id": device_id, "expires_at": None})
        minutes = int(body.get("minutes") or 30)
        minutes = max(1, min(minutes, 1440))
        expires = (now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        db.execute(
            "INSERT INTO enrolment_windows(device_id, expires_at, created_at, created_by) "
            "VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET expires_at = excluded.expires_at",
            (device_id, expires, now_iso(), who))
        audit.log("device", "enrolment_opened", "success", device_id=device_id,
                  identity=who, detail={"minutes": minutes, "expires_at": expires})
        return JSONResponse({"device_id": device_id, "expires_at": expires})

    @app.delete("/admin/api/devices/{device_id}", include_in_schema=False)
    async def api_delete_device(device_id: str, request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        row = db.query_one("SELECT COUNT(*) AS n FROM sessions WHERE device_id = ?",
                           (device_id,))
        if row and int(row["n"]) > 0:
            raise ApiError("INVALID_REQUEST",
                           "Device still owns sessions; disable it instead of deleting")
        db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        audit.log("device", "deleted", "success", device_id=device_id, identity=who)
        return JSONResponse({"ok": True})

    # --------------------------------------------------------------- keys
    @app.get("/admin/api/keys", include_in_schema=False)
    async def api_keys(request: Request) -> JSONResponse:  # noqa: ANN202
        guard(request)
        return JSONResponse({"keys": crypto.all_keys()})

    @app.post("/admin/api/keys/rotate", include_in_schema=False)
    async def api_rotate(request: Request) -> JSONResponse:  # noqa: ANN202
        who = guard(request)
        key_id = crypto.generate_key(settings.keys_dir, bits=settings.rsa_key_bits,
                                     make_active=True)
        audit.log("key", "rotated", "success", identity=who, detail={"new_key_id": key_id})
        return JSONResponse({"key_id": key_id, "keys": crypto.all_keys()})

    @app.get("/admin/api/audit", include_in_schema=False)
    async def api_audit(request: Request, category: str = "", outcome: str = "",
                        device_id: str = "", session_id: str = "", q: str = "",
                        page: int = 1, per: int = 100) -> JSONResponse:  # noqa: ANN202
        guard(request)
        per = max(1, min(per, 1000))
        rows = audit.recent(limit=per, offset=(max(page, 1) - 1) * per, category=category,
                            outcome=outcome, device_id=device_id, session_id=session_id, q=q)
        return JSONResponse({
            "entries": rows,
            "total": audit.count(category=category, outcome=outcome,
                                 device_id=device_id, session_id=session_id),
            "page": page, "per_page": per,
        })

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:  # noqa: ANN202
        return {"status": "ok", "time": now_iso()}

    return app


# ---------------------------------------------------------------------------
# data helpers
# ---------------------------------------------------------------------------

async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ApiError("INVALID_REQUEST", f"Body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ApiError("INVALID_REQUEST", "Body must be a JSON object")
    return parsed


def _dashboard_data() -> dict[str, Any]:
    by_state = {
        r["state"]: int(r["n"])
        for r in db.query("SELECT state, COUNT(*) AS n FROM sessions GROUP BY state")
    }
    totals = db.query_one(
        "SELECT COUNT(*) AS sessions, "
        "SUM(CASE WHEN ingest_confirmed = 1 THEN 1 ELSE 0 END) AS confirmed "
        "FROM sessions"
    )
    chunk_totals = db.query_one(
        "SELECT COUNT(*) AS chunks, COALESCE(SUM(ciphertext_size),0) AS bytes, "
        "COALESCE(SUM(plaintext_size),0) AS plain_bytes FROM chunks"
    )
    failures = audit.recent(limit=15, category="integrity", outcome="failure")
    security = audit.recent(limit=15, category="security", outcome="failure")
    recent_sessions = [
        _session_row(dict(r))
        for r in db.query("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 12")
    ]
    devices = [dict(r) for r in db.query(
        "SELECT device_id, display_name, enabled, allow_header_only, token_hash, "
        "cert_fingerprint, last_seen_at, battery_percent, queue_count, network_state, "
        "software_version FROM devices ORDER BY device_id")]
    for d in devices:
        d["has_token"] = bool(d.pop("token_hash", None))
    total, used, free = storage.disk_usage()
    key = crypto.active_key()
    return {
        "version": __version__,
        "sessions_total": int(totals["sessions"] or 0) if totals else 0,
        "sessions_confirmed": int(totals["confirmed"] or 0) if totals else 0,
        "sessions_by_state": by_state,
        "chunks_total": int(chunk_totals["chunks"] or 0) if chunk_totals else 0,
        "ciphertext_bytes": int(chunk_totals["bytes"] or 0) if chunk_totals else 0,
        "plaintext_bytes": int(chunk_totals["plain_bytes"] or 0) if chunk_totals else 0,
        "blob_bytes": storage.blob_bytes(),
        "disk": {"total": total, "used": used, "free": free},
        "devices": devices,
        "recent_sessions": recent_sessions,
        "integrity_failures": failures,
        "security_failures": security,
        "flac_decoder": "libsndfile" if flacinfo.decoder_available() else "structural-only",
        "queue": processing.queue_overview(),
        "costs": processing.cost_summary(),
        "providers": provider_credentials.status(),
        "policy": routing.describe(),
        "active_key": key,
        "store_plaintext": settings.store_plaintext,
        "password_protected": adminauth.password_required(),
        "startup_problems": list(STARTUP_PROBLEMS),
        "startup_info": dict(STARTUP_INFO),
        "installed_at": db.get_meta("installed_at"),
        "server_time": now_iso(),
    }


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    sid = row["session_id"]
    received = db.query_one(
        "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (sid,))
    return {
        "session_id": sid,
        "device_id": row["device_id"],
        "mode": row["mode"],
        "state": row["state"],
        "client_status": row["client_status"],
        "ingest_confirmed": bool(row["ingest_confirmed"]),
        "expected_chunks": int(row["expected_chunks"] or 0),
        "received_chunks": int(received["n"]) if received else 0,
        "started_at": row["started_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error_code": row["error_code"],
    }


def _sessions_data(state: str = "", device: str = "", q: str = "",
                   page: int = 1, per: int = 50) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if state:
        where.append("state = ?")
        params.append(state)
    if device:
        where.append("device_id = ?")
        params.append(device)
    if q:
        where.append("session_id LIKE ?")
        params.append(f"%{q}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total_row = db.query_one(f"SELECT COUNT(*) AS n FROM sessions {clause}", tuple(params))
    page = max(1, page)
    rows = db.query(
        f"SELECT * FROM sessions {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, per, (page - 1) * per),
    )
    return {
        "sessions": [_session_row(dict(r)) for r in rows],
        "total": int(total_row["n"]) if total_row else 0,
        "page": page, "per_page": per,
        "filters": {"state": state, "device": device, "q": q},
        "states": list(ALL_STATES),
        "devices": [r["device_id"] for r in db.query(
            "SELECT DISTINCT device_id FROM sessions ORDER BY device_id")],
    }


def _session_detail(session_id: str) -> dict[str, Any] | None:
    session = sessions.get(session_id)
    if session is None:
        return None
    chunks = []
    for r in db.query(
        "SELECT * FROM chunks WHERE session_id = ? ORDER BY sequence", (session_id,)
    ):
        item = dict(r)
        try:
            item["flac"] = json.loads(item.pop("flac_json") or "{}")
        except (ValueError, TypeError):
            item["flac"] = {}
        item.pop("blob_path", None)
        item.pop("plaintext_blob_path", None)
        chunks.append(item)
    manifest_rows = [dict(r) for r in db.query(
        "SELECT * FROM manifest_chunks WHERE session_id = ? ORDER BY sequence", (session_id,))]
    try:
        manifest = json.loads(session["manifest_json"] or "{}")
    except (ValueError, TypeError):
        manifest = {}
    try:
        processing_meta = json.loads(session["processing_json"] or "{}")
    except (ValueError, TypeError):
        processing_meta = {}
    return {
        "session": _session_row(session),
        "raw": {k: v for k, v in session.items()
                if k not in ("manifest_json", "wrap_ciphertext_b64")},
        "audio": json.loads(session["audio_json"] or "{}"),
        "encryption": json.loads(session["encryption_json"] or "{}"),
        "manifest_summary": {
            "schema_version": manifest.get("schema_version"),
            "chunks": len(manifest_rows),
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "status": manifest.get("status"),
        },
        "manifest_chunks": manifest_rows,
        "chunks": chunks,
        "events": sessions.events_for(session_id),
        "segments": sessions.patient_segments(session_id),
        "privacy_gaps": sessions.privacy_gaps(session_id),
        "missing_chunks": sessions.missing_chunks(session_id),
        "unverified_chunks": sessions.unverified_chunks(session_id),
        "processing": processing_meta,
        "results": processing.results_for(session_id),
        "allowed_routes": sorted(routing.allowed_for(session["mode"])),
        **_owner_templates(session_id, session["mode"]),
        "audit": audit.recent(limit=200, session_id=session_id),
        "purges": [dict(r) for r in db.query(
            "SELECT * FROM purges WHERE session_id = ? ORDER BY id DESC", (session_id,))],
        "states": list(ALL_STATES),
    }


def _owner_templates(session_id: str, mode: str) -> dict[str, Any]:
    """The OurMind templates of whoever owns this recording.

    Reachable only through that user's own token, so this returns an
    explanation rather than an empty list when it cannot be had -- an empty
    picker with no reason is the kind of thing that gets reported as "it does
    not show anything".
    """
    owner = users.owner_of_session(session_id)
    if owner is None:
        return {"templates": [], "owner_rule": {},
                "template_error": "geen gebruiker aan dit device gekoppeld"}
    rule = users.rule(owner["user_id"], mode) or {}
    try:
        client = get_provider("ourmind", token=users.access_token(owner["user_id"]))
        return {"templates": client.templates(), "owner_rule": rule,
                "template_error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"templates": [], "owner_rule": rule,
                "template_error": str(exc)[:200]}


def _users_data() -> dict[str, Any]:
    rows = users.listing()
    for row in rows:
        row["devices"] = users.devices_of(row["user_id"])
        row["token"] = users.token_status(row["user_id"])
    return {"users": rows, "unbound": users.unbound_devices()}


def _devices_data() -> dict[str, Any]:
    devices = []
    for r in db.query("SELECT * FROM devices ORDER BY device_id"):
        item = dict(r)
        item["has_token"] = bool(item.pop("token_hash", None))
        counts = db.query_one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN ingest_confirmed = 1 THEN 1 ELSE 0 END) AS ok "
            "FROM sessions WHERE device_id = ?", (item["device_id"],))
        item["sessions"] = int(counts["n"]) if counts else 0
        item["sessions_confirmed"] = int(counts["ok"] or 0) if counts else 0
        owner = users.get(item["user_id"]) if item.get("user_id") else None
        item["owner_email"] = owner["email"] if owner else ""
        item["owner_name"] = (owner or {}).get("display_name") or ""
        devices.append(item)
    windows = {r["device_id"]: r["expires_at"] for r in db.query(
        "SELECT * FROM enrolment_windows")}
    return {"devices": devices, "enrolment_windows": windows,
            "require_device_auth": settings.require_device_auth}


def _device_detail(device_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return None
    device = dict(row)
    device["has_token"] = bool(device.pop("token_hash", None))
    try:
        device["config"] = json.loads(device.pop("config_json") or "{}")
    except (ValueError, TypeError):
        device["config"] = {}
    window = db.query_one("SELECT expires_at FROM enrolment_windows WHERE device_id = ?",
                          (device_id,))
    return {
        "device": device,
        "enrolment_expires_at": window["expires_at"] if window else None,
        "sessions": [_session_row(dict(r)) for r in db.query(
            "SELECT * FROM sessions WHERE device_id = ? ORDER BY created_at DESC LIMIT 50",
            (device_id,))],
        "audit": audit.recent(limit=100, device_id=device_id),
    }


def _decrypt_chunk(session_id: str, row: dict[str, Any]) -> bytes:
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    wrapped = session.get("wrap_ciphertext_b64")
    if not wrapped:
        raise ApiError("INVALID_KEY_WRAP", "Session has no server key wrap")
    key = crypto.cached_session_key(session_id)
    if key is None:
        try:
            key, _ = crypto.unwrap_session_key(b64decode_strict(wrapped),
                                               session.get("wrap_key_id"))
        except (ValueError, crypto.KeyWrapError) as exc:
            raise ApiError("INVALID_KEY_WRAP", str(exc)) from exc
        crypto.cache_session_key(session_id, key, settings.session_key_cache_seconds)
    data = storage.read_blob(row["blob_path"])
    try:
        return crypto.decrypt_chunk(key, b64decode_strict(row["nonce_b64"]),
                                    row["aad"].encode("utf-8"), data)
    except crypto.DecryptError as exc:
        raise ApiError("CHUNK_DECRYPT_FAILED", str(exc)) from exc


def _session_wav(session_id: str) -> bytes:
    import numpy as np
    import soundfile as sf

    rows = [dict(r) for r in db.query(
        "SELECT * FROM chunks WHERE session_id = ? ORDER BY sequence", (session_id,))]
    if not rows:
        raise ApiError("UNKNOWN_SESSION", "Session has no chunks")
    # A whole session is decoded and concatenated in memory here, so a long
    # recording is refused rather than allowed to exhaust the pod. The .zip
    # export streams and has no such limit.
    estimated = 0
    for row in rows:
        try:
            info = json.loads(row.get("flac_json") or "{}")
        except (ValueError, TypeError):
            info = {}
        rate = info.get("sample_rate") or 48000
        estimated += int((info.get("duration_ms") or 0) / 1000 * rate) *             max(int(info.get("channels") or 1), 1) * 2
    if estimated > MAX_WAV_EXPORT_BYTES:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            f"This session would export to about {estimated // (1024 * 1024)} MB of "
            f"WAV, above the {MAX_WAV_EXPORT_BYTES // (1024 * 1024)} MB limit. "
            "Download the .zip and decode it locally instead.",
        )
    blocks = []
    rate = None
    channels = None
    for row in rows:
        flac = _decrypt_chunk(session_id, row)
        with sf.SoundFile(io.BytesIO(flac)) as handle:
            if rate is None:
                rate, channels = handle.samplerate, handle.channels
            elif handle.samplerate != rate or handle.channels != channels:
                raise ApiError("INVALID_REQUEST",
                               "Chunks have inconsistent audio parameters")
            blocks.append(handle.read(dtype="int16", always_2d=True))
    audio = np.concatenate(blocks, axis=0)
    out = io.BytesIO()
    sf.write(out, audio, rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def _session_zip(session_id: str) -> io.BytesIO:
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", session["manifest_json"] or "{}")
        zf.writestr("events.json", json.dumps(sessions.events_for(session_id),
                                              indent=2, default=str))
        zf.writestr("segments.json", json.dumps(sessions.patient_segments(session_id),
                                                indent=2, default=str))
        zf.writestr("status.json", json.dumps(sessions.status_payload(session_id),
                                              indent=2, default=str))
        zf.writestr("audit.json", json.dumps(audit.recent(limit=5000, session_id=session_id),
                                             indent=2, default=str))
        for row in db.query("SELECT * FROM chunks WHERE session_id = ? ORDER BY sequence",
                            (session_id,)):
            zf.writestr(f"audio/chunk-{int(row['sequence']):06d}.flac.enc",
                        storage.read_blob(row["blob_path"]))
    return buf


def _purge(session_id: str, scope: str, who: str) -> dict[str, Any]:
    session = sessions.get(session_id)
    if session is None:
        raise ApiError("UNKNOWN_SESSION", "Unknown session")
    removed = 0
    if scope in ("working_audio", "all"):
        for row in db.query(
            "SELECT plaintext_blob_path FROM chunks WHERE session_id = ? "
            "AND plaintext_blob_path IS NOT NULL", (session_id,)
        ):
            if storage.delete_blob(row["plaintext_blob_path"]):
                removed += 1
        db.execute("UPDATE chunks SET plaintext_blob_path = NULL WHERE session_id = ?",
                   (session_id,))
    if scope in ("source_audio", "all"):
        removed += storage.delete_session_blobs(session_id)
        db.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
    crypto.drop_session_key(session_id)

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO purges(session_id, scope, actor, ts, detail_json) VALUES(?,?,?,?,?)",
            (session_id, scope, who, now_iso(),
             json.dumps({"files_removed": removed})),
        )
        if scope == "all":
            conn.execute(
                "UPDATE sessions SET state = 'PURGED', ingest_confirmed = 0, "
                "wrap_ciphertext_b64 = NULL, purged_at = ?, updated_at = ? "
                "WHERE session_id = ?",
                (now_iso(), now_iso(), session_id),
            )
        elif scope == "source_audio":
            # ingest_confirmed means "every manifested chunk is on durable
            # storage". Once the source audio is gone that is no longer true,
            # so the flag must drop with it rather than wait for the next
            # evaluate() to notice.
            conn.execute(
                "UPDATE sessions SET ingest_confirmed = 0, updated_at = ? "
                "WHERE session_id = ?", (now_iso(), session_id),
            )
        # The audit trail of the purge itself is deliberately retained.
        audit.log("purge", f"purged_{scope}", "success", session_id=session_id,
                  device_id=session["device_id"], identity=who,
                  detail={"files_removed": removed, "scope": scope}, conn=conn)
    return {"session_id": session_id, "scope": scope, "files_removed": removed,
            "state": (sessions.get(session_id) or {}).get("state")}


app = create_admin_app()
