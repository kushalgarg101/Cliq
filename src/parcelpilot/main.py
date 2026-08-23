from __future__ import annotations

import json

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent import Agent
from .auth import actor_from_request, login, require_csrf, seed_demo_users, set_session
from .config import ROOT, get_settings
from .db import connect, initialize_runtime
from .ingest import ensure_ingested
from .services import confirm_action, scan_issue_signals

settings = get_settings()
initialize_runtime(settings.runtime_db)
if settings.environment == "demo":
    seed_demo_users(settings.runtime_db)
data_ready = ensure_ingested(settings.source_zip, settings.source_db)
agent = Agent(settings, settings.source_db, settings.runtime_db)
app = FastAPI(
    title="ParcelPilot Support",
    docs_url="/docs" if settings.environment == "demo" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment == "demo" else None,
)
templates = Jinja2Templates(directory=str(ROOT / "src" / "parcelpilot" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "src" / "parcelpilot" / "static")), name="static")


def llm_status() -> str:
    if settings.llm_mode == "offline":
        return "limited deterministic mode"
    if not settings.llm_api_key:
        return "provider key missing" if settings.llm_mode == "provider" else "limited mode · provider key not configured"
    return f"{settings.llm_provider} · {settings.llm_model}"


@app.get("/healthz")
def healthz():
    """Unauthenticated deployment health check; it never reveals tenant data."""
    if not data_ready:
        raise HTTPException(503, "Assessment data pack is not ready.")
    return {"status": "ok", "data_ready": True}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'"
    )
    if request.url.path in {"/", "/login"} or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "demo": settings.environment == "demo"})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(), password: str = Form()):
    session = login(settings.runtime_db, username, password)
    if not session:
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password.", "demo": settings.environment == "demo"}, status_code=401)
    response = RedirectResponse("/", status_code=303)
    set_session(response, *session, secure=settings.cookie_secure)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form()):
    actor = actor_from_request(request, settings.runtime_db)
    if csrf_token != actor["csrf_token"]:
        raise HTTPException(403, "Invalid CSRF token")
    session_id = request.cookies.get("pp_session")
    if session_id:
        with connect(settings.runtime_db) as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("pp_session")
    response.delete_cookie("pp_csrf")
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        actor = actor_from_request(request, settings.runtime_db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "actor": actor,
            "data_ready": data_ready,
            "provider": llm_status(),
            "full_chat_available": settings.llm_mode != "offline" and bool(settings.llm_api_key),
            "provider_configuration_required": settings.llm_mode == "provider" and not settings.llm_api_key,
        },
    )


@app.get("/api/me")
def me(request: Request):
    actor = actor_from_request(request, settings.runtime_db)
    return {key: actor[key] for key in ("username", "role", "account_id", "display_name")}


@app.post("/api/chat")
async def chat(request: Request):
    actor = actor_from_request(request, settings.runtime_db)
    require_csrf(request, actor)
    if not data_ready:
        raise HTTPException(503, "Data pack is not configured. Set DATA_PACK_ZIP and restart.")
    try:
        payload = await request.json()
    except json.JSONDecodeError as error:
        raise HTTPException(400, "Request body must be valid JSON.") from error
    if not isinstance(payload, dict):
        raise HTTPException(422, "Request body must be a JSON object.")
    raw_message = payload.get("message", "")
    if not isinstance(raw_message, str):
        raise HTTPException(422, "Message must be a string.")
    message = raw_message.strip()
    if not message:
        raise HTTPException(422, "Message is required")
    if len(message) > 4000:
        raise HTTPException(422, "Messages are limited to 4,000 characters.")
    try:
        return agent.reply(message, actor)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error


@app.post("/api/actions/{action_id}/confirm")
def action_confirm(action_id: str, request: Request):
    actor = actor_from_request(request, settings.runtime_db)
    require_csrf(request, actor)
    try:
        return confirm_action(settings.source_db, settings.runtime_db, actor, action_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/insights")
def insights(request: Request):
    actor = actor_from_request(request, settings.runtime_db)
    try:
        return scan_issue_signals(settings.source_db, actor)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
