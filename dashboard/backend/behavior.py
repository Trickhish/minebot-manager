"""Autonomous behavior director.

Turns a bot's (role, status) into a running behavior. One cancellable daemon
thread per active bot, mirroring the macro engine's per-bot arms
(``macros.py``). Setting a new role/status cancels the current behavior and
starts the new one; behaviors cooperatively check ``ctx.cancelled`` and pause
themselves while the bot is out of play or under first-person control, so
autonomous motion never fights a human driver.

Behaviors drive the bot only through the public ``Client`` API (navigate/chat/
look), so no framework changes are needed. WS ``behavior`` events (started /
log / error / stopped) and ``role`` state events are fanned out to the UI.
"""

from __future__ import annotations

import math
import random
import threading
import time

import roles


class BehaviorContext:
    """Handle passed to a behavior: the bot, cancellation, and safe helpers."""

    def __init__(self, director: "BehaviorDirector", bot, cancelled: threading.Event,
                 script_id: str | None = None):
        self.director = director
        self.bot = bot
        self.cancelled = cancelled
        self.script_id = script_id

    @property
    def client(self):
        return self.bot.client

    # -- signalling ---------------------------------------------------------
    def emit(self, phase: str, detail=None):
        self.bot._emit_threadsafe("behavior", {
            "role": self.bot.role, "status": self.bot.role_status,
            "phase": phase, "detail": detail})

    def log(self, message: str):
        self.emit("log", str(message))

    def sleep(self, seconds: float) -> bool:
        """Sleep, returning False if cancelled during the wait."""
        return not self.cancelled.wait(seconds)

    def blocked(self) -> bool:
        """Whether the bot can't act right now (not in play, or being driven)."""
        return self.bot.state != "play" or self.bot.control_owner is not None

    # -- movement -----------------------------------------------------------
    def navigate(self, x: float, z: float, timeout: float = 30.0) -> str | None:
        """Pathfind to (x, z) and block until it resolves. Returns the final
        navigation phase (arrived/stuck/no_path) or None if we bailed."""
        client = self.client
        gen = client.navigate_to(x, z)
        done = threading.Event()
        box = {"phase": None}

        def on_nav(info):
            if info.get("navigation_id") == gen and info.get("phase") in (
                    "arrived", "stuck", "no_path", "cancelled"):
                box["phase"] = info.get("phase")
                done.set()

        client.on("navigation", on_nav)
        try:
            deadline = time.monotonic() + timeout
            while not done.wait(0.2):
                if self.cancelled.is_set() or self.blocked() \
                        or time.monotonic() > deadline:
                    client.cancel_navigation()
                    return None
        finally:
            client.off("navigation", on_nav)
        return box["phase"]


# -- built-in behaviors ------------------------------------------------------
def behavior_idle(ctx: BehaviorContext):
    while ctx.sleep(0.5):
        pass


def behavior_placeholder(ctx: BehaviorContext):
    ctx.log("this status isn't implemented yet -- idling. Attach a custom "
            "script for now.")
    while ctx.sleep(1.0):
        pass


def behavior_explore(ctx: BehaviorContext):
    """Wander: repeatedly pathfind to a random nearby reachable point."""
    while not ctx.cancelled.is_set():
        if ctx.blocked():
            if not ctx.sleep(1.0):
                return
            continue
        pos = ctx.client.get_position()
        angle = random.uniform(0.0, 2.0 * math.pi)
        dist = random.uniform(8.0, 20.0)
        tx = pos["x"] + dist * math.cos(angle)
        tz = pos["z"] + dist * math.sin(angle)
        ctx.log(f"heading toward ({tx:.0f}, {tz:.0f})")
        ctx.navigate(tx, tz, timeout=25.0)
        if not ctx.sleep(random.uniform(0.6, 1.6)):
            return


def behavior_script(ctx: BehaviorContext):
    store = ctx.director.script_store
    if store is None or not ctx.script_id:
        ctx.log("no script attached to this status")
        while ctx.sleep(1.0):
            pass
        return
    store.run_in_context(ctx)  # blocks until the script finishes or is cancelled


BEHAVIORS = {
    "idle": behavior_idle,
    "placeholder": behavior_placeholder,
    "explore": behavior_explore,
    "script": behavior_script,
}


class _Arm:
    """A single running behavior on one bot."""

    def __init__(self, director: "BehaviorDirector", bot, fn, script_id=None):
        self.bot = bot
        self.cancelled = threading.Event()
        self.ctx = BehaviorContext(director, bot, self.cancelled, script_id)
        self._fn = fn
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"behavior-{bot.id}")

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
        try:
            self.bot.client.cancel_navigation()
        except Exception:  # noqa: BLE001 - client may be mid-reconnect
            pass

    def _run(self):
        self.ctx.emit("started")
        try:
            self._fn(self.ctx)
        except Exception as exc:  # noqa: BLE001 - a behavior must not crash us
            self.ctx.emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            self.ctx.emit("stopped")


class BehaviorDirector:
    """Owns the running behavior per bot and switches it on role/status change."""

    def __init__(self, manager):
        self.manager = manager
        self.script_store = None  # set by host once scripts are available
        self._arms: dict[str, _Arm] = {}
        self._lock = threading.Lock()

    def set_role(self, bot, role: str | None, status: str | None,
                 script_id: str | None = None) -> tuple[str, str]:
        role, status = roles.normalize(role, status)
        bot.role = role
        bot.role_status = status
        bot.script_id = script_id
        self.manager._save()
        self._restart(bot)
        bot._emit_threadsafe("role", {
            "role": role, "status": status, "script_id": script_id})
        return role, status

    def clear(self, bot_id: str):
        with self._lock:
            arm = self._arms.pop(bot_id, None)
        if arm is not None:
            arm.cancel()

    def resume_all(self):
        """Start behaviors for restored bots (called once after roster restore)."""
        for bot in self.manager.list():
            self._restart(bot)

    def _restart(self, bot):
        self.clear(bot.id)
        sdef = roles.status_def(bot.role, bot.role_status)
        name = sdef["behavior"] if sdef else "idle"
        if name == "idle":
            return  # idle needs no thread
        fn = BEHAVIORS.get(name, behavior_idle)
        arm = _Arm(self, bot, fn, script_id=getattr(bot, "script_id", None))
        with self._lock:
            self._arms[bot.id] = arm
        arm.start()
