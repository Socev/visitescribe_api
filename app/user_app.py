"""The user-facing site (default port 8082, a private Olares entrance).

A third ASGI app on a third socket, for the same reason the admin interface is
one: separation by port rather than by path. The public ingest entrance has no
route here, and this app has no route to the admin interface.

What a user can reach is scoped to the devices an admin bound to them. There
is no "all sessions" query in this file at all -- every read joins through
devices.user_id, so a missing filter is a missing join and fails loudly rather
than showing someone else's consultations.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import audit, db, processing, routing, sessions, userauth, users
from .bootstrap import initialise
from .config import settings
from .errors import ApiError, api_error_handler, unhandled_handler
from .providers import ProviderError
from .providers import get as get_provider
from .user_html import render_login, render_recording, render_recordings, render_settings


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    initialise()
    yield


def _cookie(response: Response, token: str) -> None:
    response.set_cookie(
        userauth.COOKIE_NAME, token, httponly=True, samesite="lax",
        secure=True, max_age=userauth.SESSION_HOURS * 3600, path="/",
    )


def _who(user: dict[str, Any]) -> str:
    return user.get("display_name") or user.get("email") or ""


def _recordings_of(user_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every recording from every device bound to this user.

    The join through devices IS the authorisation. There is no separate check
    to forget.
    """
    rows = db.query(
        "SELECT s.session_id, s.mode, s.state, s.ingest_confirmed, s.started_at, "
        "s.created_at, "
        # Duration is not a column: it is the sum of the per-chunk FLAC
        # headers, which is the only place the real length is recorded.
        "(SELECT SUM(json_extract(c.flac_json,'$.duration_ms')) / 1000.0 "
        " FROM chunks c WHERE c.session_id = s.session_id) AS duration_seconds, "
        "(SELECT route FROM processing_jobs j WHERE j.session_id = s.session_id "
        " ORDER BY j.id LIMIT 1) AS route, "
        "(SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id "
        " AND e.event = 'patient_boundary') + 1 AS segment_count "
        "FROM sessions s JOIN devices d ON d.device_id = s.device_id "
        "WHERE d.user_id = ? AND s.state != 'PURGED' "
        "ORDER BY COALESCE(s.started_at, s.created_at) DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


def _recording_of(user_id: str, session_id: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT s.* FROM sessions s JOIN devices d ON d.device_id = s.device_id "
        "WHERE d.user_id = ? AND s.session_id = ?", (user_id, session_id))
    if row is None:
        # Deliberately the same answer as a session that does not exist: a
        # user must not be able to learn that someone else's recording is real.
        raise ApiError("UNKNOWN_SESSION", "Deze opname bestaat niet.",
                       status_code=404)
    return dict(row)


def create_user_app() -> FastAPI:
    app = FastAPI(title="VisiteScribe", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=_lifespan)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_handler)

    # -- signing in ------------------------------------------------------
    @app.get("/inloggen", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if userauth.current_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render_login())

    @app.post("/inloggen", response_class=HTMLResponse)
    async def login_start(email: str = Form("")) -> Response:
        try:
            userauth.start_login(email)
        except (ApiError, ProviderError) as exc:
            return HTMLResponse(render_login(error=str(exc)), status_code=200)
        # The same page whether or not the address is known.
        return HTMLResponse(render_login(
            email=users.normalise_email(email), stage="code",
            notice="Als dit adres bij OurMind bekend is, is de code onderweg."))

    @app.post("/inloggen/code", response_class=HTMLResponse)
    async def login_finish(email: str = Form(""), code: str = Form("")) -> Response:
        try:
            token, user = userauth.finish_login(email, code)
        except (ApiError, ProviderError) as exc:
            return HTMLResponse(render_login(
                email=users.normalise_email(email), stage="code", error=str(exc)))
        response = RedirectResponse("/", status_code=303)
        _cookie(response, token)
        return response

    @app.post("/uitloggen")
    async def logout(request: Request) -> Response:
        userauth.sign_out(request.cookies.get(userauth.COOKIE_NAME))
        response = RedirectResponse("/inloggen", status_code=303)
        response.delete_cookie(userauth.COOKIE_NAME, path="/")
        return response

    # -- recordings ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        user = userauth.current_user(request)
        if user is None:
            return RedirectResponse("/inloggen", status_code=303)
        users.touch(user["user_id"])
        return HTMLResponse(render_recordings({
            "recordings": _recordings_of(user["user_id"]),
            "types": users.recording_types(),
        }, _who(user)))

    @app.get("/opname/{session_id}", response_class=HTMLResponse)
    async def recording(session_id: str, request: Request) -> Response:
        user = userauth.require_user(request)
        rec = _recording_of(user["user_id"], session_id)
        rec["duration_seconds"] = (db.query_one(
            "SELECT SUM(json_extract(flac_json,'$.duration_ms')) / 1000.0 AS d "
            "FROM chunks WHERE session_id = ?", (session_id,)) or {})["d"]
        results = processing.results_for(session_id)
        by_segment: dict[Any, dict[str, Any]] = {}
        for item in results["transcripts"]:
            by_segment.setdefault(item["segment_index"], {})["transcript"] = item["text"]
        for item in results["notes"]:
            entry = by_segment.setdefault(item["segment_index"], {})
            entry["note"] = item["body"]
        merged = [{"segment_index": k, **v} for k, v in sorted(
            by_segment.items(), key=lambda kv: (kv[0] is not None, kv[0]))]
        busy = any(j["state"] in ("queued", "running") for j in results["jobs"])
        return HTMLResponse(render_recording({
            "recording": rec,
            "results": merged,
            "types": users.recording_types(),
            "allowed_routes": sorted(routing.allowed_for(rec["mode"])),
            "busy": busy,
        }, _who(user)))

    @app.post("/opname/{session_id}/verwerken")
    async def process(session_id: str, request: Request,
                      route: str = Form("")) -> Response:
        user = userauth.require_user(request)
        _recording_of(user["user_id"], session_id)      # authorisation
        try:
            processing.enqueue(session_id, route, actor=user["email"])
        except (ApiError, ProviderError) as exc:
            audit.log("processing", "user_enqueue", "failure",
                      session_id=session_id, identity=user["email"],
                      detail={"reason": str(exc)[:300]})
        return RedirectResponse(f"/opname/{session_id}", status_code=303)

    # -- settings --------------------------------------------------------
    @app.get("/instellingen", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        user = userauth.require_user(request)
        templates: list[dict[str, Any]] = []
        template_error = ""
        quota: dict[str, Any] = {}
        try:
            client = get_provider("ourmind", token=users.access_token(user["user_id"]))
            templates = client.templates()
            quota = client.me()
        except (ApiError, ProviderError) as exc:
            template_error = str(exc)

        types = []
        for kind in users.recording_types():
            types.append({**kind, "allowed": sorted(routing.allowed_for(kind["mode"]))})

        return HTMLResponse(render_settings({
            "types": types,
            "rules": users.rules_for(user["user_id"]),
            "templates": templates,
            "template_error": template_error,
            "quota": quota,
            "email": user["email"],
            "org_name": user["org_name"],
            "auto_allowed": settings.auto_process,
        }, _who(user)))

    @app.post("/instellingen")
    async def save_settings(request: Request) -> Response:
        user = userauth.require_user(request)
        form = await request.form()
        titles = {t["id"]: t["title"] for t in []}
        for kind in users.recording_types():
            mode = kind["mode"]
            route = str(form.get(f"route__{mode}") or "").strip()
            raw = str(form.get(f"template__{mode}") or "").strip()
            template_id, _, template_type = raw.partition(":")
            auto = bool(form.get(f"auto__{mode}"))
            if route and route not in routing.allowed_for(mode):
                # Ignore rather than fail the whole form: the picker is built
                # from the policy, so this only happens if the policy moved
                # while the page was open.
                continue
            users.set_rule(
                user["user_id"], mode, route=route, template_id=template_id,
                template_type=template_type or "template",
                template_title=titles.get(template_id, ""),
                auto=auto and bool(route), actor=user["email"])
        return RedirectResponse("/instellingen", status_code=303)

    @app.post("/ourmind/loskoppelen")
    async def disconnect(request: Request) -> Response:
        user = userauth.require_user(request)
        users.forget_token(user["user_id"])
        audit.log("user_login", "ourmind_disconnected", "success",
                  identity=user["email"], detail={"user_id": user["user_id"]})
        return RedirectResponse("/instellingen", status_code=303)

    @app.get("/api/stand")
    async def stand(request: Request, opname: str = "") -> Response:
        """A cheap fingerprint of what this user can see.

        The pages poll this and reload only when it changes, so a recording
        that is still being processed updates by itself without throwing away
        your scroll position every few seconds.
        """
        user = userauth.require_user(request)
        if opname:
            _recording_of(user["user_id"], opname)      # authorisation
            row = db.query_one(
                "SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM processing_jobs "
                "WHERE session_id = ?", (opname,))
            res = db.query_one(
                "SELECT (SELECT COUNT(*) FROM transcripts WHERE session_id = ?) "
                "+ (SELECT COUNT(*) FROM notes WHERE session_id = ?) AS n", (opname, opname))
            state = db.query_one("SELECT state FROM sessions WHERE session_id = ?",
                                 (opname,))
            mark = f"{row['n']}:{row['u']}:{res['n']}:{state['state']}"
        else:
            row = db.query_one(
                "SELECT COUNT(*) AS n, MAX(s.updated_at) AS u FROM sessions s "
                "JOIN devices d ON d.device_id = s.device_id WHERE d.user_id = ?",
                (user["user_id"],))
            mark = f"{row['n']}:{row['u']}"
        return JSONResponse({"stand": mark})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app
