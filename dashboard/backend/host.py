"""The mcbot *bot-host*: the long-lived process that actually owns the bots.

It runs every `mcbot.Client` (with its reconnect loop), the macro engine, and
world/minimap state, and exposes the bot control API over HTTP + WebSocket on a
localhost-only port. The web dashboard (``app.py``) is a separate process that
authenticates users, serves the SPA, and reverse-proxies to this one -- so the
dashboard can restart (deploys, crashes, config changes) without dropping any
bot's connection. The roster is persisted to disk so a restart of *this*
process brings the same bots back too.

Bind to 127.0.0.1 only: this API is unauthenticated and trusts its single
caller (the dashboard). Nothing external should reach this port.

Run:
    uvicorn host:app --host 127.0.0.1 --port 21307
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

from macros import MacroEngine, MacroError
from manager import BotManager
from models import ChatRequest, CreateBotRequest, NavigateRequest, ServerAuthRequest
from textures import TILE, TextureAtlas

from mcbot.protocol import available_versions

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MACRO_STORE = os.path.join(DATA_DIR, "macros.json")
BOTS_STORE = os.path.join(DATA_DIR, "bots.json")
# Texture source for the first-person view. A resource pack the user owns
# (default: the one vendored alongside the framework). Set RESOURCE_PACK to
# override, or to "" to disable textures (view falls back to flat colors).
DEFAULT_PACK = os.path.join(os.path.dirname(__file__), "..", "..",
                            "framework", "default_rpack.zip")
RESOURCE_PACK = os.environ.get("RESOURCE_PACK", DEFAULT_PACK)

app = FastAPI(title="mcbot bot-host")
manager = BotManager(store_path=BOTS_STORE, request_model=CreateBotRequest)
macros = MacroEngine(manager, MACRO_STORE)
atlas: TextureAtlas | None = None


@app.on_event("startup")
async def _startup():
    global atlas
    manager.bind_loop(asyncio.get_running_loop())
    restored = manager.restore()
    if restored:
        print(f"[bot-host] restored {restored} bot(s) from {BOTS_STORE}")
    if RESOURCE_PACK and os.path.exists(RESOURCE_PACK):
        try:
            atlas = await asyncio.to_thread(TextureAtlas, RESOURCE_PACK, DATA_DIR)
            print(f"[bot-host] texture atlas: {len(atlas.stem_to_tile)} tiles "
                  f"({atlas.cols}x{atlas.rows})")
        except Exception as exc:  # noqa: BLE001 - textures are optional
            print(f"[bot-host] texture atlas unavailable: {exc}")


@app.on_event("shutdown")
async def _shutdown():
    manager.shutdown()


# -- REST --------------------------------------------------------------------
@app.get("/api/versions")
async def versions():
    return {"versions": available_versions()}


@app.get("/api/bots")
async def list_bots():
    return [b.status() for b in manager.list()]


@app.post("/api/bots")
async def create_bot(req: CreateBotRequest):
    if req.version != "auto" and req.version not in available_versions():
        raise HTTPException(400, f"unknown version {req.version!r}; "
                                 f"have {available_versions()}")
    if not (1 <= req.port <= 65535):
        raise HTTPException(400, "port out of range")
    bot = manager.create(req)
    return bot.status()


@app.get("/api/bots/{bot_id}")
async def get_bot(bot_id: str):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    return {**bot.status(), "history": bot.history()}


@app.post("/api/bots/{bot_id}/chat")
async def send_chat(bot_id: str, req: ChatRequest):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    if bot.state != "play":
        raise HTTPException(409, f"bot not in play state (state={bot.state})")
    try:
        bot.client.chat(req.message)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    return {"ok": True}


@app.put("/api/bots/{bot_id}/server-auth")
async def set_server_auth(bot_id: str, req: ServerAuthRequest):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    bot.set_server_auth(req.password, req.auto_register)
    return {"ok": True}


@app.delete("/api/bots/{bot_id}/server-auth")
async def clear_server_auth(bot_id: str):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    bot.clear_server_auth()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/navigate")
async def navigate_bot(bot_id: str, req: NavigateRequest):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    if bot.state != "play":
        raise HTTPException(409, f"bot not in play state (state={bot.state})")
    if bot.control_owner is not None:
        raise HTTPException(409, "exit first-person control before map navigation")
    navigation_id = bot.client.navigate_to(req.x, req.z)
    return {"ok": True, "navigation_id": navigation_id,
            "target": {"x": req.x, "z": req.z}}


@app.post("/api/bots/{bot_id}/stop")
async def stop_bot(bot_id: str):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    bot.stop()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/connect")
async def connect_bot(bot_id: str):
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    started = bot.reconnect()
    return {"ok": True, "started": started}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    if not manager.remove(bot_id):
        raise HTTPException(404, "no such bot")
    return {"ok": True}


@app.get("/api/bots/{bot_id}/state")
async def bot_state(bot_id: str):
    """Current player stats + inventory snapshot (for populating the panel on
    select; live changes arrive as 'stats'/'inventory' WS events)."""
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    return {"player": bot.player_state(), "inventory": bot.inventory_snapshot()}


@app.get("/api/bots/{bot_id}/map.png")
async def bot_map(bot_id: str, radius: int = 64,
                  center_x: int | None = None, center_z: int | None = None):
    """A top-down PNG minimap centered on the bot or an explicit X/Z point.
    Rendered on demand from the live world; the frontend polls this. 503 while
    the world isn't ready (wrong version, not in play, or no chunks parsed)."""
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    if bot.client.world is None:
        raise HTTPException(503, "world tracking unavailable for this bot/version")
    if bot.state != "play":
        raise HTTPException(503, f"bot not in play state (state={bot.state})")
    if (center_x is None) != (center_z is None):
        raise HTTPException(400, "center_x and center_z must be provided together")
    radius = max(8, min(256, radius))
    if center_x is None:
        position = bot.client.get_position()
        center_x, center_z = int(position["x"]), int(position["z"])
    center_x = max(-30_000_000, min(30_000_000, center_x))
    center_z = max(-30_000_000, min(30_000_000, center_z))
    # render_map is CPU-bound (numpy) and reads world state the bot's pump
    # thread mutates -- run it off the event loop and tolerate transient races.
    try:
        png = await asyncio.to_thread(_render_png, bot, radius, center_x, center_z)
    except Exception as exc:  # noqa: BLE001 - transient (chunk churn) or not-ready
        raise HTTPException(503, f"map not ready: {type(exc).__name__}")
    return Response(png, media_type="image/png",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Map-Center-X": str(center_x),
                        "X-Map-Center-Z": str(center_z),
                        "X-Map-Radius": str(radius),
                    })


