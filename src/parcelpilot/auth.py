from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import HTTPException, Request, Response

from .db import connect

DEMO_USERS = (
    ("northstar", "customer", "ACCT-001", "Northstar Logistics"),
    ("lumenworks", "customer", "ACCT-002", "LumenWorks"),
    ("beacon", "customer", "ACCT-003", "Beacon Retail"),
    ("axis", "customer", "ACCT-004", "Axis Labs"),
    ("maya", "support_agent", None, "Maya, Support Agent"),
    ("opslead", "operations_lead", None, "Operations Lead"),
)
VALID_ROLES = {"customer", "support_agent", "operations_lead"}
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 512


def seed_demo_users(runtime_db, password: str = "parcelpilot-demo") -> None:
    with connect(runtime_db) as db:
        for username, role, account_id, display_name in DEMO_USERS:
            if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                continue
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
            db.execute("INSERT INTO users(username,password_hash,role,account_id,display_name) VALUES (?,?,?,?,?)", (username, hashed, role, account_id, display_name))


def login(runtime_db, username: str, password: str) -> tuple[str, str] | None:
    normalised_username = username.strip().lower()
    if (
        not normalised_username
        or not password
        or len(normalised_username) > MAX_USERNAME_LENGTH
        or len(password) > MAX_PASSWORD_LENGTH
    ):
        return None
    with connect(runtime_db) as db:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(UTC).isoformat(),))
        user = db.execute("SELECT * FROM users WHERE username = ? AND active = 1", (normalised_username,)).fetchone()
        try:
            password_matches = bool(user) and bcrypt.checkpw(password.encode(), user["password_hash"])
        except (TypeError, ValueError):
            password_matches = False
        if not user or user["role"] not in VALID_ROLES or not password_matches:
            return None
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        expiry = (datetime.now(UTC) + timedelta(hours=8)).isoformat()
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, user["id"], csrf, expiry))
        db.execute(
            "INSERT INTO audit_events(user_id,event_type,detail,created_at) VALUES (?,?,?,?)",
            (user["id"], "login", "Successful password login", datetime.now(UTC).isoformat()),
        )
        return session_id, csrf


def actor_from_request(request: Request, runtime_db):
    session_id = request.cookies.get("pp_session")
    if not session_id:
        raise HTTPException(401, "Sign in required")
    with connect(runtime_db) as db:
        row = db.execute("SELECT u.*, s.csrf_token, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND u.active=1", (session_id,)).fetchone()
    try:
        expired = not row or datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC)
    except (TypeError, ValueError):
        expired = True
    if not row or row["role"] not in VALID_ROLES or expired:
        if row:
            with connect(runtime_db) as db:
                db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        raise HTTPException(401, "Session expired")
    return dict(row)


def require_csrf(request: Request, actor: dict) -> None:
    if request.headers.get("X-CSRF-Token") != actor["csrf_token"]:
        raise HTTPException(403, "Invalid CSRF token")


def set_session(response: Response, session_id: str, csrf: str, *, secure: bool) -> None:
    response.set_cookie("pp_session", session_id, httponly=True, secure=secure, samesite="lax", max_age=28800)
    response.set_cookie("pp_csrf", csrf, httponly=False, secure=secure, samesite="lax", max_age=28800)
