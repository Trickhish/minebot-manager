"""OIDC (Authentik) login for the dashboard.

Authorization-Code flow with a confidential client. Identity is resolved by
calling the provider's userinfo endpoint with the access token, so this needs
no JWT/JWKS crypto dependency -- only stdlib urllib. Sessions are opaque random
ids backed by an in-memory store (single process, same as the bots), carried in
an HttpOnly cookie.

Config comes from environment (see dashboard/.env):
    OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET,
    DASHBOARD_BASE_URL, SESSION_SECRET, AUTH_DISABLED
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "minebot_session"
_STATE_TTL = 600          # seconds an in-flight login may take
_SESSION_TTL = 12 * 3600  # session lifetime

AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "0") == "1"
ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/") + "/"
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
REDIRECT_URI = f"{BASE_URL}/auth/callback"

# Public paths that must be reachable without a session.
PUBLIC_PREFIXES = ("/auth/login", "/auth/callback", "/style.css", "/favicon")

router = APIRouter()

# In-memory stores. Single-process, cleared on restart -- users just log in
# again, same tradeoff as the bots being in-memory.
_sessions: dict[str, dict] = {}   # sid -> {"user":..., "exp":...}
_pending: dict[str, dict] = {}    # state -> {"nonce":..., "exp":..., "next":...}

_discovery_cache: dict | None = None


def _discovery() -> dict:
    """Fetch + cache the provider's OIDC discovery document."""
    global _discovery_cache
    if _discovery_cache is None:
        url = f"{ISSUER}.well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=10) as resp:
            _discovery_cache = json.load(resp)
    return _discovery_cache


def _new_sid() -> str:
    return secrets.token_urlsafe(32)


def current_user(request: Request) -> dict | None:
    """The logged-in user for this request, or None. Honors AUTH_DISABLED."""
    if AUTH_DISABLED:
        return {"sub": "dev", "name": "dev (auth disabled)", "email": ""}
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    sess = _sessions.get(sid)
    if not sess or sess["exp"] < time.time():
        _sessions.pop(sid, None)
        return None
    return sess["user"]


def is_public_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


# -- routes ------------------------------------------------------------------
@router.get("/auth/login")
async def login(request: Request):
    if AUTH_DISABLED:
        return RedirectResponse("/", status_code=302)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    next_url = request.query_params.get("next", "/")
    _prune(_pending)
    _pending[state] = {"nonce": nonce, "exp": time.time() + _STATE_TTL, "next": next_url}
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
    })
    return RedirectResponse(f"{_discovery()['authorization_endpoint']}?{params}", status_code=302)


@router.get("/auth/callback")
async def callback(request: Request):
    error = request.query_params.get("error")
    if error:
        raise HTTPException(400, f"login failed: {error}")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    pending = _pending.pop(state, None) if state else None
    if not code or not pending or pending["exp"] < time.time():
        raise HTTPException(400, "invalid or expired login state")

    token_resp = _post_token(code)
    access_token = token_resp.get("access_token")
    if not access_token:
        raise HTTPException(400, "no access token from provider")

    user = _userinfo(access_token)
    sid = _new_sid()
    _prune(_sessions)
    _sessions[sid] = {
        "user": {"sub": user.get("sub"), "name": user.get("name") or user.get("preferred_username"),
                 "email": user.get("email", "")},
        "exp": time.time() + _SESSION_TTL,
    }
    resp = RedirectResponse(pending.get("next") or "/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, sid, max_age=_SESSION_TTL, httponly=True,
                    samesite="lax", secure=True, path="/")
    return resp


@router.post("/auth/logout")
async def logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        _sessions.pop(sid, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/api/me")
async def me(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user


# -- helpers -----------------------------------------------------------------
def _post_token(code: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(_discovery()["token_endpoint"], data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _userinfo(access_token: str) -> dict:
    req = urllib.request.Request(_discovery()["userinfo_endpoint"],
                                 headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _prune(store: dict) -> None:
    now = time.time()
    for key in [k for k, v in store.items() if v.get("exp", 0) < now]:
        store.pop(key, None)
