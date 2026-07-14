"""Stream world/position data from a running bot to a separate process.

The point is to keep rendering work off the bot's own event loop entirely:
`ChunkStreamServer` runs inside the bot process and pushes position updates
plus a bounded, radius-limited window of nearby chunk data to any connected
viewers; `ChunkStreamClient` runs in a wholly separate script, reconstructs a
local `World` from what it receives, and can render at whatever pace it can
sustain without costing the bot anything beyond serializing small updates.

All socket I/O on the server side happens on background threads via a queue
-- the bot's packet-pump loop (and any event handler on it) only ever pays
for a cheap, non-blocking `queue.put()`, even if a slow or stalled viewer
would otherwise block a socket write.

This is a small private protocol (not Minecraft's), built on the same
length-prefixed-frame idea and `Buffer` primitives as the real one:

    frame := VarInt(payload_length) payload
    payload := message_type(1 byte) message_body

Message types:
    HELLO (0)        server->client, once: version(string), min_y(i32)
    POSITION (1)     x,y,z(f64) yaw,pitch(f32)
    CHUNK_LOAD (2)   chunk_x,chunk_z(i32) + zlib-compressed concatenated
                     section bytes (array('I', 4096) per section, back to back)
    CHUNK_UNLOAD (3) chunk_x,chunk_z(i32)
    BLOCK_CHANGE (4) x,y,z(i32) state_id(VarInt)
"""

from __future__ import annotations

import queue
import socket
import threading
import zlib
from array import array

from .blocks import get_block_table
from .buffer import Buffer
from .world import World

MSG_HELLO = 0
MSG_POSITION = 1
MSG_CHUNK_LOAD = 2
MSG_CHUNK_UNLOAD = 3
MSG_BLOCK_CHANGE = 4

_SECTION_BYTES = 4096 * 4  # array('I', 4096) -> 4 bytes/entry


# -- framing -----------------------------------------------------------------

def _send_frame(sock, payload: bytes) -> None:
    header = Buffer()
    header.write_varint(len(payload))
    sock.sendall(header.getvalue() + payload)


