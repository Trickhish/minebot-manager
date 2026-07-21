"""User-authored behavior scripts.

Unlike the declarative macro engine (``macros.py``), a *script* is arbitrary
Python executed against a curated ``BotAPI`` facade. It powers the ``scripted``
status: pick it for a role and the behavior director runs your script in a loop
of your own making.

SAFETY: the dashboard is single-user behind Authentik OIDC, so scripts are
*trusted-author* code. ``exec`` runs with ``__builtins__`` restricted to a safe
allowlist (no ``import`` / ``open`` / ``eval`` / ``exec`` / file / network) as a
footgun-reducer -- it is NOT a hardened sandbox. Don't expose this to untrusted
users without a real sandbox.

Cooperative cancellation: the runner cannot preempt a Python thread, so scripts
must yield through ``bot.wait(...)`` / ``bot.navigate(...)`` (which abort when
the status changes) and loop on ``while bot.running:``.
"""

from __future__ import annotations

import builtins
import json
import math
import os
import random
import threading
import time
import uuid

MAX_CODE = 20_000

_SAMPLE = """# Runs while this status is active. `bot` is your handle.
while bot.running:
    p = bot.position()
    bot.log(f"at {p['x']:.0f}, {p['z']:.0f}")
    bot.navigate(p['x'] + 10, p['z'])   # walk 10 blocks east
    bot.wait(3)
"""


class ScriptError(ValueError):
    """Invalid script (message is safe to show the user)."""


class _Stop(Exception):
    """Raised inside a script to unwind it when the status is cancelled."""


# -- the API exposed to scripts as `bot` -------------------------------------
class BotAPI:
    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def running(self) -> bool:
        """True while the script should keep going (not cancelled, in play)."""
        return not self._ctx.cancelled.is_set() and self._ctx.bot.state == "play"

    @property
    def stopping(self) -> bool:
        return self._ctx.cancelled.is_set()

    def _check(self):
        if self._ctx.cancelled.is_set():
            raise _Stop()

    def log(self, *parts):
        self._ctx.log(" ".join(str(p) for p in parts))

    def chat(self, message):
        self._check()
        self._ctx.client.chat(str(message))

    def look(self, yaw, pitch=0.0):
        self._check()
        self._ctx.client.look(float(yaw), float(pitch))

    def wait(self, seconds):
        """Sleep; aborts the script promptly if the status changes."""
        if not self._ctx.sleep(float(seconds)):
            raise _Stop()

    def position(self) -> dict:
        return self._ctx.client.get_position()

    def distance_to(self, x, z) -> float:
        p = self.position()
        return math.hypot(float(x) - p["x"], float(z) - p["z"])

    def navigate(self, x, z, timeout=30.0):
        """Pathfind to (x, z); blocks until arrival. Returns the final phase."""
        self._check()
        phase = self._ctx.navigate(float(x), float(z), timeout=float(timeout))
        if self._ctx.cancelled.is_set():
            raise _Stop()
        return phase

    def move_to(self, x, y, z):
        self._check()
        self._ctx.client.move_to(float(x), float(y), float(z))

    def block_at(self, x, y, z):
        world = getattr(self._ctx.client, "world", None)
        if world is None:
            return None
        return world.block_name_at(int(x), int(y), int(z))


_SAFE_NAMES = (
    "abs min max round range len int float str bool list dict tuple set frozenset "
    "enumerate zip sorted reversed sum map filter any all divmod pow isinstance "
    "repr ord chr bin hex format True False None").split()


def _script_globals(api: BotAPI) -> dict:
    safe = {n: getattr(builtins, n) for n in _SAFE_NAMES if hasattr(builtins, n)}
    safe["print"] = lambda *a, **k: api.log(*a)
    return {"__builtins__": safe, "bot": api, "math": math, "random": random}


# -- persistence store -------------------------------------------------------
class ScriptStore:
    """CRUD + JSON persistence for scripts (mirrors MacroEngine's storage)."""

    def __init__(self, path: str):
        self._path = path
        self._scripts: dict[str, dict] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                for s in json.load(fh).get("scripts", []):
                    self._scripts[s["id"]] = s
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"scripts": list(self._scripts.values())}, fh, indent=2)
        os.replace(tmp, self._path)

    def list(self) -> list:
        return list(self._scripts.values())

    def get(self, script_id: str) -> dict | None:
        return self._scripts.get(script_id)

    def _validate(self, data: dict) -> tuple[str, str]:
        if not isinstance(data, dict):
            raise ScriptError("script must be an object")
        name = str(data.get("name", "")).strip()
        if not name:
            raise ScriptError("script needs a name")
        code = data.get("code", "")
        if not isinstance(code, str) or not code.strip():
            raise ScriptError("script needs code")
        if len(code) > MAX_CODE:
            raise ScriptError(f"script too long (max {MAX_CODE} chars)")
        try:
            compile(code, f"<script:{name}>", "exec")
        except SyntaxError as exc:
            raise ScriptError(f"syntax error: {exc.msg} (line {exc.lineno})")
        return name, code

    def create(self, data: dict) -> dict:
        name, code = self._validate(data)
        script = {"id": uuid.uuid4().hex[:12], "name": name, "code": code,
                  "created": time.time(), "updated": time.time()}
        self._scripts[script["id"]] = script
        self._save()
        return script

    def update(self, script_id: str, data: dict) -> dict:
        script = self._scripts.get(script_id)
        if script is None:
            raise ScriptError("no such script")
        name, code = self._validate(data)
        script.update(name=name, code=code, updated=time.time())
        self._save()
        return script

    def delete(self, script_id: str) -> bool:
        if self._scripts.pop(script_id, None) is None:
            return False
        self._save()
        return True

    # -- execution (called by the behavior director's script behavior) ------
    def run_in_context(self, ctx):
        """Execute the script referenced by ctx.script_id, blocking until it
        finishes or the behavior is cancelled. Runs on the behavior arm thread."""
        script = self.get(ctx.script_id) if ctx.script_id else None
        if script is None:
            ctx.log("script not found")
            while ctx.sleep(1.0):
                pass
            return
        api = BotAPI(ctx)
        try:
            exec(compile(script["code"], f"<script:{script['name']}>", "exec"),
                 _script_globals(api))
        except _Stop:
            pass  # cancelled -- normal stop
        except Exception as exc:  # noqa: BLE001 - surface script errors to the UI
            ctx.emit("error", f"{type(exc).__name__}: {exc}")


SAMPLE_CODE = _SAMPLE
