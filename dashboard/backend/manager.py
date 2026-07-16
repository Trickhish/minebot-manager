"""Bot supervision: run each `mcbot.Client` in its own thread and bridge its
(thread-side) events onto the FastAPI asyncio event loop.

The one delicate part of the whole dashboard lives here. `Client.connect()`
blocks and its `bot.on(...)` handlers fire on the client's own pump thread, so
they must never touch asyncio objects directly. Instead every handler hands its
event to the event loop with `loop.call_soon_threadsafe(...)`, which is the
only safe way to cross from an arbitrary thread into the loop. From there the
event is appended to a bounded history ring and pushed to each subscriber's
asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Optional

from mcbot.client import (
    Client,
    Disconnected,
    OnlineModeRequired,
    UnsupportedProtocol,
)

HISTORY_SIZE = 200

# Auto-reconnect backoff: exponential from BASE, capped at MAX (seconds).
RECONNECT_BASE_DELAY = 3.0
RECONNECT_MAX_DELAY = 60.0
SERVER_AUTH_PROMPT_WINDOW = 120.0
SERVER_AUTH_PROBE_DELAY = 3.0
# Outcomes that will never succeed on retry -> don't auto-reconnect.
TERMINAL_OUTCOMES = {"online_mode_required", "unsupported_protocol"}

# Which client events we mirror to the dashboard, and how each maps to a
# dashboard event `type`. Chat/state/position are the useful monitor signals
# for v1; the rest (block changes, inventory) are deferred.
_LIFECYCLE_STATE = {
    "handshaking": "connecting",
    "login": "connecting",
    "configuration": "configuring",
    "play": "play",
}


class ManagedBot:
    """A single bot: its Client, worker thread, status, and event fan-out."""

    def __init__(self, req, loop: asyncio.AbstractEventLoop,
                 bot_id: Optional[str] = None):
        self.id = bot_id or uuid.uuid4().hex[:12]
        self.loop = loop
        self.username = req.username
        self.host = req.host
        self.port = req.port
        self.requested_version = req.version
        self.version = req.version
        self.advertise_protocol = req.advertise_protocol
        self.auto_reconnect = getattr(req, "auto_reconnect", True)

        self.state = "created"
        self.position: Optional[dict] = None
        self.created_at = time.time()
        self.connected_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.reconnect_attempts = 0
        self._session_disconnect_reason: Optional[str] = None

        self._history: deque = deque(maxlen=HISTORY_SIZE)
        self._subscribers: set[asyncio.Queue] = set()
        self.control_owner = None
        self._thread: Optional[threading.Thread] = None
        # Set when the user stops/removes the bot: breaks the reconnect loop
        # and wakes it out of any backoff sleep.
        self._stopping = False
        self._wake = threading.Event()
        self._server_auth_lock = threading.Lock()
        self._server_auth_credentials = None
        self._server_auth_pending = None
        self._server_auth_started_at = time.monotonic()
        self._server_auth_login_attempted = False
        self._server_auth_register_attempted = False
        self._server_auth_probe_attempted = False
        self._server_auth_session = 0
        self._server_auth_detected = False
        self._server_auth_complete = False

        self.client = self._new_client()

    # -- persistence --------------------------------------------------------
    def spec(self) -> dict:
        """The minimal set of fields needed to recreate this bot after a
        bot-host restart (see BotManager persistence)."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "version": self.requested_version,
            "advertise_protocol": self.advertise_protocol,
            "auto_reconnect": self.auto_reconnect,
        }

    # -- status -------------------------------------------------------------
    def status(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "state": self.state,
            "position": self.position,
            "created_at": self.created_at,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "auto_reconnect": self.auto_reconnect,
            "reconnect_attempts": self.reconnect_attempts,
        }

    def _new_client(self) -> Client:
        """Build a fresh Client and wire its handlers.

        A dropped `Client` has consumed its socket/protocol state, so each
        (re)connect attempt uses a brand-new instance.
        """
        client = Client(
            self.host, port=self.port, username=self.username,
            version=self.requested_version,
            advertise_protocol=self.advertise_protocol)
        self.client = client
        self._register_handlers()
        return client

    # -- event fan-out (called on the loop thread only) ---------------------
    def _emit(self, type_: str, data=None) -> None:
        """Record + fan out an event. MUST run on the loop thread."""
        event = {"type": type_, "bot_id": self.id, "ts": time.time(), "data": data}
        # High-frequency UI snapshots belong on the live stream, but retaining
        # them would evict useful lifecycle and chat history.
        if type_ not in ("move", "action_bar", "stats", "inventory"):
            self._history.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a slow subscriber drops events rather than blocking others

    def _emit_threadsafe(self, type_: str, data=None) -> None:
        """Hand an event from the bot's pump thread onto the loop thread."""
        self.loop.call_soon_threadsafe(self._emit, type_, data)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def history(self) -> list:
        return list(self._history)

    # -- client event handlers (run on the bot's pump thread) ---------------
    def _register_handlers(self) -> None:
        bot = self.client

        @bot.on("state")
        def _state(state):
            mapped = _LIFECYCLE_STATE.get(state, state)
            self._set_state_threadsafe(mapped)
            if state == "play":
                self._schedule_server_auth_probe()

        @bot.on("ready")
        def _ready():
            # A good session resets the backoff so a later drop retries fast.
            self.loop.call_soon_threadsafe(
                lambda: setattr(self, "reconnect_attempts", 0))
            self._emit_threadsafe("ready")

        @bot.on("spawn")
        def _spawn(position):
            self._on_position_threadsafe(position, "spawn")

        @bot.on("move")
        def _move(position):
            self._on_position_threadsafe(position, "move")

        @bot.on("chat")
        def _chat(name, params, raw):
            event_type = "action_bar" if _is_action_bar(params) else "chat"
            self._emit_threadsafe(
                event_type, {"packet": name, "params": _safe(params)})
            self._handle_server_auth_message(name, params)

        @bot.on("disconnect")
        def _disconnect(reason):
            self._session_disconnect_reason = _format_reason(reason)
            self._emit_threadsafe("disconnect", {"reason": _safe(reason)})

        @bot.on("authentication")
        def _authentication(info):
            self._emit_threadsafe("auth", _safe(info))

        @bot.on("protocol")
        def _protocol(info):
            def apply():
                self.version = info["version"]
                self.advertise_protocol = info["protocol"]
                self._emit("protocol", _safe(info))
            self.loop.call_soon_threadsafe(apply)

        @bot.on("player_state")
        def _stats(snapshot):
            self._emit_threadsafe("stats", _safe(snapshot))

        def _inv(*_a):
            self._emit_threadsafe("inventory", self.inventory_snapshot())
        for _evt in ("window_items", "set_slot", "held_item_slot",
                     "open_window", "close_window"):
            bot.on(_evt, _inv)

    # -- offline-server /login ---------------------------------------------
    def set_server_auth(self, password: str, auto_register: bool = False) -> None:
        """Keep browser-supplied credentials in memory for this process only."""
        with self._server_auth_lock:
            self._server_auth_credentials = {
                "password": password,
                "auto_register": bool(auto_register),
            }
            self._server_auth_login_attempted = False
            self._server_auth_register_attempted = False
            self._server_auth_complete = False
        self._try_server_auth()

    def clear_server_auth(self) -> None:
        with self._server_auth_lock:
            self._server_auth_credentials = None

    def _handle_server_auth_message(self, packet_name, params) -> None:
        # Signed/player-authored chat must never trigger a credential command.
        if packet_name in ("player_chat", "profileless_chat"):
            return
        text = _chat_text(params)
        if not text:
            return
        detected = False
        with self._server_auth_lock:
            if _is_server_auth_success(text):
                self._server_auth_complete = True
                self._server_auth_pending = None
                return
            kind = _server_auth_prompt(text)
            if kind is None:
                return
            if time.monotonic() - self._server_auth_started_at > SERVER_AUTH_PROMPT_WINDOW:
                return
            if not self._server_auth_detected:
                self._server_auth_detected = True
                detected = True
            # A usage response to our argument-free probe only proves that the
            # plugin exists. Try login first; an unregistered account will then
            # receive a real registration prompt and follow the opt-in path.
            if (kind == "register" and self._server_auth_probe_attempted
                    and _is_register_usage(text)):
                kind = "login"
            self._server_auth_pending = {"kind": kind, "text": text}
        if detected:
            self._emit_threadsafe("server_auth", {"action": "detected"})
        self._try_server_auth()

    def _schedule_server_auth_probe(self) -> None:
        """Probe silent offline auth plugins after giving prompts time to arrive."""
        with self._server_auth_lock:
            session = self._server_auth_session
        timer = threading.Timer(
            SERVER_AUTH_PROBE_DELAY, self._probe_server_auth, args=(session,))
        timer.daemon = True
        timer.start()

    def _probe_server_auth(self, session: int) -> None:
        with self._server_auth_lock:
            if (session != self._server_auth_session
                    or self._server_auth_probe_attempted
                    or self._server_auth_pending
                    or self._server_auth_complete
                    or self.client.online_mode is not False
                    or self.client.state != "play"):
                return
            self._server_auth_probe_attempted = True

        try:
            self.client.chat("/register")
        except Exception as exc:  # noqa: BLE001 - the probe contains no secret
            self._emit_threadsafe("error", {
                "kind": "server_auth",
                "message": f"server login-system probe failed: {exc}",
            })
        else:
            self._emit_threadsafe("server_auth", {"action": "probe_sent"})

    def _try_server_auth(self) -> None:
        with self._server_auth_lock:
            credentials = self._server_auth_credentials
            pending = self._server_auth_pending
            if (not credentials or not pending or self._server_auth_complete
                    or self.client.online_mode is not False
                    or self.client.state != "play"):
                return
            kind = pending["kind"]
            if kind == "register" and not credentials["auto_register"]:
                return
            if kind == "login" and self._server_auth_login_attempted:
                return
            if kind == "register" and self._server_auth_register_attempted:
                return
            if kind == "login":
                self._server_auth_login_attempted = True
                command = f"/login {credentials['password']}"
            else:
                self._server_auth_register_attempted = True
                command = _register_command(pending["text"], credentials["password"])

        try:
            self.client.chat(command)
        except Exception as exc:  # noqa: BLE001 - report without exposing command
            self._emit_threadsafe("error", {
                "kind": "server_auth",
                "message": f"automatic server {kind} failed: {exc}",
            })
        else:
            self._emit_threadsafe("server_auth", {"action": f"{kind}_sent"})

    # -- snapshots ----------------------------------------------------------
    def player_state(self) -> dict:
        return _safe(self.client.player_state())

    def inventory_snapshot(self) -> dict:
        inv = self.client.inventory
        slots = inv.windows.get(0, {})
        return {
            "window_id": inv.window_id,
            "held_slot": inv.held_slot,
            "held_index": inv.HOTBAR_START + inv.held_slot,
            "cursor": _safe(inv.cursor_item),
            "open_window": _safe(inv.open_window_info),
            "slots": {str(i): _safe(item) for i, item in slots.items() if item is not None},
        }

    def _set_state_threadsafe(self, state: str) -> None:
        def apply():
            self.state = state
            if state == "play" and self.connected_at is None:
                self.connected_at = time.time()
            self._emit("state", {"state": state})
        self.loop.call_soon_threadsafe(apply)

    def _on_position_threadsafe(self, position: dict, type_: str) -> None:
        pos = dict(position)

        def apply():
            self.position = pos
            self._emit(type_, pos)
        self.loop.call_soon_threadsafe(apply)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"bot-{self.id}")
        self._thread.start()

    def reconnect(self) -> bool:
        """(Re)start the connection of a stopped/disconnected bot. Returns
        False (no-op) if the connection thread is still running."""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stopping = False
        self._wake.clear()
        self.reconnect_attempts = 0
        self.last_error = None
        self._new_client()  # a stopped Client is spent; wire up a fresh one
        self.start()
        return True

    def _run(self) -> None:
        """Connect, and (unless stopped) reconnect with exponential backoff."""
        while not self._stopping:
            self._reset_session()
            outcome, message = self._run_once()

            if self._stopping:
                break
            if not self.auto_reconnect or outcome in TERMINAL_OUTCOMES:
                self._finalize(outcome, message)
                return

            self.reconnect_attempts += 1
            delay = min(
                RECONNECT_BASE_DELAY * (2 ** (self.reconnect_attempts - 1)),
                RECONNECT_MAX_DELAY)
            self._notify_reconnect(outcome, message, delay, self.reconnect_attempts)

            # Interruptible backoff: stop() sets the event and we bail out.
            if self._wake.wait(delay):
                break
            self._new_client()

        self._finalize("disconnected", "stopped")

    def _run_once(self):
        """One blocking connect. Returns (outcome, message)."""
        try:
            self.client.connect()  # blocks, pumping packets
        except OnlineModeRequired as exc:
            return "online_mode_required", str(exc)
        except UnsupportedProtocol as exc:
            return "unsupported_protocol", str(exc)
        except (Disconnected, ConnectionError, OSError) as exc:
            return "disconnected", _connection_failure_message(
                self.client.state, exc)
        except Exception as exc:  # noqa: BLE001 - surface anything else, don't die silently
            return "error", _connection_failure_message(self.client.state, exc)
        else:
            message = self._session_disconnect_reason
            if not message:
                message = _connection_failure_message(self.client.state)
            return "disconnected", message

    def _reset_session(self) -> None:
        """Clear per-connection state at the start of each attempt."""
        self._session_disconnect_reason = None
        with self._server_auth_lock:
            self._server_auth_session += 1
            self._server_auth_pending = None
            self._server_auth_started_at = time.monotonic()
            self._server_auth_login_attempted = False
            self._server_auth_register_attempted = False
            self._server_auth_probe_attempted = False
            self._server_auth_detected = False
            self._server_auth_complete = False

        def apply():
            self.connected_at = None
            self.state = "connecting"
            self._emit("state", {"state": "connecting"})
        self.loop.call_soon_threadsafe(apply)

    def _notify_reconnect(self, outcome, message, delay, attempt) -> None:
        def apply():
            self.state = "reconnecting"
            if message:
                self.last_error = message
            if message and not self._session_disconnect_reason:
                self._emit("error", {"kind": outcome, "message": message})
            self._emit("state", {
                "state": "reconnecting",
                "attempt": attempt,
                "retry_in": round(delay, 1),
                "reason": message,
            })
        self.loop.call_soon_threadsafe(apply)

    def _finalize(self, outcome, message) -> None:
        def apply():
            self.state = "error" if outcome in ("error", "online_mode_required") else "disconnected"
            if message and message != "stopped":
                self.last_error = message
            if message and message != "stopped" and not self._session_disconnect_reason:
                self._emit("error", {"kind": outcome, "message": message})
            self._emit("state", {"state": self.state})
        self.loop.call_soon_threadsafe(apply)

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        try:
            self.client.stop()
        except Exception:  # noqa: BLE001 - stop() is best-effort
            pass


