"""Low-level byte (de)serialization for the Minecraft protocol.

`Buffer` wraps a byte string with a read cursor and an append-only write
buffer. It only knows about *primitive* wire encodings (VarInt, big/little
endian integers, floats, length-prefixed strings, ...). Everything structural
(containers, arrays, switches) lives in the protodef interpreter in `types.py`.

Reference: https://minecraft.wiki/w/Java_Edition_protocol#Data_types
"""

from __future__ import annotations

import struct


class BufferUnderrun(Exception):
    """Raised when a read needs more bytes than remain in the buffer.

    The connection layer uses this to know a packet is incomplete and it
    should wait for more data from the socket.
    """


class Buffer:
    def __init__(self, data: bytes = b"") -> None:
        self._read = memoryview(bytes(data))
        self._pos = 0
        self._write = bytearray()

    # -- state -------------------------------------------------------------
    @property
    def remaining(self) -> int:
        return len(self._read) - self._pos

    def getvalue(self) -> bytes:
        """Return everything written so far."""
        return bytes(self._write)

    def read_bytes(self, n: int) -> bytes:
        if n < 0:
            raise ValueError(f"negative read length {n}")
        if self._pos + n > len(self._read):
            raise BufferUnderrun(
                f"tried to read {n} bytes, only {self.remaining} remain"
            )
        out = self._read[self._pos : self._pos + n]
        self._pos += n
        return bytes(out)

    def peek_byte(self) -> int:
        if self._pos >= len(self._read):
            raise BufferUnderrun("peek past end of buffer")
        return self._read[self._pos]

    def read_rest(self) -> bytes:
        out = self._read[self._pos :]
        self._pos = len(self._read)
        return bytes(out)

    def write_bytes(self, data: bytes) -> None:
        self._write += data

    # -- fixed-width integers ---------------------------------------------
    # struct format chars, big-endian ('>') unless a little-endian ('l'*)
    # variant is requested. Signed/unsigned distinction matters on read.
    _INT_FMT = {
        "i8": ">b", "u8": ">B",
        "i16": ">h", "u16": ">H", "li16": "<h", "lu16": "<H",
        "i32": ">i", "u32": ">I", "li32": "<i", "lu32": "<I",
        "i64": ">q", "u64": ">Q", "li64": "<q", "lu64": "<Q",
        "f32": ">f", "f64": ">d", "lf32": "<f", "lf64": "<d",
    }
    _INT_SIZE = {
        "i8": 1, "u8": 1,
        "i16": 2, "u16": 2, "li16": 2, "lu16": 2,
        "i32": 4, "u32": 4, "li32": 4, "lu32": 4,
        "i64": 8, "u64": 8, "li64": 8, "lu64": 8,
        "f32": 4, "f64": 8, "lf32": 4, "lf64": 8,
    }

    def read_num(self, kind: str):
        return struct.unpack(self._INT_FMT[kind], self.read_bytes(self._INT_SIZE[kind]))[0]

    def write_num(self, kind: str, value) -> None:
        self.write_bytes(struct.pack(self._INT_FMT[kind], value))

    # -- VarInt / VarLong --------------------------------------------------
    # LEB128-ish: 7 data bits per byte, high bit = continuation. The decoded
    # value is interpreted as a two's-complement signed 32/64-bit integer.
    def read_varint(self, max_bits: int = 32) -> int:
        value = 0
        for i in range((max_bits + 6) // 7):
            byte = self.read_bytes(1)[0]
            value |= (byte & 0x7F) << (7 * i)
            if not byte & 0x80:
                # sign-extend from max_bits
                if value & (1 << (max_bits - 1)):
                    value -= 1 << max_bits
                return value
        raise ValueError("VarInt is too long / malformed")

    def read_varlong(self) -> int:
        return self.read_varint(64)

    def write_varint(self, value: int, max_bits: int = 32) -> None:
        # normalize to unsigned in the target width
        value &= (1 << max_bits) - 1
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                self.write_bytes(bytes((byte | 0x80,)))
            else:
                self.write_bytes(bytes((byte,)))
                return

    def write_varlong(self, value: int) -> None:
        self.write_varint(value, 64)

    @staticmethod
    def varint_size(value: int, max_bits: int = 32) -> int:
        value &= (1 << max_bits) - 1
        n = 1
        while value >= 0x80:
            value >>= 7
            n += 1
        return n

    # -- strings / buffers -------------------------------------------------
    def read_string(self) -> str:
        length = self.read_varint()
        return self.read_bytes(length).decode("utf-8")

    def write_string(self, value: str) -> None:
        data = value.encode("utf-8")
        self.write_varint(len(data))
        self.write_bytes(data)

    def read_bool(self) -> bool:
        return self.read_bytes(1)[0] != 0

    def write_bool(self, value: bool) -> None:
        self.write_bytes(b"\x01" if value else b"\x00")

    def read_uuid(self) -> str:
        raw = self.read_bytes(16)
        h = raw.hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    def write_uuid(self, value: str) -> None:
        self.write_bytes(bytes.fromhex(value.replace("-", "")))
