"""FastAPI app for the mcbot dashboard (the *controller*).

This process owns authentication (OIDC via Authentik), serves the SPA, and
reverse-proxies every bot-control call to the separate long-lived *bot-host*
process (``host.py``). Because the bots live in that other process, this one
can restart freely -- deploys, crashes, auth/config changes -- without dropping
any bot's connection.

Run:
    uvicorn app:app --host 127.0.0.1 --port 21306
(served behind the reverse proxy at minebot.dury.dev)
"""

from __future__ import annotations

import asyncio
import os

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import auth
from auth import current_user

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# The bot-host's localhost API. Everything under /api/* is forwarded here.
BOTHOST_URL = os.environ.get("BOTHOST_URL", "http://127.0.0.1:21307").rstrip("/")
BOTHOST_WS = "ws" + BOTHOST_URL[len("http"):]  # http->ws, https->wss

app = FastAPI(title="mcbot dashboard")
app.include_router(auth.router)

# Long-lived client for proxying REST calls to the bot-host.
_client: httpx.AsyncClient | None = None
# Hop-by-hop / host-specific headers we must not blindly forward.
_SKIP_REQ_HEADERS = {"host", "content-length", "connection", "cookie"}
_SKIP_RESP_HEADERS = {"content-length", "content-encoding", "transfer-encoding",
                      "connection"}


@app.on_event("startup")
async def _startup():
    global _client
    _client = httpx.AsyncClient(base_url=BOTHOST_URL, timeout=30.0)


@app.on_event("shutdown")
async def _shutdown():
    if _client is not None:
        await _client.aclose()


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Gate every HTTP route except the public login paths."""
    path = request.url.path
    if auth.is_public_path(path) or current_user(request) is not None:
        return await call_next(request)
    if path.startswith("/api/"):
        return Response('{"detail":"not authenticated"}', status_code=401,
                        media_type="application/json")
    # Browser navigation -> send to the login flow, remembering where we were.
    return RedirectResponse(f"/auth/login?next={path}", status_code=302)


# -- reverse proxy: /api/* -> bot-host --------------------------------------
@app.api_route("/api/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_api(path: str, request: Request):
    """Forward an authenticated REST call to the bot-host and relay its reply.

    Auth has already been enforced by the middleware; the bot-host trusts this
    caller, so we don't forward the session cookie."""
    assert _client is not None
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _SKIP_REQ_HEADERS}
    try:
        upstream = await _client.request(
            request.method, f"/api/{path}",
            params=request.query_params, content=body, headers=headers)
    except httpx.ConnectError:
        return Response('{"detail":"bot-host unavailable"}', status_code=502,
                        media_type="application/json")
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in _SKIP_RESP_HEADERS}
    return Response(upstream.content, status_code=upstream.status_code,
                    headers=resp_headers,
                    media_type=upstream.headers.get("content-type"))


# -- WebSocket relay: /api/bots/{id}/ws -> bot-host -------------------------
@app.websocket("/api/bots/{bot_id}/ws")
async def ws_proxy(websocket: WebSocket, bot_id: str):
    # The HTTP middleware doesn't run for WS, so authenticate here.
    if current_user(websocket) is None:
        await websocket.close(code=4401)  # unauthenticated
        return
    await websocket.accept()
    try:
        upstream = await websockets.connect(f"{BOTHOST_WS}/api/bots/{bot_id}/ws")
    except Exception:  # noqa: BLE001 - bot-host down or refused the socket
        await websocket.close(code=1011)
        return

    async def downstream_to_upstream():
        try:
            while True:
                await upstream.send(await websocket.receive_text())
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            pass

    async def upstream_to_downstream():
        try:
            async for message in upstream:
                await websocket.send_text(message)
        except websockets.ConnectionClosed:
            pass

    d2u = asyncio.ensure_future(downstream_to_upstream())
    u2d = asyncio.ensure_future(upstream_to_downstream())
    try:
        await asyncio.wait({d2u, u2d}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (d2u, u2d):
            task.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed


# -- static frontend (mounted last so /api/* wins) --------------------------
@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