class BotManager:
    def __init__(self, store_path: Optional[str] = None,
                 request_model=None):
        self._bots: dict[str, ManagedBot] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # Where the roster is persisted so bots survive a bot-host restart.
        # `request_model` reconstructs a validated request object from a
        # stored spec dict (the dashboard passes CreateBotRequest).
        self._store = store_path
        self._request_model = request_model

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def create(self, req, bot_id: Optional[str] = None,
               persist: bool = True) -> ManagedBot:
        assert self.loop is not None, "event loop not bound"
        bot = ManagedBot(req, self.loop, bot_id=bot_id)
        self._bots[bot.id] = bot
        bot.start()
        if persist:
            self._save()
        return bot

    def get(self, bot_id: str) -> Optional[ManagedBot]:
        return self._bots.get(bot_id)

    def list(self) -> list[ManagedBot]:
        return list(self._bots.values())

    def remove(self, bot_id: str) -> bool:
        bot = self._bots.pop(bot_id, None)
        if bot is None:
            return False
        bot.stop()
        self._save()
        return True

    def shutdown(self) -> None:
        # Just stop the running threads; leave the persisted roster intact so
        # a restart brings the same bots back.
        for bot in self._bots.values():
            bot.stop()
        self._bots.clear()

    # -- persistence --------------------------------------------------------
    def restore(self) -> int:
        """Recreate persisted bots (called once at startup, after bind_loop).
        Their auto-reconnect loop then re-establishes the live sessions."""
        if not self._store or not os.path.exists(self._store):
            return 0
        try:
            with open(self._store, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return 0
        n = 0
        for entry in saved.get("bots", []):
            spec, bot_id = entry.get("spec"), entry.get("id")
            if not spec:
                continue
            try:
                # Rosters created before automatic detection persisted the UI
                # selection. Migrate them so restored bots detect as well.
                spec = {**spec, "version": "auto", "advertise_protocol": None}
                req = self._request_model(**spec) if self._request_model else _Spec(spec)
                self.create(req, bot_id=bot_id, persist=False)
                n += 1
            except Exception:  # noqa: BLE001 - skip a corrupt/invalid entry
                continue
        return n

    def _save(self) -> None:
        if not self._store:
            return
        data = {"bots": [{"id": b.id, "spec": b.spec()} for b in self._bots.values()]}
        try:
            os.makedirs(os.path.dirname(self._store), exist_ok=True)
            tmp = f"{self._store}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._store)
        except OSError:
            pass  # best-effort; a failed save shouldn't break bot control


class _Spec:
    """Fallback attribute-access wrapper when no request model is supplied."""
    def __init__(self, d: dict):
        self.__dict__.update(d)


def _safe(value):
    """Coerce arbitrary decoded packet params into JSON-serializable form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _format_reason(reason) -> str:
    """Turn a decoded server disconnect payload into a console message."""
    value = _safe(reason)
    if value is None:
        return "server disconnected without a reason"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("reason", "message", "text"):
            detail = value.get(key)
            if isinstance(detail, str) and detail:
                return detail
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_action_bar(params) -> bool:
    if not isinstance(params, dict):
        return False
    position = params.get("position")
    return (
        params.get("isActionBar") is True
        or params.get("overlay") is True
        or position in (2, "action_bar", "game_info")
    )


def _chat_text(params) -> str:
    if isinstance(params, str):
        return _strip_minecraft_formatting(params)
    if not isinstance(params, dict):
        return ""
    value = next((params[key] for key in (
        "message", "content", "plainMessage", "unsignedContent", "text")
        if params.get(key) is not None), "")
    return re.sub(r"\s+", " ", _minecraft_text(value)).strip()


def _minecraft_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _strip_minecraft_formatting(value)
    if isinstance(value, (list, tuple)):
        return "".join(_minecraft_text(part) for part in value)
    if not isinstance(value, dict):
        return str(value)
    own = value.get("text") if isinstance(value.get("text"), str) else ""
    translated = (
        value.get("translate")
        if not own and isinstance(value.get("translate"), str) else "")
    return own + translated + _minecraft_text(value.get("extra"))


def _strip_minecraft_formatting(value: str) -> str:
    return re.sub(r"§[0-9a-fk-or]", "", value, flags=re.IGNORECASE)


def _server_auth_prompt(text: str) -> str | None:
    if re.search(
            r"already\s+(?:been\s+)?registered|"
            r"account\s+(?:is\s+)?already\s+registered",
            text, re.IGNORECASE):
        return "login"
    if re.search(
            r"/register\b|please\s+register\b|"
            r"you\s+(?:must|need to)\s+register\b|not\s+registered\b",
            text, re.IGNORECASE):
        return "register"
    if re.search(
            r"/login\b|please\s+log\s*in\b|please\s+login\b|"
            r"you\s+(?:must|need to)\s+log\s*in\b|authentication\s+required",
            text, re.IGNORECASE):
        return "login"
    return None


def _is_server_auth_success(text: str) -> bool:
    return bool(re.search(
        r"successfully\s+(?:logged\s*in|registered|authenticated)|"
        r"(?:login|registration|authentication)\s+successful|"
        r"you\s+(?:are|have been)\s+(?:now\s+)?"
        r"(?:logged\s*in|registered|authenticated)|already\s+logged\s*in",
        text, re.IGNORECASE))


def _is_register_usage(text: str) -> bool:
    return bool(re.search(r"\busage\b[^\r\n]*/register\b", text, re.IGNORECASE))


def _register_command(prompt: str, password: str) -> str:
    usage = re.search(r"/register\b([^\r\n]*)", prompt, re.IGNORECASE)
    placeholders = re.findall(
        r"<[^>]+>|\[[^\]]+\]", usage.group(1) if usage else "")
    if len(placeholders) == 1:
        return f"/register {password}"
    return f"/register {password} {password}"


def _connection_failure_message(state: str, exc=None) -> str:
    """Describe where a connection failed in Minecraft protocol terms."""
    phase = {
        "handshaking": "Minecraft handshake failed before login",
        "login": "Minecraft login failed before the bot entered the world",
        "configuration": (
            "Minecraft login configuration failed before the bot entered the world"),
        "play": "Minecraft connection was lost after the bot entered the world",
    }.get(state, f"Minecraft connection failed during {state}")
    if exc is None:
        detail = "connection ended without a server reason"
    else:
        detail = str(exc).strip() or type(exc).__name__
    return f"{phase}: {detail}"
