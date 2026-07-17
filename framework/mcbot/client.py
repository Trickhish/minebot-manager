"""High-level client: the protocol state machine + an event API.

Ties together `Protocol` (what packets mean) and `Connection` (how bytes move)
and walks the login flow to reach the Play state, then pumps packets and
dispatches them as events. Version-aware: it handles both the classic
login->play path (<=1.20.1) and the login->configuration->play path (>=1.20.2).

Offline (unauthenticated) login is implemented, including servers that encrypt
transport without Microsoft authentication. If the server requests session
authentication, we surface that clearly instead of silently failing.

Usage:
    client = Client("mc.example.com", username="Bot")
    client.on("chat", lambda name, params, raw: print(params))
    client.connect()   # blocks, pumping packets
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import sys
import threading
import time as _time
from collections import defaultdict

from .blocks import get_block_table
from .buffer import BufferUnderrun
from .connection import Connection
from .inventory import Inventory, make_slot_value
from .items import get_item_table
from .pathfinding import find_path
from .protocol import (
    Protocol,
    available_protocols,
    latest_available_version,
    version_for_protocol,
)
from .world import World

# Versions whose chunk sections predate the modern (no cross-long bit packing)
# paletted-container format `chunk.py` parses -- world tracking is disabled
# for these rather than silently misreading chunk bytes.
_LEGACY_CHUNK_FORMAT_VERSIONS = {"1.8"}

# -- first-person control physics (vanilla-tuned) -----------------------------
# Player collision box: 0.6 wide (0.3 half-extent) x 1.8 tall.
_PLAYER_HALF_WIDTH = 0.3
_PLAYER_HEIGHT = 1.8
# Auto step-up height. Treated as full cubes, so 1.0 lets the bot walk up a
# single-block ledge (like stairs) without jumping, matching the old feel.
_STEP_HEIGHT = 1.0
# Vertical motion, in blocks/second (0.42 blocks/tick jump, 0.08 gravity, 0.98
# drag, all scaled from vanilla's 20 Hz tick).
_JUMP_VELOCITY = 8.4
_GRAVITY = 32.0
_VERTICAL_DRAG = 0.98

# Blocks the player can stand inside of (no horizontal/ground collision).
# We have no per-block collision data vendored, so this is a curated list of
# blocks that in vanilla have no (or negligible) collision box: fluids, air,
# and small non-solid decorations/flora.
_PASSABLE_EXACT = frozenset({
    # air & fluids
    "air", "cave_air", "void_air", "water", "lava", "bubble_column",
    # grass & ferns
    "short_grass", "grass", "tall_grass", "short_dry_grass", "tall_dry_grass",
    "fern", "large_fern", "dead_bush", "bush", "firefly_bush",
    "seagrass", "tall_seagrass",
    # vines, roots & lichen
    "vine", "weeping_vines", "weeping_vines_plant", "twisting_vines",
    "twisting_vines_plant", "cave_vines", "cave_vines_plant", "glow_lichen",
    "sculk_vein", "resin_clump", "hanging_roots", "nether_sprouts",
    "warped_roots", "crimson_roots",
    # canes, kelp, bamboo shoot
    "bamboo", "bamboo_sapling", "sugar_cane", "kelp", "kelp_plant",
    # mushrooms & fungi
    "brown_mushroom", "red_mushroom", "crimson_fungus", "warped_fungus",
    # flowers
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose",
    "torchflower", "red_tulip", "orange_tulip", "white_tulip", "pink_tulip",
    "sunflower", "lilac", "rose_bush", "peony", "spore_blossom",
    "chorus_flower", "chorus_plant", "cactus_flower", "pink_petals",
    "wildflowers", "leaf_litter", "closed_eyeblossom", "open_eyeblossom",
    # crops
    "wheat", "carrots", "potatoes", "beetroots", "nether_wart",
    "melon_stem", "pumpkin_stem", "attached_melon_stem",
    "attached_pumpkin_stem", "sweet_berry_bush", "pitcher_crop",
    "torchflower_crop", "cocoa",
    # amethyst buds (small collision -- treat as passable)
    "small_amethyst_bud", "medium_amethyst_bud", "large_amethyst_bud",
    "amethyst_cluster",
    # misc thin / non-colliding
    "lily_pad", "small_dripleaf", "cobweb", "fire", "soul_fire",
    "redstone_wire", "tripwire", "tripwire_hook", "lever", "ladder",
    "scaffolding", "snow", "structure_void", "light", "moss_carpet",
    "pale_moss_carpet", "frogspawn", "sculk_shrieker",
})

# Block-name suffixes that are always passable (whole families of decorations).
_PASSABLE_SUFFIXES = (
    "_sign", "_banner", "_sapling", "_torch", "_button", "_pressure_plate",
    "_rail", "_carpet", "_coral_fan", "_coral_wall_fan",
)


def _is_passable(name: str | None) -> bool:
    """Whether the player can occupy a block cell without colliding.

    Unknown/unloaded (``None``) cells are treated as passable so the bot never
    gets stuck against the edge of a chunk that hasn't streamed in yet.
    """
    if name is None or name in _PASSABLE_EXACT:
        return True
    return name.endswith(_PASSABLE_SUFFIXES)


def offline_uuid(username: str) -> str:
    """The UUID an offline-mode server derives for a username (name-based v3)."""
    md5 = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode()).digest())
    md5[6] = (md5[6] & 0x0F) | 0x30  # version 3
    md5[8] = (md5[8] & 0x3F) | 0x80  # RFC 4122 variant
    h = md5.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


_CHAT_PACKETS = ("chat", "system_chat", "player_chat", "profileless_chat")
_WORLD_PACKETS = ("map_chunk", "unload_chunk", "block_change", "multi_block_change")


class OnlineModeRequired(Exception):
    """Server demanded Microsoft authentication; offline login can't continue."""


class Disconnected(Exception):
    """Server closed the session or sent a disconnect packet."""


class UnsupportedProtocol(Exception):
    """The server protocol has no exact vendored packet schema."""


