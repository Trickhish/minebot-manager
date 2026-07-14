"""Minimal NBT (Named Binary Tag) reader/writer.

Returns/accepts plain Python values. Compounds -> dict, lists -> list,
arrays -> list of ints. Tag type information is only needed on write, so the
writer infers types from Python values with an optional hint for lists/arrays.

Two wire flavours:
  * classic (<=1.20.1): the root tag carries a name.
  * network  (>=1.20.2): the root tag has no name.
Pass `network=True` for the newer flavour.
"""

from __future__ import annotations

import struct

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


def read_nbt(buf, network: bool = False, optional: bool = False):
    """Read a root NBT tag from a `mcbot.buffer.Buffer`.

    Returns None for an empty/absent tag. When `optional`, a leading TAG_END
    byte means "no tag".
    """
    tag_type = buf.read_bytes(1)[0]
    if tag_type == TAG_END:
        return None
    if not network:
        # skip the root name
        name_len = struct.unpack(">H", buf.read_bytes(2))[0]
        buf.read_bytes(name_len)
    return _read_payload(buf, tag_type)


def _read_payload(buf, tag_type):
    if tag_type == TAG_BYTE:
        return struct.unpack(">b", buf.read_bytes(1))[0]
    if tag_type == TAG_SHORT:
        return struct.unpack(">h", buf.read_bytes(2))[0]
    if tag_type == TAG_INT:
        return struct.unpack(">i", buf.read_bytes(4))[0]
    if tag_type == TAG_LONG:
        return struct.unpack(">q", buf.read_bytes(8))[0]
    if tag_type == TAG_FLOAT:
        return struct.unpack(">f", buf.read_bytes(4))[0]
    if tag_type == TAG_DOUBLE:
        return struct.unpack(">d", buf.read_bytes(8))[0]
    if tag_type == TAG_BYTE_ARRAY:
        n = struct.unpack(">i", buf.read_bytes(4))[0]
        return list(buf.read_bytes(n))
    if tag_type == TAG_STRING:
        n = struct.unpack(">H", buf.read_bytes(2))[0]
        return buf.read_bytes(n).decode("utf-8")
    if tag_type == TAG_LIST:
        item_type = buf.read_bytes(1)[0]
        n = struct.unpack(">i", buf.read_bytes(4))[0]
        return [_read_payload(buf, item_type) for _ in range(n)]
    if tag_type == TAG_COMPOUND:
        out = {}
        while True:
            child_type = buf.read_bytes(1)[0]
            if child_type == TAG_END:
                break
            name_len = struct.unpack(">H", buf.read_bytes(2))[0]
            name = buf.read_bytes(name_len).decode("utf-8")
            out[name] = _read_payload(buf, child_type)
        return out
    if tag_type == TAG_INT_ARRAY:
        n = struct.unpack(">i", buf.read_bytes(4))[0]
        return [struct.unpack(">i", buf.read_bytes(4))[0] for _ in range(n)]
    if tag_type == TAG_LONG_ARRAY:
        n = struct.unpack(">i", buf.read_bytes(4))[0]
        return [struct.unpack(">q", buf.read_bytes(8))[0] for _ in range(n)]
    raise ValueError(f"unknown NBT tag type {tag_type}")


def write_nbt(buf, value, network: bool = False, name: str = "") -> None:
    """Write a root NBT tag. None writes a single TAG_END byte."""
    if value is None:
        buf.write_bytes(bytes((TAG_END,)))
        return
    tag_type = _infer_type(value)
    buf.write_bytes(bytes((tag_type,)))
    if not network:
        encoded = name.encode("utf-8")
        buf.write_bytes(struct.pack(">H", len(encoded)) + encoded)
    _write_payload(buf, tag_type, value)


def _infer_type(value):
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, int):
        return TAG_INT
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, (list, tuple)):
        return TAG_LIST
    raise TypeError(f"cannot infer NBT type for {type(value)}")


def _write_payload(buf, tag_type, value):
    if tag_type == TAG_BYTE:
        buf.write_bytes(struct.pack(">b", int(value)))
    elif tag_type == TAG_SHORT:
        buf.write_bytes(struct.pack(">h", value))
    elif tag_type == TAG_INT:
        buf.write_bytes(struct.pack(">i", value))
    elif tag_type == TAG_LONG:
        buf.write_bytes(struct.pack(">q", value))
    elif tag_type == TAG_FLOAT:
        buf.write_bytes(struct.pack(">f", value))
    elif tag_type == TAG_DOUBLE:
        buf.write_bytes(struct.pack(">d", value))
    elif tag_type == TAG_STRING:
        encoded = value.encode("utf-8")
        buf.write_bytes(struct.pack(">H", len(encoded)) + encoded)
    elif tag_type == TAG_COMPOUND:
        for k, v in value.items():
            ct = _infer_type(v)
            buf.write_bytes(bytes((ct,)))
            ek = k.encode("utf-8")
            buf.write_bytes(struct.pack(">H", len(ek)) + ek)
            _write_payload(buf, ct, v)
        buf.write_bytes(bytes((TAG_END,)))
    elif tag_type == TAG_LIST:
        item_type = _infer_type(value[0]) if value else TAG_END
        buf.write_bytes(bytes((item_type,)))
        buf.write_bytes(struct.pack(">i", len(value)))
        for item in value:
            _write_payload(buf, item_type, item)
    else:
        raise TypeError(f"unsupported NBT payload type {tag_type} on write")