def _render_png(bot, radius: int, center_x: int, center_z: int) -> bytes:
    from mcbot.render import encode_png
    return encode_png(bot.client.render_map(
        radius=radius, center_x=center_x, center_z=center_z))


@app.get("/api/textures/atlas.json")
async def atlas_meta():
    """Atlas grid geometry for the client's UV math, or has_textures=false."""
    if atlas is None:
        return {"has_textures": False}
    return {"has_textures": True, "tile": TILE, "cols": atlas.cols,
            "rows": atlas.rows, "stems": atlas.stem_to_tile}


@app.get("/api/textures/atlas.png")
async def atlas_png():
    if atlas is None:
        raise HTTPException(404, "no texture atlas")
    # `no-cache` = the browser may store it but must revalidate via ETag every
    # load, so a rebuilt atlas (e.g. new tints) propagates immediately instead
    # of being pinned for a day. Revalidation is a cheap 304 when unchanged.
    return FileResponse(atlas.png_path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/bots/{bot_id}/voxels")
async def bot_voxels(bot_id: str, radius: int = 40, up: int = 40, down: int = 40):
    """The nearby block volume around the bot, as a run-length-encoded voxel
    grid + color palette + camera pose. The browser meshes and renders this
    (first-person 'bot vision'); the server only serializes -- no rendering."""
    bot = manager.get(bot_id)
    if bot is None:
        raise HTTPException(404, "no such bot")
    if bot.client.world is None:
        raise HTTPException(503, "world tracking unavailable for this bot/version")
    if bot.state != "play":
        raise HTTPException(503, f"bot not in play state (state={bot.state})")
    radius = max(8, min(64, radius))
    up = max(4, min(64, up))
    down = max(4, min(64, down))
    try:
        return await asyncio.to_thread(_voxel_payload, bot, radius, up, down)
    except Exception as exc:  # noqa: BLE001 - transient chunk churn / not ready
        raise HTTPException(503, f"voxels not ready: {type(exc).__name__}")


def _voxel_payload(bot, radius: int, up: int, down: int) -> dict:
    import numpy as np
    from mcbot.blocks import AIR_NAMES
    from mcbot.colors import get_block_color

    client = bot.client
    world = client.world
    pos = client.position
    cx, cy, cz = int(pos["x"]), int(pos["y"]), int(pos["z"])

    origin, dims, ids = world.voxel_box(cx, cy, cz, radius, up, down)

    # Map each distinct block-state id to a palette index (0 = air/empty).
    uniq = np.unique(ids)
    bt = world.block_table
    palette = [[0, 0, 0]]
    lut = np.zeros(int(uniq.max()) + 1, dtype=np.uint16)
    for sid in uniq.tolist():
        name = bt.name_for(sid)
        if name is None or name in AIR_NAMES:
            continue  # stays 0 in the lut
        lut[sid] = len(palette)
        entry = list(get_block_color(name))
        if atlas is not None:  # append [top, side, bottom] atlas tile indices
            entry += list(atlas.face_tiles(name))
        info = bt.range_for(sid)
        entry.append(sid - info[0] if info is not None else -1)
        entry.append(name)
        palette.append(entry)
    idx_grid = lut[ids]  # (ny, nz, nx) uint16 palette indices

    # Vectorized run-length encoding over the C-order flattening (y, z, x).
    flat = idx_grid.reshape(-1)
    if flat.size:
        bounds = np.concatenate(
            ([0], np.nonzero(np.diff(flat))[0] + 1, [flat.size]))
        counts = np.diff(bounds).astype(np.int64)
        vals = flat[bounds[:-1]].astype(np.int64)
        rle = np.empty(counts.size * 2, dtype=np.int64)
        rle[0::2], rle[1::2] = counts, vals
        rle_list = rle.tolist()
    else:
        rle_list = []

    return {
        "origin": list(origin),
        "dims": list(dims),                     # (nx, ny, nz)
        "palette": palette,
        "rle": rle_list,                        # [count, idx, count, idx, ...]
        "eye": [pos["x"], pos["y"] + 1.62, pos["z"]],
        "yaw": pos["yaw"],
        "pitch": pos["pitch"],
    }


# -- macros ------------------------------------------------------------------
@app.get("/api/macros")
async def list_macros():
    return macros.list()


@app.post("/api/macros")
async def create_macro(body: dict):
    try:
        return macros.create(body)
    except MacroError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/macros/{macro_id}")
async def update_macro(macro_id: str, body: dict):
    try:
        return macros.update(macro_id, body)
    except KeyError:
        raise HTTPException(404, "no such macro")
    except MacroError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/macros/{macro_id}")
async def delete_macro(macro_id: str):
    if not macros.delete(macro_id):
        raise HTTPException(404, "no such macro")
    return {"ok": True}


@app.get("/api/bots/{bot_id}/macros")
async def bot_macros(bot_id: str):
    if manager.get(bot_id) is None:
        raise HTTPException(404, "no such bot")
    return macros.bot_status(bot_id)


@app.post("/api/bots/{bot_id}/macros/{macro_id}/run")
async def run_macro(bot_id: str, macro_id: str):
    try:
        return {"run_id": macros.run_now(bot_id, macro_id)}
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except (MacroError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/bots/{bot_id}/macros/{macro_id}/arm")
async def arm_macro(bot_id: str, macro_id: str):
    try:
        macros.arm(bot_id, macro_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except MacroError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/bots/{bot_id}/macros/{macro_id}/disarm")
async def disarm_macro(bot_id: str, macro_id: str):
    macros.disarm(bot_id, macro_id)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/runs/{run_id}/cancel")
async def cancel_run(bot_id: str, run_id: str):
    if not macros.cancel_run(run_id):
        raise HTTPException(404, "no such run")
    return {"ok": True}


# -- WebSocket: live event stream -------------------------------------------
# No auth here: the dashboard authenticates the user before relaying to us.
@app.websocket("/api/bots/{bot_id}/ws")
async def bot_ws(websocket: WebSocket, bot_id: str):
    bot = manager.get(bot_id)
    if bot is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    queue = bot.subscribe()
    owner = object()
    controls = {
        "active": False, "forward": 0.0, "strafe": 0.0,
        "jump": False, "sneak": False,
        "yaw": 0.0, "pitch": 0.0, "updated": 0.0,
    }

    async def send_events():
        await websocket.send_json({"type": "snapshot", "bot_id": bot_id,
                                   "data": {"status": bot.status(),
                                            "history": bot.history()}})
        while True:
            await websocket.send_json(await queue.get())

    async def receive_controls():
        while True:
            message = await websocket.receive_json()
            if (not isinstance(message, dict)
                    or message.get("type") != "control"
                    or not isinstance(message.get("data"), dict)):
                continue
            data = message["data"]
            active = bool(data.get("active"))
            if not active:
                controls["active"] = False
                if bot.control_owner is owner:
                    bot.control_owner = None
                continue
            bot.client.cancel_navigation()
            bot.control_owner = owner
            controls.update(
                active=True,
                forward=_control_number(data.get("forward"), -1.0, 1.0),
                strafe=_control_number(data.get("strafe"), -1.0, 1.0),
                jump=bool(data.get("jump")),
                sneak=bool(data.get("sneak")),
                yaw=_control_number(data.get("yaw"), -360000.0, 360000.0),
                pitch=_control_number(data.get("pitch"), -90.0, 90.0),
                updated=asyncio.get_running_loop().time(),
            )

    async def drive_bot():
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(0.05)
            # Advance physics by the real wall-clock elapsed time, not a fixed
            # 0.05, so loop jitter / await overhead doesn't play jumps and
            # movement back in slow motion. Clamp to keep a single step sane.
            now = loop.time()
            dt = max(0.01, min(0.1, now - last))
            last = now
            if not controls["active"] or bot.control_owner is not owner:
                continue
            if now - controls["updated"] > 0.5:
                controls["active"] = False
                if bot.control_owner is owner:
                    bot.control_owner = None
                continue
            if bot.state != "play":
                continue
            await asyncio.to_thread(
                bot.client.control_step,
                controls["forward"], controls["strafe"], controls["jump"],
                controls["sneak"], controls["yaw"], controls["pitch"], dt,
            )

    tasks = [asyncio.create_task(fn()) for fn in
             (send_events, receive_controls, drive_bot)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        controls["active"] = False
        if bot.control_owner is owner:
            bot.control_owner = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bot.unsubscribe(queue)


def _control_number(value, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(low, min(high, number))