class Client:
    def __init__(self, host, port=25565, username="Bot", version="auto",
                 advertise_protocol=None):
        self.host = host
        self.port = port
        self.username = username
        self.requested_version = version
        initial_version = (
            latest_available_version() if version == "auto" else version)
        self.protocol = Protocol(initial_version)
        # What protocol number to claim in the handshake. Defaults to the
        # schema's own number, but can be bumped to talk to a server that is
        # slightly newer than the schema we decode with.
        self.advertise_protocol = advertise_protocol or self.protocol.protocol_version
        self.conn = Connection(host, port)
        self.state = "handshaking"
        self.uuid = offline_uuid(username)
        self.running = False
        self.online_mode: bool | None = None
        self._handlers: dict = defaultdict(list)
        # Outbound packet-id overrides for talking to a server whose packet
        # numbering drifted from our schema: {(state, name): id}. Needed when
        # advertising a protocol newer than the vendored data.
        self.id_overrides: dict = {}

        # -- movement state ---------------------------------------------
        self.position = {"x": 0.0, "y": 0.0, "z": 0.0,
                          "yaw": 0.0, "pitch": 0.0, "on_ground": True}
        self.spawned = False
        self.walk_speed = 4.317  # blocks/sec, vanilla walking speed
        self._position_lock = threading.Lock()
        self._position_thread: threading.Thread | None = None
        self._position_stop = threading.Event()
        self._client_tick_interval = 0.05  # vanilla's 20 Hz client tick
        self._position_interval = 0.5  # heartbeat while idle (server-side anti-AFK)
        self._control_until = 0.0
        self._control_velocity_y = 0.0
        self._control_jump_held = False
        self._navigation_lock = threading.Lock()
        self._navigation_generation = 0
        self._navigation_active = None

        # -- world state --------------------------------------------------
        # None when this version's chunk format or block table isn't supported.
        self.world: World | None = None
        if initial_version not in _LEGACY_CHUNK_FORMAT_VERSIONS:
            try:
                self.world = World(
                    get_block_table(initial_version), self.protocol.protocol_version)
            except ValueError:
                pass  # no block table (and no fallback) for this version
        self._world_packet_queue: queue.Queue = queue.Queue()
        self._world_worker: threading.Thread | None = None
        self._world_stop = threading.Event()
        self._world_generation = 0
        self._dimension_types: list[dict | None] = []
        self.dimension = None
        # Optional ResourcePack (see resourcepack.py) used by render_map() /
        # save_map() / start_live_map() when no per-call one is given.
        self.resource_pack = None

        # -- inventory state ------------------------------------------------
        try:
            self.inventory = Inventory(get_item_table(initial_version))
        except ValueError:
            self.inventory = Inventory(None)  # no item table; names stay None

        # -- player state ---------------------------------------------------
        # Populated from server packets once in Play; None until first seen.
        self.entity_id = None            # our own entity id (from Join Game)
        self.health = None               # 0.0-20.0
        self.food = None                 # 0-20
        self.saturation = None           # float
        self.gamemode = None             # "survival"/"creative"/"adventure"/"spectator"
        self.experience = {"bar": 0.0, "level": 0, "total": 0}
        self.effects: dict = {}          # effect_id -> {effect_id, amplifier, duration}

    # -- events ------------------------------------------------------------
    def on(self, event, fn=None):
        """Register a callback. `fn(name, params, raw)` for packet events, or
        `fn(*args)` for lifecycle events ('state', 'login', 'disconnect').
        Usable directly (`client.on("chat", handler)`) or as a decorator
        (`@client.on("chat")`)."""
        if fn is None:
            def decorator(fn):
                self._handlers[event].append(fn)
                return fn
            return decorator
        self._handlers[event].append(fn)
        return fn

    def off(self, event, fn) -> bool:
        """Remove a previously-registered callback. Returns True if it was
        found and removed. Safe to call with an unregistered fn."""
        handlers = self._handlers.get(event)
        if handlers and fn in handlers:
            handlers.remove(fn)
            return True
        return False

    def emit(self, event, *args):
        # Iterate a copy: a handler may off() itself (or another) mid-dispatch.
        for fn in list(self._handlers.get(event, ())):
            fn(*args)

    # -- sending -----------------------------------------------------------
    def send(self, name, params=None):
        body = self.protocol.encode(self.state, "toServer", name, params or {})
        override = self.id_overrides.get((self.state, name))
        if override is not None:
            body = self._swap_packet_id(body, override)
        self.conn.send_packet(body)

    @staticmethod
    def _swap_packet_id(body, new_id):
        from .buffer import Buffer
        b = Buffer(body)
        b.read_varint()  # discard schema id
        rest = b.read_rest()
        out = Buffer()
        out.write_varint(new_id)
        out.write_bytes(rest)
        return out.getvalue()

    def _has(self, state, direction, name):
        return name in self.protocol.id_to_name(state, direction).values()

    def _set_state(self, state):
        self.state = state
        self.emit("state", state)

    # -- connect / login ---------------------------------------------------
    def connect(self):
        """Open the socket, log in, then block pumping packets until closed."""
        if self.requested_version == "auto":
            self._detect_server_version()
        self.conn.connect()
        # Handshake: advertise the server's own protocol number so a server
        # newer than our vendored schema still accepts us.
        self.conn.send_packet(self.protocol.encode(
            "handshaking", "toServer", "set_protocol", {
                "protocolVersion": self.advertise_protocol,
                "serverHost": self.host, "serverPort": self.port, "nextState": 2,
            }))
        self._set_state("login")
        self.send("login_start", self._login_start_params())

        self.running = True
        try:
            while self.running:
                try:
                    self._pump()
                except OSError:
                    if not self.running:
                        break  # socket closed by stop(); clean exit
                    raise
        except (ConnectionError, Disconnected):
            self.running = False
            raise
        finally:
            self._world_stop.set()
            self.conn.close()

    def _detect_server_version(self):
        """Status-ping the server and select an exact local packet schema."""
        status_conn = Connection(self.host, self.port, timeout=8.0)
        try:
            status_conn.connect()
            status_conn.send_packet(self.protocol.encode(
                "handshaking", "toServer", "set_protocol", {
                    "protocolVersion": self.protocol.protocol_version,
                    "serverHost": self.host,
                    "serverPort": self.port,
                    "nextState": 1,
                }))
            status_conn.send_packet(self.protocol.encode(
                "status", "toServer", "ping_start", {}))
            _, params = self.protocol.decode(
                "status", "toClient", status_conn.read_packet())
            response = json.loads(params["response"])
            server_version = response["version"]
            protocol_number = int(server_version["protocol"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConnectionError(
                f"server status response did not contain a valid protocol: {exc}") from exc
        finally:
            status_conn.close()

        version = version_for_protocol(protocol_number)
        if version is None:
            supported = ", ".join(
                f"{number} ({name})"
                for number, name in sorted(available_protocols().items()))
            raise UnsupportedProtocol(
                f"server advertises unsupported Minecraft protocol "
                f"{protocol_number} ({server_version.get('name', 'unknown version')}); "
                f"supported protocols: {supported}")

        self._use_protocol_version(version, protocol_number)
        self.emit("protocol", {
            "version": version,
            "protocol": protocol_number,
            "server_name": server_version.get("name"),
        })

    def _use_protocol_version(self, version, protocol_number):
        """Replace provisional protocol-dependent state before login."""
        self.protocol = Protocol(version)
        self.advertise_protocol = protocol_number
        self.world = None
        if version not in _LEGACY_CHUNK_FORMAT_VERSIONS:
            try:
                self.world = World(
                    get_block_table(version), self.protocol.protocol_version)
            except ValueError:
                pass
        try:
            self.inventory = Inventory(get_item_table(version))
        except ValueError:
            self.inventory = Inventory(None)

    def stop(self):
        """Ask the pump loop to exit and close the socket."""
        self.running = False
        self._position_stop.set()
        self._world_stop.set()
        self.cancel_navigation()
        self.stop_live_map()
        self.stop_stream_server()
        self.conn.close()

    def chat(self, message):
        """Send a chat message (or command if it starts with '/')."""
        if message.startswith("/") and self._has("play", "toServer", "chat_command"):
            self.send("chat_command", {"command": message[1:]})
            return
        if self._has("play", "toServer", "chat_message"):
            import os
            import time as _t
            params = {
                "message": message,
                "timestamp": int(_t.time() * 1000),
                "salt": int.from_bytes(os.urandom(8), "big", signed=True),
                "signature": None,          # unsigned (server must allow it)
                "offset": 0,
                "acknowledged": b"\x00\x00\x00",
                "checksum": 0,
            }
            # only send fields this version actually defines
            fields = self._packet_fields("play", "toServer", "chat_message")
            self.send("chat_message", {k: v for k, v in params.items() if k in fields})
        elif self._has("play", "toServer", "chat"):
            self.send("chat", {"message": message})

    def _login_start_params(self):
        # login_start fields vary by version; fill what exists.
        fields = self._packet_fields("login", "toServer", "login_start")
        params = {}
        if "username" in fields:
            params["username"] = self.username
        if "playerUUID" in fields:
            params["playerUUID"] = self.uuid
        return params

    def _packet_fields(self, state, direction, name):
        """Names of the top-level fields of a packet body (best-effort)."""
        proto, _ = self.protocol._codec(state, direction)
        sw = proto.types["packet"][1][1]["type"][1]["fields"]
        tdef = sw.get(name)
        while isinstance(tdef, str) and tdef in proto.types:
            tdef = proto.types[tdef]
        if isinstance(tdef, list) and tdef[0] == "container":
            return {f.get("name") for f in tdef[1]}
        return set()

    # -- the pump ----------------------------------------------------------
    def _pump(self):
        raw = self.conn.read_packet()
        try:
            name = self.protocol.decode_name(self.state, "toClient", raw)
        except KeyError as exc:
            self.emit("unknown_packet", raw)
            return
        handler = getattr(self, f"_on_{self.state}", None)
        if handler is None:
            return
        try:
            handler(name, raw)
        except (OnlineModeRequired, Disconnected):
            raise
        except Exception as exc:  # noqa: BLE001
            # One packet whose body doesn't match our schema (expected with a
            # drift-hybrid schema like 26.2, which borrows 1.21.11 layouts) --
            # a mis-decode can surface as BufferUnderrun, struct.error,
            # OverflowError, or others depending on which field diverged.
            # read_packet() is length-framed, so the stream is still aligned on
            # the next packet: skip this one rather than dropping the whole
            # connection. Surfaced as a non-fatal event, and logged to stderr
            # (journal) so a recurring offender can be pinned down.
            self.emit("decode_error", name, repr(exc))
            print(f"[mcbot] skipped {self.state}/{name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)

    def _decode(self, name, raw):
        """Fully decode a packet's params, tolerating schema drift."""
        try:
            _, params = self.protocol.decode(self.state, "toClient", raw)
            return params
        except (BufferUnderrun, KeyError, ValueError):
            return None

    # -- per-state handlers ------------------------------------------------
    def _on_login(self, name, raw):
        if name == "compress":
            params = self._decode(name, raw)
            self.conn.compression_threshold = params["threshold"]
        elif name == "encryption_begin":
            params = self._decode(name, raw) or {}
            should_authenticate = params.get("shouldAuthenticate", True)
            self.online_mode = bool(should_authenticate)
            self.emit("authentication", {
                "online_mode": self.online_mode,
                "encrypted": True,
            })
            if should_authenticate:
                raise OnlineModeRequired(
                    "server requires a premium/online-mode Minecraft account "
                    "(encryption request requires Microsoft authentication); "
                    "offline login cannot continue")
            self._enable_offline_encryption(params)
        elif name == "login_plugin_request":
            params = self._decode(name, raw) or {}
            self.send("login_plugin_response",
                      {"messageId": params.get("messageId", 0), "data": None})
        elif name == "success":
            if self.online_mode is None:
                self.online_mode = False
                self.emit("authentication", {
                    "online_mode": False,
                    "encrypted": False,
                })
            params = self._decode(name, raw)
            self.emit("login", params)
            if self._has("login", "toServer", "login_acknowledged"):
                self.send("login_acknowledged", {})
            if self.protocol.has_configuration:
                self._enter_configuration()
            else:
                self._set_state("play")
        elif name == "disconnect":
            params = self._decode(name, raw)
            self.emit("disconnect", params)
            self.running = False

    def _enable_offline_encryption(self, params):
        """Complete encryption when the server skips Microsoft authentication."""
        import os

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = serialization.load_der_public_key(params["publicKey"])
        shared_secret = os.urandom(16)

        def encrypt(value):
            return public_key.encrypt(value, padding.PKCS1v15())

        self.send("encryption_begin", {
            "sharedSecret": encrypt(shared_secret),
            "verifyToken": encrypt(params["verifyToken"]),
        })
        # The response itself is plaintext; encryption starts immediately
        # after it for every subsequent byte in both directions.
        self.conn.enable_encryption(shared_secret)

    def _enter_configuration(self):
        self._set_state("configuration")
        # Announce client settings immediately (some servers require it).
        if self._has("configuration", "toServer", "settings"):
            self.send("settings", self._default_settings())

    def _on_configuration(self, name, raw):
        if name == "keep_alive":
            params = self._decode(name, raw)
            self.send("keep_alive", {"keepAliveId": params["keepAliveId"]})
        elif name == "ping":
            params = self._decode(name, raw)
            self.send("pong", params)
        elif name == "select_known_packs":
            # claim no packs so the server streams full registry data
            self.send("select_known_packs", {"packs": []})
        elif name == "registry_data":
            params = self._decode(name, raw)
            if params.get("id", "").removeprefix("minecraft:") == "dimension_type":
                self._dimension_types = [entry.get("value") for entry in params["entries"]]
        elif name == "finish_configuration":
            self.send("finish_configuration", {})
            self._set_state("play")
            # Modern clients acknowledge that their terrain loading screen has
            # closed. Headless clients have no screen to wait for, and some
            # proxies withhold all play traffic until this packet arrives.
            if self._has("play", "toServer", "player_loaded"):
                self.send("player_loaded", {})
            self.emit("ready")
        elif name == "disconnect":
            self.emit("disconnect", self._decode(name, raw))
            self.running = False
        # registry_data / tags / feature_flags / custom_payload: ignored

    def _on_play(self, name, raw):
        params = None
        if name == "keep_alive":
            params = self._decode(name, raw)
            self.send("keep_alive", {"keepAliveId": params["keepAliveId"]})
        elif name == "ping" and self._has("play", "toServer", "pong"):
            params = self._decode(name, raw)
            self.send("pong", params)
        elif (name == "chunk_batch_finished"
              and self._has("play", "toServer", "chunk_batch_received")):
            # Since 1.20.2 the server pauses terrain streaming until the client
            # acknowledges each completed batch and reports an acceptable
            # processing rate. Keep this on the socket thread: it is tiny and
            # unblocks the next batch while chunk decoding continues off-thread.
            self._decode(name, raw)
            self.send("chunk_batch_received", {"chunksPerTick": 8.0})
        elif name == "position":
            # Server-authoritative position sync (spawn + teleports). Always
            # decoded and confirmed -- this is how we know where we are.
            params = self._decode(name, raw)
            self._handle_position_sync(params)
        elif name in _WORLD_PACKETS and self.world is not None:
            # Chunk decoding is CPU-heavy. Keep it off the socket pump so a
            # burst of terrain data cannot delay keepalive replies.
            self._queue_world_packet(name, raw)
        elif name == "window_items":
            params = self._decode(name, raw)
            self.inventory.set_window_items(
                params["windowId"], params.get("stateId", 0),
                params["items"], params.get("carriedItem"))
        elif name == "set_slot":
            params = self._decode(name, raw)
            self.inventory.set_slot(
                params["windowId"], params.get("stateId", 0),
                params["slot"], params["item"])
        elif name == "open_window":
            params = self._decode(name, raw)
            self.inventory.open_window(
                params["windowId"], params.get("inventoryType"), params.get("windowTitle"))
        elif name == "close_window":
            params = self._decode(name, raw)
            self.inventory.close_window()
        elif name == "held_item_slot":
            params = self._decode(name, raw)
            self.inventory.held_slot = params["slot"]
        elif name in ("login", "respawn", "update_health", "game_state_change",
                      "experience", "entity_effect", "remove_entity_effect"):
            params = self._decode(name, raw)
            if params is not None:
                self._update_player_state(name, params)
        elif name in ("kick_disconnect", "disconnect"):
            self.emit("disconnect", self._decode(name, raw))
            self.running = False
            return
        # Surface every packet as an event. Decode params only when someone is
        # listening for this packet (or for 'chat'/'packet'), to avoid paying
        # to parse thousands of movement/metadata packets nobody asked for.
        if params is None:
            wanted = (self._handlers.get(name) or self._handlers.get("packet")
                      or (self._handlers.get("chat") and name in _CHAT_PACKETS))
            if wanted:
                params = self._decode(name, raw)
        if name in _CHAT_PACKETS:
            self.emit("chat", name, params, raw)
        self.emit(name, name, params, raw)
        self.emit("packet", name, params, raw)

    def _queue_world_packet(self, name, raw):
        if self._world_worker is None or not self._world_worker.is_alive():
            self._world_stop.clear()
            self._world_worker = threading.Thread(
                target=self._world_packet_loop, daemon=True,
                name=f"world-{self.username}")
            self._world_worker.start()
        self._world_packet_queue.put((self._world_generation, name, raw))

    def _world_packet_loop(self):
        while not self._world_stop.is_set():
            try:
                generation, name, raw = self._world_packet_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if generation != self._world_generation:
                    continue
                # Chunk decoding is CPU-heavy pure Python and therefore still
                # contends for the GIL even on this worker thread. Let the
                # socket pump drain protocol traffic first so keepalives never
                # sit behind the initial terrain burst.
                while (not self._world_stop.is_set()
                       and self.conn.has_pending_data()):
                    _time.sleep(0.005)
                if generation != self._world_generation:
                    continue
                params = self._decode(name, raw)
                if params is None:
                    continue
                if name == "map_chunk":
                    self.world.load_chunk(
                        params["x"], params["z"], params["chunkData"])
                elif name == "unload_chunk":
                    self.world.unload_chunk(params["chunkX"], params["chunkZ"])
                elif name == "block_change":
                    loc = params["location"]
                    self.world.set_block_state(
                        loc["x"], loc["y"], loc["z"], params["type"])
                elif name == "multi_block_change":
                    self._apply_multi_block_change(params)
            except Exception as exc:  # noqa: BLE001 - skip one malformed update
                print(f"[mcbot] skipped world/{name}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                self._world_packet_queue.task_done()

    # -- movement ------------------------------------------------------------
    # Relative-axis bits for the legacy (<=1.20.1) raw-int position "flags".
    _LEGACY_REL_BITS = {"x": 0x01, "y": 0x02, "z": 0x04, "yaw": 0x08, "pitch": 0x10}

    def _handle_position_sync(self, params):
        flags = params.get("flags", 0)

        def is_relative(axis):
            if isinstance(flags, dict):
                return bool(flags.get(axis))
            return bool(flags & self._LEGACY_REL_BITS[axis])

        with self._position_lock:
            pos = self.position
            for axis in ("x", "y", "z", "yaw", "pitch"):
                pos[axis] = (pos[axis] + params[axis]) if is_relative(axis) else params[axis]

        if "teleportId" in params:
            confirm_name = ("teleport_confirm"
                             if self._has("play", "toServer", "teleport_confirm")
                             else "accept_teleportation")
            if self._has("play", "toServer", confirm_name):
                self.send(confirm_name, {"teleportId": params["teleportId"]})

        first_spawn = not self.spawned
        self.spawned = True
        if first_spawn:
            self._start_position_ticker()
        self.emit("spawn" if first_spawn else "move", dict(self.position))

    def _start_position_ticker(self):
        if self._position_thread and self._position_thread.is_alive():
            return
        self._position_stop.clear()
        self._position_thread = threading.Thread(
            target=self._position_tick_loop, daemon=True)
        self._position_thread.start()

    def _snap_to_ground(self) -> None:
        """Scan downward from the bot's feet and snap Y to the top of the
        highest solid block below.  No-ops when the chunk isn't loaded yet
        or when the world is unavailable.  This is simple floor-snapping,
        not full physics -- but it is enough to satisfy the vanilla server's
        anti-float check while the bot is standing still."""
        if (self.world is None
                or self.gamemode in ("creative", "spectator")
                or _time.monotonic() < self._control_until):
            return
        with self._position_lock:
            x, y, z = self.position["x"], self.position["y"], self.position["z"]
        new_y = self._ground_level_at(x, y, z)
        if new_y is None:
            return
        with self._position_lock:
            if abs(self.position["y"] - new_y) > 0.001:
                self.position["y"] = new_y
                self.position["on_ground"] = True

    def _ground_level_at(self, x, y, z):
        """Highest solid surface supporting the player box whose feet are at
        (x, y, z).

        The box is 0.6 wide, so it can straddle up to four block columns. We
        scan every column the footprint overlaps and return the *highest* top
        surface found -- that is the block the player actually stands on (and
        is what lets the bot climb a staircase whose step is in the neighbour
        column rather than dead-centre under its feet). Returns ``None`` only
        when no column has a loaded solid block below the scan window.
        """
        if self.world is None:
            return None
        r = _PLAYER_HALF_WIDTH
        eps = 1e-4
        x0, x1 = math.floor(x - r + eps), math.floor(x + r - eps)
        z0, z1 = math.floor(z - r + eps), math.floor(z + r - eps)
        foot_y = int(math.floor(y))
        # Scan down, limited to 32 blocks so a bot mid-air over an unloaded
        # chunk doesn't scan forever.
        scan_limit = max(self.world.min_y, foot_y - 32)
        best = None
        for bx in range(x0, x1 + 1):
            for bz in range(z0, z1 + 1):
                for by in range(foot_y, scan_limit - 1, -1):
                    name = self.world.block_name_at(bx, by, bz)
                    if name is None:
                        break  # chunk not loaded -- skip this column
                    if not _is_passable(name):
                        top = float(by + 1)
                        if best is None or top > best:
                            best = top
                        break
        return best

    def _box_blocked(self, x, y, z):
        """Whether the player's collision box, with feet at (x, y, z), overlaps
        any solid block. Samples every block cell the 0.6 x 1.8 x 0.6 box spans,
        so it accounts for player width rather than a single centre point."""
        if self.world is None:
            return False
        r = _PLAYER_HALF_WIDTH
        # Nudge the bounds inward by an epsilon so merely touching the face of an
        # adjacent block (standing exactly against a wall) doesn't count as a hit.
        eps = 1e-4
        x0, x1 = math.floor(x - r + eps), math.floor(x + r - eps)
        z0, z1 = math.floor(z - r + eps), math.floor(z + r - eps)
        y0 = math.floor(y + eps)
        y1 = math.floor(y + _PLAYER_HEIGHT - eps)
        for bx in range(x0, x1 + 1):
            for bz in range(z0, z1 + 1):
                for by in range(y0, y1 + 1):
                    if not _is_passable(self.world.block_name_at(bx, by, bz)):
                        return True
        return False

    def _position_tick_loop(self):
        next_position = _time.monotonic() + self._position_interval
        while not self._position_stop.wait(self._client_tick_interval):
            if self.state != "play":
                continue
            try:
                now = _time.monotonic()
                if now >= next_position:
                    self._snap_to_ground()
                    self._send_position_update()
                    next_position = now + self._position_interval
                self._send_tick_end()
            except OSError:
                # The socket pump owns disconnect reporting. A concurrent
                # ticker should simply stop once that socket has gone away.
                return


    def _position_update_params(self, packet_name="position_look"):
        fields = self._packet_fields("play", "toServer", packet_name)
        with self._position_lock:
            pos = dict(self.position)
        params = {k: pos[k] for k in ("x", "y", "z", "yaw", "pitch") if k in fields}
        if "onGround" in fields:
            params["onGround"] = pos["on_ground"]
        if "flags" in fields:
            params["flags"] = {"onGround": pos["on_ground"], "hasHorizontalCollision": False}
        return params

    def _send_position_update(self):
        if self._has("play", "toServer", "position_look"):
            self.send("position_look", self._position_update_params("position_look"))
        elif self._has("play", "toServer", "flying"):
            self.send("flying", self._position_update_params("flying"))

    def _send_tick_end(self):
        # Modern servers use this boundary to finish processing each client
        # tick. Older protocol schemas do not define it, so this is a no-op.
        if self._has("play", "toServer", "tick_end"):
            self.send("tick_end", {})

    def get_position(self):
        """Thread-safe snapshot of the bot's tracked position/orientation."""
        with self._position_lock:
            return dict(self.position)

    def look(self, yaw, pitch, on_ground=None):
        """Set the bot's facing direction and notify the server immediately."""
        with self._position_lock:
            self.position["yaw"] = float(yaw) % 360.0
            self.position["pitch"] = max(-90.0, min(90.0, float(pitch)))
            if on_ground is not None:
                self.position["on_ground"] = on_ground
        self._send_position_update()
        self.emit("move", self.get_position())

    def control_step(self, forward, strafe, jump, sneak, yaw, pitch,
                     seconds=0.05):
        """Apply one first-person control tick and report it to the server.

        This intentionally follows the same simple dead-reckoning model as
        ``move_to``, with lightweight jump/gravity and terrain following.
        """
        forward = max(-1.0, min(1.0, float(forward)))
        strafe = max(-1.0, min(1.0, float(strafe)))
        jump = bool(jump)
        sneak = bool(sneak)
        seconds = max(0.0, min(0.1, float(seconds)))
        length = max(1.0, math.hypot(forward, strafe))
        forward /= length
        strafe /= length
        yaw = float(yaw) % 360.0
        pitch = max(-90.0, min(90.0, float(pitch)))
        radians = math.radians(yaw)
        distance = self.walk_speed * (0.3 if sneak else 1.0) * seconds
        self._control_until = _time.monotonic() + 0.2

        dx = (-math.sin(radians) * forward
              - math.cos(radians) * strafe) * distance
        dz = (math.cos(radians) * forward
              - math.sin(radians) * strafe) * distance

        with self._position_lock:
            x, y, z = (self.position["x"], self.position["y"],
                       self.position["z"])
            on_ground = self.position["on_ground"]
            self.position["yaw"] = yaw
            self.position["pitch"] = pitch

            jump_started = (jump and not self._control_jump_held and on_ground)
            self._control_jump_held = jump
            if jump_started:
                self._control_velocity_y = _JUMP_VELOCITY
                on_ground = False

            # Horizontal move, resolved one axis at a time so the box slides
            # along a wall it hits instead of stopping dead. A blocked axis can
            # auto step-up a low ledge (only while grounded, with headroom).
            x, z = self._resolve_horizontal(x, y, z, dx, 0.0, on_ground)
            x, z = self._resolve_horizontal(x, y, z, 0.0, dz, on_ground)
            # Stepping up nudges the feet, but the exact surface is found below.
            if self._box_blocked(x, y, z):
                y = self._step_up_to(x, y, z)

            ground = self._ground_level_at(x, y, z)

            if on_ground:
                if ground is not None:
                    step = ground - y
                    if -0.51 <= step <= _STEP_HEIGHT + 0.01:
                        y = ground  # follow terrain / climb a low ledge
                    elif step < -0.51:
                        on_ground = False  # walked off an edge
                        self._control_velocity_y = 0.0
            else:
                vy = self._control_velocity_y
                new_y = y + vy * seconds
                if vy > 0 and self._box_blocked(x, new_y, z):
                    new_y = y  # bonked head on a ceiling
                    vy = 0.0
                y = new_y
                vy = (vy - _GRAVITY * seconds) * _VERTICAL_DRAG
                if vy <= 0 and ground is not None and y <= ground:
                    y = ground  # landed
                    on_ground = True
                    vy = 0.0
                self._control_velocity_y = vy

            self.position["x"], self.position["y"], self.position["z"] = x, y, z
            self.position["on_ground"] = on_ground
        self._send_position_update()
        self.emit("move", self.get_position())

    def _resolve_horizontal(self, x, y, z, dx, dz, on_ground):
        """Apply a single-axis horizontal delta, honouring collisions.

        Returns the new ``(x, z)``: the full move if clear, a step-up if a low
        ledge can be climbed, otherwise the original (blocked) coordinate.
        """
        if not dx and not dz:
            return x, z
        nx, nz = x + dx, z + dz
        if not self._box_blocked(nx, y, nz):
            return nx, nz
        if on_ground and not self._box_blocked(nx, y + _STEP_HEIGHT, nz):
            return nx, nz  # low ledge; feet snap up to the surface in caller
        # Blocked by a wall: creep up to its face in small increments instead of
        # stopping short, so the box ends flush against the wall (and can still
        # slide, since each axis is resolved separately).
        steps = max(1, int(math.ceil((abs(dx) + abs(dz)) / 0.05)))
        sx, sz = dx / steps, dz / steps
        for _ in range(steps):
            tx, tz = x + sx, z + sz
            if self._box_blocked(tx, y, tz):
                break
            x, z = tx, tz
        return x, z

    def _step_up_to(self, x, y, z):
        """Feet Y that clears the ledge the player is standing inside of."""
        offset = 0.5
        while offset <= _STEP_HEIGHT + 0.01:
            if not self._box_blocked(x, y + offset, z):
                return y + offset
            offset += 0.5
        return y

    def move_to(self, x, y, z, speed=None):
        """Walk in a straight line to (x, y, z), blocking the calling thread.

        This has no collision or gravity simulation -- it linearly
        interpolates position at a fixed 20Hz tick and reports it to the
        server, the same "dead reckoning" approach mineflayer's bare
        movement plugin uses before physics. It will happily walk through
        walls or off ledges; call from a background thread if you don't
        want to block your event handlers.
        """
        speed = speed or self.walk_speed
        tick = 0.05  # 20 Hz, matches the vanilla server tick rate
        with self._position_lock:
            start = (self.position["x"], self.position["y"], self.position["z"])
        dx, dy, dz = x - start[0], y - start[1], z - start[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance < 1e-6:
            return
        steps = max(1, int((distance / speed) / tick))
        for i in range(1, steps + 1):
            if not self.running:
                return
            t = i / steps
            with self._position_lock:
                self.position["x"] = start[0] + dx * t
                self.position["y"] = start[1] + dy * t
                self.position["z"] = start[2] + dz * t
            self._send_position_update()
            self.emit("move", self.get_position())
            _time.sleep(tick)

    def navigate_to(self, x, z):
        """Start replaceable, collision-aware pathfinding to an X/Z target."""
        x, z = float(x), float(z)
        with self._navigation_lock:
            self._navigation_generation += 1
            generation = self._navigation_generation
            self._navigation_active = generation
        thread = threading.Thread(
            target=self._navigate_loop, args=(generation, x, z), daemon=True,
            name=f"navigate-{self.username}")
        thread.start()
        return generation

    def cancel_navigation(self):
        with self._navigation_lock:
            if self._navigation_active is None:
                return False
            self._navigation_generation += 1
            self._navigation_active = None
            return True

    def _navigate_loop(self, generation, target_x, target_z):
        target = {"x": target_x, "z": target_z}
        def event(phase, **details):
            return {"phase": phase, "target": target,
                    "navigation_id": generation, **details}
        self.emit("navigation", event("started"))

        # Legacy protocols without decoded chunks retain the old direct walker.
        if self.world is None:
            self.emit("navigation", event("direct"))
            self._navigate_direct_loop(
                generation, target_x, target_z, event)
            return

        started = _time.monotonic()
        initial = self.get_position()
        initial_distance = math.hypot(
            target_x - initial["x"], target_z - initial["z"])
        max_seconds = max(
            20.0, initial_distance / max(self.walk_speed, 0.1) * 5.0)
        replans = 0

        while self.running and self.state == "play":
            if self._navigation_cancelled(generation):
                self.emit("navigation", event("cancelled"))
                return
            position = self.get_position()
            if math.hypot(
                    target_x - position["x"], target_z - position["z"]) <= 0.7:
                self._finish_navigation(generation)
                self.emit("navigation", event("arrived"))
                return

            if _time.monotonic() - started > max_seconds:
                self._finish_navigation(generation)
                self.emit("navigation", event("stuck"))
                return

            self.emit("navigation", event("planning", replan=replans))
            with self.world.lock:
                result = find_path(
                    self.world,
                    (position["x"], position["y"], position["z"]),
                    (target_x, target_z),
                    _is_passable,
                )
            if result is None:
                self._finish_navigation(generation)
                self.emit("navigation", event("no_path", replan=replans))
                return

            path = result.nodes[1:]
            self.emit("navigation", event(
                "path_found", waypoints=len(path), visited=result.visited,
                replan=replans))
            if self._follow_path(generation, path, started, max_seconds):
                self._finish_navigation(generation)
                self.emit("navigation", event("arrived"))
                return
            if self._navigation_cancelled(generation):
                self.emit("navigation", event("cancelled"))
                return
            replans += 1
            if replans > 3:
                self._finish_navigation(generation)
                self.emit("navigation", event("stuck", replans=replans))
                return

        self._finish_navigation(generation)
        self.emit("navigation", event("cancelled"))

    def _follow_path(self, generation, path, started, max_seconds):
        """Walk path nodes. False requests a replan after lost progress."""
        for block_x, feet_y, block_z in path:
            waypoint_x, waypoint_z = block_x + 0.5, block_z + 0.5
            best = float("inf")
            last_progress = _time.monotonic()
            tick = 0
            while self.running and self.state == "play":
                if self._navigation_cancelled(generation):
                    return False
                if _time.monotonic() - started > max_seconds:
                    return False
                position = self.get_position()
                dx = waypoint_x - position["x"]
                dz = waypoint_z - position["z"]
                horizontal = math.hypot(dx, dz)
                vertical = abs(feet_y - position["y"])
                distance = horizontal + vertical * 0.25
                if horizontal <= 0.3 and vertical <= 0.65:
                    break
                now = _time.monotonic()
                if distance < best - 0.08:
                    best = distance
                    last_progress = now
                elif now - last_progress > 3.0:
                    return False

                yaw = math.degrees(math.atan2(-dx, dz))
                # Wait over the destination column while gravity completes a
                # planned drop. One-tick jump pulses help full-block climbs on
                # servers whose collision correction is stricter than ours.
                forward = 0.0 if horizontal <= 0.22 else 1.0
                jump = (feet_y > position["y"] + 0.55
                        and horizontal < 1.1
                        and bool(position.get("on_ground"))
                        and tick % 6 == 0)
                self.control_step(
                    forward, 0.0, jump, False, yaw, 0.0, 0.05)
                tick += 1
                _time.sleep(0.05)
        return True

    def _navigate_direct_loop(self, generation, target_x, target_z, event):
        started = _time.monotonic()
        best_distance = float("inf")
        last_progress = started
        max_seconds = None
        tick = 0
        while self.running and self.state == "play":
            if self._navigation_cancelled(generation):
                self.emit("navigation", event("cancelled"))
                return
            position = self.get_position()
            dx, dz = target_x - position["x"], target_z - position["z"]
            distance = math.hypot(dx, dz)
            if distance <= 0.7:
                self._finish_navigation(generation)
                self.emit("navigation", event("arrived"))
                return
            if max_seconds is None:
                max_seconds = max(
                    15.0, distance / max(self.walk_speed, 0.1) * 3.0)
            now = _time.monotonic()
            if distance < best_distance - 0.2:
                best_distance = distance
                last_progress = now
            elif now - last_progress > 5.0:
                self._finish_navigation(generation)
                self.emit("navigation", event("stuck"))
                return
            if now - started > max_seconds:
                self._finish_navigation(generation)
                self.emit("navigation", event("stuck"))
                return
            yaw = math.degrees(math.atan2(-dx, dz))
            jump = (now - last_progress > 0.8 and tick % 10 == 0
                    and bool(position.get("on_ground")))
            self.control_step(1.0, 0.0, jump, False, yaw, 0.0, 0.05)
            tick += 1
            _time.sleep(0.05)
        self._finish_navigation(generation)
        self.emit("navigation", event("cancelled"))

    def _navigation_cancelled(self, generation):
        with self._navigation_lock:
            return self._navigation_active != generation

    def _finish_navigation(self, generation):
        with self._navigation_lock:
            if self._navigation_active == generation:
                self._navigation_active = None

    def _apply_multi_block_change(self, params):
        coords = params["chunkCoordinates"]
        section_x, section_y, section_z = coords["x"], coords["y"], coords["z"]
        for record in params["records"]:
            state_id = record >> 12
            local = record & 0xFFF
            lx, lz, ly = (local >> 8) & 0xF, (local >> 4) & 0xF, local & 0xF
            self.world.set_block_state(
                section_x * 16 + lx, section_y * 16 + ly, section_z * 16 + lz, state_id)

    # -- player state --------------------------------------------------------
    _GAMEMODES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}

    @classmethod
    def _gamemode_name(cls, value):
        """Normalize a gamemode (int, float, or already-a-name) to a name."""
        if isinstance(value, str):
            return value
        if value is None:
            return None
        return cls._GAMEMODES.get(int(value), str(value))

    def _update_player_state(self, name, params):
        """Fold a decoded player-related packet into tracked player state.
        Emits a 'player_state' event when anything changed."""
        if name == "login":
            self._apply_dimension(params)
            self.entity_id = params.get("entityId", self.entity_id)
            self.effects.clear()  # fresh world; old effects no longer apply
            self.gamemode = self._extract_gamemode(params) or self.gamemode
        elif name == "respawn":
            self._apply_dimension(params)
            self.effects.clear()
            gm = self._extract_gamemode(params)
            if gm is not None:
                self.gamemode = gm

        elif name == "update_health":
            self.health = params.get("health")
            self.food = params.get("food")
            self.saturation = params.get("foodSaturation")
        elif name == "game_state_change":
            # reason is "change_game_mode" (mapper) or 3 (raw u8), value in gameMode
            reason = params.get("reason")
            if reason in ("change_game_mode", 3):
                self.gamemode = self._gamemode_name(params.get("gameMode"))
        elif name == "experience":
            self.experience = {
                "bar": params.get("experienceBar", 0.0),
                "level": params.get("level", 0),
                "total": params.get("totalExperience", 0),
            }
        elif name == "entity_effect":
            if params.get("entityId") == self.entity_id:
                eid = params.get("effectId")
                self.effects[eid] = {
                    "effect_id": eid,
                    "amplifier": params.get("amplifier", 0),
                    "duration": params.get("duration", 0),
                }
        elif name == "remove_entity_effect":
            if params.get("entityId") == self.entity_id:
                self.effects.pop(params.get("effectId"), None)
        self.emit("player_state", self.player_state())

    def _apply_dimension(self, params):
        """Select the server-provided vertical range before decoding chunks."""
        if self.world is None:
            return
        world_state = params.get("worldState") or {}
        dimension = world_state.get("dimension", params.get("dimension"))
        metadata = None
        if isinstance(dimension, int) and 0 <= dimension < len(self._dimension_types):
            metadata = self._dimension_types[dimension]
        elif isinstance(dimension, dict):
            metadata = dimension
        min_y = metadata.get("min_y") if isinstance(metadata, dict) else None
        if not isinstance(min_y, int):
            min_y = self.world.min_y
        self.dimension = {
            "id": dimension,
            "name": world_state.get("name", params.get("worldName")),
            "min_y": min_y,
            "height": metadata.get("height") if isinstance(metadata, dict) else None,
        }
        self._world_generation += 1
        self.world.reset_dimension(min_y)

    def _extract_gamemode(self, params):
        """Gamemode from a login/respawn packet, whichever shape the version
        uses: a top-level field (<=1.18) or nested in `worldState`/SpawnInfo."""
        if "gameMode" in params or "gamemode" in params:
            return self._gamemode_name(params.get("gameMode", params.get("gamemode")))
        world_state = params.get("worldState")
        if isinstance(world_state, dict):
            return self._gamemode_name(world_state.get("gamemode", world_state.get("gameMode")))
        return None

    def player_state(self) -> dict:
        """Snapshot of tracked player vitals/mode/effects/xp + position."""
        return {
            "entity_id": self.entity_id,
            "health": self.health,
            "food": self.food,
            "saturation": self.saturation,
            "gamemode": self.gamemode,
            "experience": dict(self.experience),
            "effects": [dict(e) for e in self.effects.values()],
            "position": self.get_position(),
        }

    # -- world queries -------------------------------------------------------
    def block_at(self, x, y, z):
        """Block name at an absolute (x, y, z), or None if unloaded/unsupported."""
        if self.world is None:
            return None
        return self.world.block_name_at(int(x), int(y), int(z))

    def nearby_blocks(self, radius=8, include_air=False):
        """Block name + position for every loaded block within `radius` of
        the bot's current position. Empty list if world tracking is off."""
        if self.world is None:
            return []
        with self._position_lock:
            x, y, z = self.position["x"], self.position["y"], self.position["z"]
        return self.world.nearby_blocks(x, y, z, radius, include_air=include_air)

    # -- map rendering ---------------------------------------------------
    def render_map(self, radius=64, resource_pack=None, center_x=None, center_z=None):
        """A numpy uint8 (H, W, 3) top-down map of loaded chunks centered on
        the bot or an explicit X/Z point. Requires numpy (`pip install numpy`)
        and world tracking.
        `resource_pack`: an optional `ResourcePack` (see `resourcepack.py`) to
        color blocks from real textures instead of the built-in
        approximations; defaults to `self.resource_pack` if set."""
        from .render import render_top_down
        if self.world is None:
            raise ValueError("render_map requires world tracking (unsupported for this version)")
        with self._position_lock:
            pos = (self.position["x"], self.position["y"], self.position["z"])
        map_x = int(pos[0]) if center_x is None else int(center_x)
        map_z = int(pos[2]) if center_z is None else int(center_z)
        return render_top_down(self.world, map_x, map_z, radius, bot_position=pos,
                                resource_pack=resource_pack or self.resource_pack)

    def save_map(self, path, radius=64, resource_pack=None):
        """Render and write a PNG of the current top-down map to `path`."""
        from .render import encode_png
        with open(path, "wb") as fh:
            fh.write(encode_png(self.render_map(radius, resource_pack)))

    def start_live_map(self, path, interval=0.5, radius=64, resource_pack=None):
        """Continuously re-render and save the map to `path` every `interval`
        seconds, in a background thread, until `stop_live_map()`."""
        self._live_map_stop = threading.Event()

        def loop():
            while not self._live_map_stop.wait(interval):
                if self.state == "play":
                    try:
                        self.save_map(path, radius, resource_pack)
                    except ValueError:
                        pass  # world not ready yet (no chunks loaded)

        self._live_map_thread = threading.Thread(target=loop, daemon=True)
        self._live_map_thread.start()

    def stop_live_map(self):
        if getattr(self, "_live_map_stop", None) is not None:
            self._live_map_stop.set()

    # -- streaming world data to a separate render process --------------------
    def start_stream_server(self, host="127.0.0.1", port=25566, radius=48, tick_interval=0.2):
        """Serve nearby chunk data + live position to `ChunkStreamClient`
        connections, so a separate process can render without costing this
        bot's event loop anything beyond cheap background-thread work.
        Requires world tracking (unsupported for this version otherwise)."""
        from .stream import ChunkStreamServer
        if self.world is None:
            raise ValueError("start_stream_server requires world tracking (unsupported for this version)")
        self._stream_server = ChunkStreamServer(
            self, host=host, port=port, radius=radius, tick_interval=tick_interval)
        return self._stream_server

    def stop_stream_server(self):
        if getattr(self, "_stream_server", None) is not None:
            self._stream_server.stop()
            self._stream_server = None

    # -- inventory -----------------------------------------------------------
    def select_hotbar(self, slot: int) -> None:
        """Select a hotbar slot (0-8) as the held item."""
        if not (0 <= slot <= 8):
            raise ValueError("hotbar slot must be 0-8")
        fields = self._packet_fields("play", "toServer", "held_item_slot")
        key = "slotId" if "slotId" in fields else "slot"
        self.send("held_item_slot", {key: slot})
        self.inventory.held_slot = slot

    def creative_give(self, slot: int, item_name: str, count: int = 1) -> None:
        """Place an item stack directly into a slot. Creative mode only --
        the server silently ignores this in survival."""
        item_id = self.inventory.item_table.id_for(item_name) if self.inventory.item_table else None
        if item_id is None:
            raise ValueError(f"unknown item {item_name!r} for this version")
        field_names = self._slot_field_names("toServer", "set_creative_slot", "item")
        self.send("set_creative_slot", {
            "slot": slot, "item": make_slot_value(field_names, item_id, count)})

    def close_window(self) -> None:
        """Close whichever non-player window is currently open."""
        window_id = self.inventory.window_id
        if window_id != 0:
            self.send("close_window", {"windowId": window_id})
        self.inventory.close_window()

    def click_slot(self, *args, **kwargs):
        """Not implemented: survival-mode slot clicks (`window_click`) require
        replicating the server's item-hash algorithm for `HashedSlot` (added
        alongside data components, 1.21.2+) to declare predicted slot/cursor
        state. That hashing scheme isn't implemented here. Use
        `creative_give` for creative-mode item placement instead."""
        raise NotImplementedError(
            "window_click (survival slot clicking) is not implemented -- "
            "see click_slot's docstring")

    def _slot_field_names(self, direction, packet_name, field_name):
        """Outer field names of a packet field's resolved container type."""
        proto, _ = self.protocol._codec("play", direction)
        sw = proto.types["packet"][1][1]["type"][1]["fields"]
        tdef = sw.get(packet_name)
        while isinstance(tdef, str) and tdef in proto.types:
            tdef = proto.types[tdef]
        if not (isinstance(tdef, list) and tdef[0] == "container"):
            return set()
        field = next((f for f in tdef[1] if f.get("name") == field_name), None)
        if field is None:
            return set()
        ftdef = field["type"]
        while isinstance(ftdef, str) and ftdef in proto.types:
            ftdef = proto.types[ftdef]
        if isinstance(ftdef, list) and ftdef[0] == "container":
            return {f.get("name") for f in ftdef[1] if "name" in f}
        return set()

    # -- defaults ----------------------------------------------------------
    def _default_settings(self):
        fields = self._packet_fields("configuration", "toServer", "settings")
        base = {
            "locale": "en_US", "viewDistance": 8, "chatFlags": 0,
            "chatColors": True, "skinParts": 0x7F, "mainHand": 1,
            "enableTextFiltering": False, "enableServerListing": True,
            "particleStatus": "all",
        }
        return {k: v for k, v in base.items() if k in fields}