def _recv_exact(sock, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("stream connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(sock) -> bytes:
    value, shift = 0, 0
    while True:
        byte = _recv_exact(sock, 1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return _recv_exact(sock, value)


# -- message encoding ---------------------------------------------------------

def _encode_hello(version: str, min_y: int) -> bytes:
    b = Buffer()
    b.write_bytes(bytes((MSG_HELLO,)))
    b.write_string(version)
    b.write_num("i32", min_y)
    return b.getvalue()


def _encode_position(x, y, z, yaw, pitch) -> bytes:
    b = Buffer()
    b.write_bytes(bytes((MSG_POSITION,)))
    b.write_num("f64", x)
    b.write_num("f64", y)
    b.write_num("f64", z)
    b.write_num("f32", yaw)
    b.write_num("f32", pitch)
    return b.getvalue()


def _encode_chunk_load(cx, cz, compressed: bytes) -> bytes:
    b = Buffer()
    b.write_bytes(bytes((MSG_CHUNK_LOAD,)))
    b.write_num("i32", cx)
    b.write_num("i32", cz)
    b.write_bytes(compressed)
    return b.getvalue()


def _encode_chunk_unload(cx, cz) -> bytes:
    b = Buffer()
    b.write_bytes(bytes((MSG_CHUNK_UNLOAD,)))
    b.write_num("i32", cx)
    b.write_num("i32", cz)
    return b.getvalue()


def _encode_block_change(x, y, z, state_id) -> bytes:
    b = Buffer()
    b.write_bytes(bytes((MSG_BLOCK_CHANGE,)))
    b.write_num("i32", x)
    b.write_num("i32", y)
    b.write_num("i32", z)
    b.write_varint(state_id)
    return b.getvalue()


# -- server (runs inside the bot process) -------------------------------------

class ChunkStreamServer:
    def __init__(self, bot, host="127.0.0.1", port=25566, radius=48, tick_interval=0.2):
        self.bot = bot
        self.radius = radius
        self.tick_interval = tick_interval

        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.bind((host, port))
        self._listen_sock.listen(5)
        self.port = self._listen_sock.getsockname()[1]

        self._clients: dict = {}  # socket -> set of chunk coords already sent to it
        self._clients_lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()  # (target_sock_or_None, payload)
        self._stop = threading.Event()
        self._last_sent_pos = None

        for target in (self._accept_loop, self._sender_loop, self._tick_loop):
            threading.Thread(target=target, daemon=True).start()

        bot.on("block_change", self._on_block_change)
        bot.on("multi_block_change", self._on_multi_block_change)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listen_sock.close()
        except OSError:
            pass
        with self._clients_lock:
            for sock in list(self._clients):
                try:
                    sock.close()
                except OSError:
                    pass
            self._clients.clear()

    # -- accepting -----------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listen_sock.accept()
            except OSError:
                return  # listen socket closed (stop() called)
            with self._clients_lock:
                self._clients[conn] = set()
            self._queue.put((conn, _encode_hello(self.bot.protocol.version, self.bot.world.min_y)))
            # An immediate baseline position: the broadcast-on-change path
            # below only reaches clients already connected *when* the change
            # happens, so a late joiner (the normal case) would otherwise
            # never learn the bot's position at all if it isn't moving.
            pos = self.bot.get_position()
            self._queue.put((conn, _encode_position(
                pos["x"], pos["y"], pos["z"], pos["yaw"], pos["pitch"])))
            # the next tick naturally sends a full catch-up burst: this
            # client's "already sent" set starts empty, so every in-range
            # chunk looks new to it.

    # -- sending ---------------------------------------------------------
    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                target, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            frame = Buffer()
            frame.write_varint(len(payload))
            data = frame.getvalue() + payload
            targets = [target] if target is not None else self._client_sockets()
            for sock in targets:
                try:
                    sock.sendall(data)
                except OSError:
                    self._drop_client(sock)

    def _client_sockets(self):
        with self._clients_lock:
            return list(self._clients.keys())

    def _client_items(self):
        with self._clients_lock:
            return list(self._clients.items())

    def _drop_client(self, sock) -> None:
        with self._clients_lock:
            self._clients.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass

    # -- periodic: position + windowed chunk load/unload ---------------------
    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_interval):
            if self.bot.state != "play" or self.bot.world is None:
                continue

            pos = self.bot.get_position()
            key = (pos["x"], pos["y"], pos["z"])
            if key != self._last_sent_pos:
                self._last_sent_pos = key
                self._queue.put((None, _encode_position(
                    pos["x"], pos["y"], pos["z"], pos["yaw"], pos["pitch"])))

            r = self.radius >> 4
            cx0, cz0 = int(pos["x"]) >> 4, int(pos["z"]) >> 4
            desired = {
                (cx, cz)
                for cx in range(cx0 - r, cx0 + r + 1)
                for cz in range(cz0 - r, cz0 + r + 1)
                if (cx, cz) in self.bot.world.chunks
            }
            for sock, sent in self._client_items():
                for coord in desired - sent:
                    payload = self._encode_chunk(coord)
                    if payload is not None:
                        self._queue.put((sock, payload))
                        sent.add(coord)
                for coord in sent - desired:
                    self._queue.put((sock, _encode_chunk_unload(*coord)))
                    sent.discard(coord)

    def _encode_chunk(self, coord):
        sections = self.bot.world.chunks.get(coord)
        if not sections:
            return None
        raw = b"".join(s.tobytes() for s in sections)
        return _encode_chunk_load(coord[0], coord[1], zlib.compress(raw, 6))

    # -- event hooks (called on the bot's pump thread -- must stay cheap) -----
    def _on_block_change(self, name, params, raw) -> None:
        loc = params["location"]
        self._queue.put((None, _encode_block_change(loc["x"], loc["y"], loc["z"], params["type"])))

    def _on_multi_block_change(self, name, params, raw) -> None:
        coords = params["chunkCoordinates"]
        sx, sy, sz = coords["x"], coords["y"], coords["z"]
        for record in params["records"]:
            state_id = record >> 12
            local = record & 0xFFF
            lx, lz, ly = (local >> 8) & 0xF, (local >> 4) & 0xF, local & 0xF
            self._queue.put((None, _encode_block_change(
                sx * 16 + lx, sy * 16 + ly, sz * 16 + lz, state_id)))


# -- client (runs in the separate render script) ------------------------------

class ChunkStreamClient:
    def __init__(self, host="127.0.0.1", port=25566, timeout=10.0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self.sock.settimeout(None)

        version, min_y = self._read_hello()
        # protocol_version is unused on this path: we never call
        # World.load_chunk() (which needs it to parse raw MC bytes) -- chunk
        # sections arrive already decoded, so we assign them directly.
        self.world = World(get_block_table(version), protocol_version=0, min_y=min_y)
        self.position = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "pitch": 0.0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def get_position(self) -> dict:
        with self._lock:
            return dict(self.position)

    def _read_hello(self):
        buf = Buffer(_recv_frame(self.sock))
        msg_type = buf.read_bytes(1)[0]
        if msg_type != MSG_HELLO:
            raise ValueError(f"expected HELLO (0), got message type {msg_type}")
        return buf.read_string(), buf.read_num("i32")

    def _recv_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._handle(Buffer(_recv_frame(self.sock)))
        except (ConnectionError, OSError):
            pass  # connection closed

    def _handle(self, buf: Buffer) -> None:
        msg_type = buf.read_bytes(1)[0]
        if msg_type == MSG_POSITION:
            x, y, z = buf.read_num("f64"), buf.read_num("f64"), buf.read_num("f64")
            yaw, pitch = buf.read_num("f32"), buf.read_num("f32")
            with self._lock:
                self.position.update(x=x, y=y, z=z, yaw=yaw, pitch=pitch)
        elif msg_type == MSG_CHUNK_LOAD:
            cx, cz = buf.read_num("i32"), buf.read_num("i32")
            raw = zlib.decompress(buf.read_rest())
            n_sections = len(raw) // _SECTION_BYTES
            sections = [array("I", raw[i * _SECTION_BYTES:(i + 1) * _SECTION_BYTES])
                        for i in range(n_sections)]
            self.world.chunks[(cx, cz)] = sections
            self.world.dirty_chunks.add((cx, cz))
        elif msg_type == MSG_CHUNK_UNLOAD:
            self.world.unload_chunk(buf.read_num("i32"), buf.read_num("i32"))
        elif msg_type == MSG_BLOCK_CHANGE:
            x, y, z = buf.read_num("i32"), buf.read_num("i32"), buf.read_num("i32")
            self.world.set_block_state(x, y, z, buf.read_varint())
