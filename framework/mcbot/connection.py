"""The transport layer: socket + framing + compression + (optional) encryption.

Wire format of one packet:
  * uncompressed:  VarInt(length) | packet-bytes
  * compressed:    VarInt(packet_length) | VarInt(data_length) | payload
      - data_length == 0  -> payload is the raw packet-bytes (below threshold)
      - data_length  > 0  -> payload is zlib(packet-bytes); data_length is the
                             uncompressed size

"packet-bytes" here means VarInt(id) + params -- exactly what `Protocol.encode`
produces and `Protocol.decode` consumes. This layer is version-agnostic.

Encryption (online mode) is AES/CFB8 over the whole stream once enabled; the
hook is here (`enable_encryption`) but the online handshake lives in `auth/`.
"""

from __future__ import annotations

import select
import socket
import threading
import zlib

from .buffer import Buffer


class Connection:
    def __init__(self, host: str, port: int = 25565, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.compression_threshold = -1  # -1 = compression disabled
        self._encryptor = None
        self._decryptor = None
        # Encryption is a stateful byte stream. Movement, keepalive, chat, and
        # heartbeat packets can originate on different threads, so one lock
        # must cover framing, encryption, and the socket write as a unit.
        self._send_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def enable_encryption(self, shared_secret: bytes) -> None:
        """Turn on AES/CFB8 stream encryption (online mode)."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(shared_secret), modes.CFB8(shared_secret))
        self._encryptor = cipher.encryptor()
        self._decryptor = cipher.decryptor()

    # -- raw socket i/o (handles encryption transparently) -----------------
    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ConnectionError("server closed the connection")
            if self._decryptor is not None:
                chunk = self._decryptor.update(chunk)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_varint(self) -> int:
        """Read a VarInt straight off the socket, byte by byte."""
        value = 0
        for i in range(5):
            byte = self._recv_exact(1)[0]
            value |= (byte & 0x7F) << (7 * i)
            if not byte & 0x80:
                if value & (1 << 31):
                    value -= 1 << 32
                return value
        raise ValueError("VarInt too long on socket")

    def _send_all(self, data: bytes) -> None:
        if self._encryptor is not None:
            data = self._encryptor.update(data)
        self.sock.sendall(data)

    # -- packet i/o --------------------------------------------------------
    def send_packet(self, packet_bytes: bytes) -> None:
        """Frame (and compress) one packet and put it on the wire."""
        with self._send_lock:
            if self.compression_threshold >= 0:
                if len(packet_bytes) >= self.compression_threshold:
                    data_length = len(packet_bytes)
                    body = zlib.compress(packet_bytes)
                else:
                    data_length = 0
                    body = packet_bytes
                inner = Buffer()
                inner.write_varint(data_length)
                inner.write_bytes(body)
                payload = inner.getvalue()
            else:
                payload = packet_bytes

            frame = Buffer()
            frame.write_varint(len(payload))
            frame.write_bytes(payload)
            self._send_all(frame.getvalue())

    def read_packet(self) -> bytes:
        """Block until a full packet arrives; return its raw id+params bytes."""
        length = self._recv_varint()
        payload = self._recv_exact(length)

        if self.compression_threshold >= 0:
            buf = Buffer(payload)
            data_length = buf.read_varint()
            rest = buf.read_rest()
            if data_length == 0:
                return rest
            return zlib.decompress(rest)
        return payload

    def has_pending_data(self) -> bool:
        """Return whether the socket can be read without blocking."""
        sock = self.sock
        if sock is None:
            return False
        try:
            return bool(select.select((sock,), (), (), 0)[0])
        except (OSError, ValueError):
            return False
