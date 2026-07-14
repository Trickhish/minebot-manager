"""Declarative macro engine for the dashboard.

A macro is a named, persisted list of safe predefined steps run against a bot.
No arbitrary code -- only the actions in ACTIONS below, each validated on save
and re-checked at run time. Macros run in their own daemon thread so a blocking
step (wait, move_to) never stalls the event loop or another bot.

Triggers:
  * manual   -- run once via the API/UI.
  * interval -- re-run every N seconds while the bot is in play (until disarmed).
  * event    -- run when a bot event fires (chat matching a pattern, spawn,
                ready, disconnect).

Definitions persist to a JSON file; per-bot arming/runs are in-memory (bots are
in-memory too, so arming a macro on a bot that no longer exists is meaningless).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

# action name -> required field names (all besides these are rejected)
ACTIONS = {
    "chat": ("message",),
    "wait": ("seconds",),
    "look": ("yaw", "pitch"),
    "move_to": ("x", "y", "z"),
    "select_hotbar": ("slot",),
    "creative_give": ("slot", "item", "count"),
}
EVENTS = ("chat", "spawn", "ready", "disconnect")
MAX_STEPS = 200
MAX_LOOP = 10000


class MacroError(ValueError):
    """Invalid macro definition (message is safe to show the user)."""


# -- validation --------------------------------------------------------------
def validate_macro(data: dict) -> dict:
    """Validate + normalize a macro definition. Raises MacroError on bad input."""
    if not isinstance(data, dict):
        raise MacroError("macro must be an object")
    name = str(data.get("name", "")).strip()
    if not name:
        raise MacroError("macro needs a name")

    steps_in = data.get("steps") or []
    if not isinstance(steps_in, list) or not steps_in:
        raise MacroError("macro needs at least one step")
    if len(steps_in) > MAX_STEPS:
        raise MacroError(f"too many steps (max {MAX_STEPS})")
    steps = [_validate_step(i, s) for i, s in enumerate(steps_in)]

    loop = data.get("loop", 1)
    if not isinstance(loop, int) or not (1 <= loop <= MAX_LOOP):
        raise MacroError(f"loop must be an integer 1..{MAX_LOOP}")

    trigger = _validate_trigger(data.get("trigger") or {"type": "manual"})
    return {"name": name, "steps": steps, "loop": loop, "trigger": trigger}


def _validate_step(idx: int, step: dict) -> dict:
    if not isinstance(step, dict):
        raise MacroError(f"step {idx + 1}: must be an object")
    action = step.get("action")
    if action not in ACTIONS:
        raise MacroError(f"step {idx + 1}: unknown action {action!r}")
    out = {"action": action}
    for field in ACTIONS[action]:
        if field not in step:
            raise MacroError(f"step {idx + 1} ({action}): missing {field!r}")
        out[field] = step[field]
    _check_step_values(idx, out)
    return out


def _check_step_values(idx: int, s: dict) -> None:
    a = s["action"]
    where = f"step {idx + 1} ({a})"
    if a == "chat":
        if not isinstance(s["message"], str) or not s["message"]:
            raise MacroError(f"{where}: message must be non-empty text")
    elif a == "wait":
        _num(s, "seconds", where, lo=0, hi=3600)
    elif a == "look":
        _num(s, "yaw", where); _num(s, "pitch", where, lo=-90, hi=90)
    elif a == "move_to":
        for f in ("x", "y", "z"):
            _num(s, f, where)
    elif a == "select_hotbar":
        _int(s, "slot", where, lo=0, hi=8)
    elif a == "creative_give":
        _int(s, "slot", where, lo=0, hi=127)
        if not isinstance(s["item"], str) or not s["item"]:
            raise MacroError(f"{where}: item must be a name")
        _int(s, "count", where, lo=1, hi=64)


def _num(s, field, where, lo=None, hi=None):
    v = s[field]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise MacroError(f"{where}: {field} must be a number")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise MacroError(f"{where}: {field} out of range")
    s[field] = float(v)


def _int(s, field, where, lo=None, hi=None):
    v = s[field]
    if isinstance(v, bool) or not isinstance(v, int):
        raise MacroError(f"{where}: {field} must be an integer")
    if lo is not None and v < lo or hi is not None and v > hi:
        raise MacroError(f"{where}: {field} out of range")


def _validate_trigger(t: dict) -> dict:
    ttype = t.get("type", "manual")
    if ttype == "manual":
        return {"type": "manual"}
    if ttype == "interval":
        secs = t.get("interval_seconds")
        if not isinstance(secs, (int, float)) or not (1 <= secs <= 86400):
            raise MacroError("interval_seconds must be 1..86400")
        return {"type": "interval", "interval_seconds": float(secs)}
    if ttype == "event":
        ev = t.get("event")
        if ev not in EVENTS:
            raise MacroError(f"event must be one of {EVENTS}")
        out = {"type": "event", "event": ev}
        if ev == "chat":
            pattern = t.get("pattern", "")
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise MacroError(f"invalid chat pattern: {exc}")
            out["pattern"] = pattern
        return out
    raise MacroError(f"unknown trigger type {ttype!r}")


# -- one execution -----------------------------------------------------------
class MacroRun:
    def __init__(self, engine, bot, macro: dict, source: str):
        self.id = uuid.uuid4().hex[:12]
        self.engine = engine
        self.bot = bot
        self.macro = macro
        self.source = source  # "manual" | "interval" | "event:<ev>"
        self._cancel = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"macro-{self.id}")

    def start(self):
        self.thread.start()

    def cancel(self):
        self._cancel.set()

    def _emit(self, phase: str, detail=None):
        self.bot._emit_threadsafe("macro", {
            "run_id": self.id, "macro": self.macro["name"],
            "source": self.source, "phase": phase, "detail": detail})

    def _run(self):
        self._emit("started")
        try:
            if self.bot.state != "play":
                raise RuntimeError(f"bot not in play (state={self.bot.state})")
            for _ in range(self.macro.get("loop", 1)):
                for i, step in enumerate(self.macro["steps"]):
                    if self._cancel.is_set() or not self.bot.client.running:
                        self._emit("cancelled")
                        return
                    self._exec(step)
            self._emit("finished")
        except Exception as exc:  # noqa: BLE001 - report, don't crash the thread
            self._emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            self.engine._run_done(self)

    def _exec(self, step: dict):
        a = step["action"]
        c = self.bot.client
        if a == "chat":
            c.chat(step["message"])
        elif a == "wait":
            self._cancel.wait(step["seconds"])
        elif a == "look":
            c.look(step["yaw"], step["pitch"])
        elif a == "move_to":
            c.move_to(step["x"], step["y"], step["z"])
        elif a == "select_hotbar":
            c.select_hotbar(int(step["slot"]))
        elif a == "creative_give":
            c.creative_give(int(step["slot"]), step["item"], int(step["count"]))


# -- per-bot arming of a triggered macro -------------------------------------
class _IntervalArm:
    def __init__(self, engine, bot, macro):
        self.engine, self.bot, self.macro = engine, bot, macro
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        interval = self.macro["trigger"]["interval_seconds"]
        while not self._stop.wait(interval):
            if self.bot.state == "play" and self.bot.client.running:
                self.engine._start_run(self.bot, self.macro, "interval",
                                       dedupe_key=(self.bot.id, self.macro["id"]))

    def disarm(self):
        self._stop.set()


class _EventArm:
    def __init__(self, engine, bot, macro):
        self.engine, self.bot, self.macro = engine, bot, macro
        self.event = macro["trigger"]["event"]
        pat = macro["trigger"].get("pattern") or ""
        self._pattern = re.compile(pat) if pat else None
        bot.client.on(self.event, self._handler)

    def _handler(self, *args):
        if self.event == "chat" and self._pattern is not None:
            # chat handler args: (name, params, raw)
            params = args[1] if len(args) > 1 else None
            if not self._pattern.search(_chat_text(params)):
                return
        self.engine._start_run(self.bot, self.macro, f"event:{self.event}")

    def disarm(self):
        self.bot.client.off(self.event, self._handler)


def _chat_text(params) -> str:
    if params is None:
        return ""
    if isinstance(params, str):
        return params
    if isinstance(params, dict):
        for k in ("message", "content", "plainMessage", "unsignedContent"):
            v = params.get(k)
            if isinstance(v, str):
                return v
        return json.dumps(params, default=str)
    return str(params)


# -- engine ------------------------------------------------------------------
class MacroEngine:
    def __init__(self, manager, path: str):
        self.manager = manager
        self.path = path
        self._macros: dict[str, dict] = {}
        self._runs: dict[str, MacroRun] = {}
        self._arms: dict[tuple, object] = {}  # (bot_id, macro_id) -> arm
        self._lock = threading.Lock()
        self._load()

    # -- persistence ----
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    for m in json.load(fh):
                        self._macros[m["id"]] = m
            except (OSError, ValueError):
                pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(list(self._macros.values()), fh, indent=2)
        os.replace(tmp, self.path)

    # -- CRUD ----
    def list(self) -> list:
        return list(self._macros.values())

    def get(self, macro_id: str):
        return self._macros.get(macro_id)

    def create(self, data: dict) -> dict:
        macro = validate_macro(data)
        macro["id"] = uuid.uuid4().hex[:12]
        self._macros[macro["id"]] = macro
        self._save()
        return macro

    def update(self, macro_id: str, data: dict) -> dict:
        if macro_id not in self._macros:
            raise KeyError(macro_id)
        macro = validate_macro(data)
        macro["id"] = macro_id
        self._macros[macro_id] = macro
        self._save()
        self._disarm_all_for_macro(macro_id)  # definition changed; drop stale arms
        return macro

    def delete(self, macro_id: str) -> bool:
        if self._macros.pop(macro_id, None) is None:
            return False
        self._save()
        self._disarm_all_for_macro(macro_id)
        return True

    # -- running ----
    def run_now(self, bot_id: str, macro_id: str) -> str:
        bot = self._require_bot(bot_id)
        macro = self._require_macro(macro_id)
        run = self._start_run(bot, macro, "manual")
        if run is None:
            raise RuntimeError("could not start run")
        return run.id

    def _start_run(self, bot, macro, source, dedupe_key=None):
        with self._lock:
            if dedupe_key is not None and any(
                    r for r in self._runs.values()
                    if getattr(r, "_dedupe", None) == dedupe_key):
                return None  # previous interval run of this macro still going
            run = MacroRun(self, bot, macro, source)
            run._dedupe = dedupe_key
            self._runs[run.id] = run
        run.start()
        return run

    def _run_done(self, run):
        with self._lock:
            self._runs.pop(run.id, None)

    def cancel_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None:
            return False
        run.cancel()
        return True

    # -- arming ----
    def arm(self, bot_id: str, macro_id: str):
        bot = self._require_bot(bot_id)
        macro = self._require_macro(macro_id)
        ttype = macro["trigger"]["type"]
        if ttype == "manual":
            raise MacroError("manual macros can't be armed; use run")
        key = (bot_id, macro_id)
        self.disarm(bot_id, macro_id)
        self._arms[key] = (_IntervalArm(self, bot, macro) if ttype == "interval"
                           else _EventArm(self, bot, macro))

    def disarm(self, bot_id: str, macro_id: str) -> bool:
        arm = self._arms.pop((bot_id, macro_id), None)
        if arm is None:
            return False
        arm.disarm()
        return True

    def _disarm_all_for_macro(self, macro_id: str):
        for (bid, mid) in [k for k in self._arms if k[1] == macro_id]:
            self.disarm(bid, mid)

    def bot_status(self, bot_id: str) -> dict:
        armed = [mid for (bid, mid) in self._arms if bid == bot_id]
        running = [{"run_id": r.id, "macro_id": r.macro.get("id"),
                    "macro": r.macro["name"], "source": r.source}
                   for r in self._runs.values() if r.bot.id == bot_id]
        return {"armed": armed, "running": running}

    # -- helpers ----
    def _require_bot(self, bot_id):
        bot = self.manager.get(bot_id)
        if bot is None:
            raise KeyError(f"no such bot {bot_id}")
        return bot

    def _require_macro(self, macro_id):
        macro = self._macros.get(macro_id)
        if macro is None:
            raise KeyError(f"no such macro {macro_id}")
        return macro
