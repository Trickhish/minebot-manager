"""A protodef interpreter.

`ProtoDef` reads/writes values described by the declarative type schemas that
ship with minecraft-data (the `types` maps inside `protocol.json`). Structural
types (container/array/switch/...) are handled generically here; leaf encodings
are delegated to `Buffer`. This is what lets one engine speak every version --
each version just supplies a different schema.

A type reference is either a string (a type name) or a two-element list
[name, args]. Names resolve against the merged type table; the base cases are
the "native" types implemented in `_NATIVE_READERS` / `_NATIVE_WRITERS`.
"""

from __future__ import annotations

from . import nbt
from .buffer import Buffer

_NUMERIC = {"i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "f32", "f64",
            "li16", "lu16", "li32", "lu32", "li64", "lu64", "lf32", "lf64"}


class Ctx:
    """A lexical scope used to resolve field references (compareTo, count).

    Each container gets its own Ctx whose `.data` fills in as fields are read
    or is pre-populated from the value being written.
    """

    __slots__ = ("data", "parent")

    def __init__(self, parent: "Ctx | None" = None, data=None):
        self.data = data if data is not None else {}
        self.parent = parent


def _resolve(ctx: Ctx, path):
    """Resolve a protodef field reference to a value.

    Supports 'name', '../name', 'a/b', and leading '/' for absolute paths.
    """
    if not isinstance(path, str):
        return path  # already a literal (e.g. an int count)
    node = ctx
    if path.startswith("/"):
        while node.parent is not None:
            node = node.parent
        parts = path[1:].split("/")
    else:
        parts = path.split("/")
    i = 0
    while i < len(parts) and parts[i] == "..":
        node = node.parent
        i += 1
    cur = node.data
    for p in parts[i:]:
        if p in ("", "."):
            continue
        cur = cur[p]
    return cur


class ProtoDef:
    def __init__(self, types: dict):
        # type name -> definition (string | [name, args] | "native")
        self.types = types
        self._mapper_cache: dict = {}

    # -- public API --------------------------------------------------------
    def read(self, buf: Buffer, tdef, ctx: Ctx | None = None):
        name, args = self._split(tdef)
        reader = _NATIVE_READERS.get(name)
        if reader is not None:
            return reader(self, buf, args, ctx if ctx is not None else Ctx())
        # composite: resolve and recurse
        if name in self.types:
            return self.read(buf, self.types[name], ctx)
        raise KeyError(f"unknown type {name!r}")

    def write(self, buf: Buffer, tdef, value, ctx: Ctx | None = None):
        name, args = self._split(tdef)
        writer = _NATIVE_WRITERS.get(name)
        if writer is not None:
            return writer(self, buf, args, value, ctx if ctx is not None else Ctx())
        if name in self.types:
            return self.write(buf, self.types[name], value, ctx)
        raise KeyError(f"unknown type {name!r}")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _split(tdef):
        if isinstance(tdef, str):
            return tdef, None
        if isinstance(tdef, list):
            return tdef[0], (tdef[1] if len(tdef) > 1 else None)
        raise TypeError(f"bad type definition {tdef!r}")

    def _mapper(self, args):
        """Return (forward int->name, reverse name->int) for a mapper, cached."""
        key = id(args)
        cached = self._mapper_cache.get(key)
        if cached is None:
            fwd = {}
            for k, v in args["mappings"].items():
                fwd[int(str(k), 0)] = v  # int(...,0) parses '0x1a' and '26'
            rev = {v: k for k, v in fwd.items()}
            cached = (fwd, rev)
            self._mapper_cache[key] = cached
        return cached


# --------------------------------------------------------------------------
# Native readers. Signature: (proto, buf, args, ctx) -> value
# --------------------------------------------------------------------------

def _r_numeric(kind):
    def read(proto, buf, args, ctx):
        return buf.read_num(kind)
    return read


def _r_varint(proto, buf, args, ctx):
    return buf.read_varint()


def _r_varlong(proto, buf, args, ctx):
    return buf.read_varlong()


def _r_bool(proto, buf, args, ctx):
    return buf.read_bool()


def _r_uuid(proto, buf, args, ctx):
    return buf.read_uuid()


def _r_void(proto, buf, args, ctx):
    return None


def _r_restbuffer(proto, buf, args, ctx):
    return buf.read_rest()


def _count(proto, buf, args, ctx):
    """Resolve the length for pstring/buffer/array from countType or count."""
    if args and "countType" in args:
        return proto.read(buf, args["countType"], ctx)
    if args and "count" in args:
        return _resolve(ctx, args["count"])
    raise ValueError(f"no count for {args!r}")


def _r_pstring(proto, buf, args, ctx):
    return buf.read_bytes(_count(proto, buf, args, ctx)).decode("utf-8")


def _r_buffer(proto, buf, args, ctx):
    if args and args.get("rest"):
        return buf.read_rest()
    return buf.read_bytes(_count(proto, buf, args, ctx))


def _r_container(proto, buf, args, ctx):
    child = Ctx(parent=ctx)
    out = {}
    for field in args:
        val = proto.read(buf, field["type"], child)
        if field.get("anon"):
            if isinstance(val, dict):
                out.update(val)
                child.data.update(val)
        else:
            out[field["name"]] = val
            child.data[field["name"]] = val
    return out


def _r_array(proto, buf, args, ctx):
    n = _count(proto, buf, args, ctx)
    return [proto.read(buf, args["type"], ctx) for _ in range(n)]


def _r_option(proto, buf, args, ctx):
    if buf.read_bool():
        return proto.read(buf, args, ctx)
    return None


def _r_switch(proto, buf, args, ctx):
    cmp = _resolve(ctx, args["compareTo"])
    fields = args.get("fields", {})
    tdef = fields.get(str(cmp), args.get("default"))
    if tdef is None:
        return None
    return proto.read(buf, tdef, ctx)


def _r_mapper(proto, buf, args, ctx):
    raw = proto.read(buf, args["type"], ctx)
    fwd, _ = proto._mapper(args)
    return fwd.get(raw, raw)


def _r_bitfield(proto, buf, args, ctx):
    total_bits = sum(f["size"] for f in args)
    nbytes = (total_bits + 7) // 8
    acc = int.from_bytes(buf.read_bytes(nbytes), "big")
    out = {}
    offset = total_bits
    for f in args:
        offset -= f["size"]
        val = (acc >> offset) & ((1 << f["size"]) - 1)
        if f.get("signed") and val & (1 << (f["size"] - 1)):
            val -= 1 << f["size"]
        out[f["name"]] = val
    return out


def _r_entity_metadata_loop(proto, buf, args, ctx):
    end_val = args["endVal"]
    out = []
    while True:
        if buf.peek_byte() == end_val:
            buf.read_bytes(1)
            break
        out.append(proto.read(buf, args["type"], ctx))
    return out


def _r_topbitset_array(proto, buf, args, ctx):
    out = []
    while True:
        b = buf.peek_byte()
        item = proto.read(buf, args["type"], ctx)
        out.append(item)
        if not (b & 0x80):
            break
    return out


def _r_registry_entry_holder(proto, buf, args, ctx):
    # 1.20.5+ "Holder": VarInt n. n==0 -> inline value follows (args.otherwise);
    # n!=0 -> a registry id reference, stored as (n - 1).
    n = buf.read_varint()
    if n != 0:
        return {args["baseName"]: n - 1}
    value = proto.read(buf, args["otherwise"]["type"], ctx)
    return {args["otherwise"]["name"]: value}


def _r_registry_entry_holder_set(proto, buf, args, ctx):
    # Same idea for a set: n==0 -> a single base-type value (args.base);
    # n!=0 -> (n - 1) inline values of args.otherwise.type.
    n = buf.read_varint()
    if n == 0:
        value = proto.read(buf, args["base"]["type"], ctx)
        return {args["base"]["name"]: value}
    items = [proto.read(buf, args["otherwise"]["type"], ctx) for _ in range(n - 1)]
    return {args["otherwise"]["name"]: items}


def _r_bitflags(proto, buf, args, ctx):
    value = proto.read(buf, args["type"], ctx)
    out = {}
    for i, flag_name in enumerate(args["flags"]):
        if flag_name:
            out[flag_name] = bool(value & (1 << i))
    return out


def _r_nbt(proto, buf, args, ctx):
    return nbt.read_nbt(buf, network=False)


def _r_optional_nbt(proto, buf, args, ctx):
    return nbt.read_nbt(buf, network=False, optional=True)


def _r_anon_nbt(proto, buf, args, ctx):
    # 1.20.2+ network NBT: root tag has no name
    return nbt.read_nbt(buf, network=True)


def _r_anon_optional_nbt(proto, buf, args, ctx):
    return nbt.read_nbt(buf, network=True, optional=True)


# --------------------------------------------------------------------------
# Native writers. Signature: (proto, buf, args, value, ctx) -> None
# --------------------------------------------------------------------------

def _w_numeric(kind):
    def write(proto, buf, args, value, ctx):
        buf.write_num(kind, value)
    return write


def _w_varint(proto, buf, args, value, ctx):
    buf.write_varint(value)


def _w_varlong(proto, buf, args, value, ctx):
    buf.write_varlong(value)


def _w_bool(proto, buf, args, value, ctx):
    buf.write_bool(value)


def _w_uuid(proto, buf, args, value, ctx):
    buf.write_uuid(value)


def _w_void(proto, buf, args, value, ctx):
    pass


def _w_restbuffer(proto, buf, args, value, ctx):
    buf.write_bytes(value)


def _w_count(proto, buf, args, value, ctx, length):
    """Write an explicit count if the type owns one (countType)."""
    if args and "countType" in args:
        proto.write(buf, args["countType"], length, ctx)
    # 'count' references a sibling field written elsewhere -> nothing to do


def _w_pstring(proto, buf, args, value, ctx):
    data = value.encode("utf-8")
    _w_count(proto, buf, args, value, ctx, len(data))
    buf.write_bytes(data)


def _w_buffer(proto, buf, args, value, ctx):
    if args and args.get("rest"):
        buf.write_bytes(value)
        return
    _w_count(proto, buf, args, value, ctx, len(value))
    buf.write_bytes(value)


def _w_container(proto, buf, args, value, ctx):
    child = Ctx(parent=ctx, data=dict(value) if value else {})
    for field in args:
        if field.get("anon"):
            proto.write(buf, field["type"], value, child)
        else:
            proto.write(buf, field["type"], value[field["name"]], child)


def _w_array(proto, buf, args, value, ctx):
    _w_count(proto, buf, args, value, ctx, len(value))
    for item in value:
        proto.write(buf, args["type"], item, ctx)


def _w_option(proto, buf, args, value, ctx):
    if value is None:
        buf.write_bool(False)
    else:
        buf.write_bool(True)
        proto.write(buf, args, value, ctx)


def _w_switch(proto, buf, args, value, ctx):
    cmp = _resolve(ctx, args["compareTo"])
    fields = args.get("fields", {})
    tdef = fields.get(str(cmp), args.get("default"))
    if tdef is None:
        return
    proto.write(buf, tdef, value, ctx)


def _w_mapper(proto, buf, args, value, ctx):
    _, rev = proto._mapper(args)
    proto.write(buf, args["type"], rev.get(value, value), ctx)


def _w_bitfield(proto, buf, args, value, ctx):
    total_bits = sum(f["size"] for f in args)
    nbytes = (total_bits + 7) // 8
    acc = 0
    for f in args:
        v = value[f["name"]] & ((1 << f["size"]) - 1)
        acc = (acc << f["size"]) | v
    buf.write_bytes(acc.to_bytes(nbytes, "big"))


def _w_entity_metadata_loop(proto, buf, args, value, ctx):
    for item in value:
        proto.write(buf, args["type"], item, ctx)
    buf.write_bytes(bytes((args["endVal"],)))


def _w_registry_entry_holder(proto, buf, args, value, ctx):
    base_name = args["baseName"]
    if base_name in value:
        buf.write_varint(value[base_name] + 1)
    else:
        buf.write_varint(0)
        proto.write(buf, args["otherwise"]["type"], value[args["otherwise"]["name"]], ctx)


def _w_registry_entry_holder_set(proto, buf, args, value, ctx):
    base_name = args["base"]["name"]
    if base_name in value:
        buf.write_varint(0)
        proto.write(buf, args["base"]["type"], value[base_name], ctx)
    else:
        items = value[args["otherwise"]["name"]]
        buf.write_varint(len(items) + 1)
        for item in items:
            proto.write(buf, args["otherwise"]["type"], item, ctx)


def _w_bitflags(proto, buf, args, value, ctx):
    acc = 0
    for i, flag_name in enumerate(args["flags"]):
        if flag_name and value.get(flag_name):
            acc |= 1 << i
    proto.write(buf, args["type"], acc, ctx)


def _w_nbt(proto, buf, args, value, ctx):
    nbt.write_nbt(buf, value, network=False)


def _w_anon_nbt(proto, buf, args, value, ctx):
    nbt.write_nbt(buf, value, network=True)


_NATIVE_READERS = {
    "varint": _r_varint, "varlong": _r_varlong, "bool": _r_bool,
    "UUID": _r_uuid, "void": _r_void, "restBuffer": _r_restbuffer,
    "pstring": _r_pstring, "buffer": _r_buffer, "container": _r_container,
    "array": _r_array, "option": _r_option, "switch": _r_switch,
    "mapper": _r_mapper, "bitfield": _r_bitfield, "bitflags": _r_bitflags,
    "registryEntryHolder": _r_registry_entry_holder,
    "registryEntryHolderSet": _r_registry_entry_holder_set,
    "entityMetadataLoop": _r_entity_metadata_loop,
    "topBitSetTerminatedArray": _r_topbitset_array,
    "nbt": _r_nbt, "optionalNbt": _r_optional_nbt,
    "anonymousNbt": _r_anon_nbt, "anonOptionalNbt": _r_anon_optional_nbt,
}
_NATIVE_WRITERS = {
    "varint": _w_varint, "varlong": _w_varlong, "bool": _w_bool,
    "UUID": _w_uuid, "void": _w_void, "restBuffer": _w_restbuffer,
    "pstring": _w_pstring, "buffer": _w_buffer, "container": _w_container,
    "array": _w_array, "option": _w_option, "switch": _w_switch,
    "mapper": _w_mapper, "bitfield": _w_bitfield, "bitflags": _w_bitflags,
    "registryEntryHolder": _w_registry_entry_holder,
    "registryEntryHolderSet": _w_registry_entry_holder_set,
    "entityMetadataLoop": _w_entity_metadata_loop,
    "nbt": _w_nbt, "optionalNbt": _w_nbt,
    "anonymousNbt": _w_anon_nbt, "anonOptionalNbt": _w_anon_nbt,
}
for _k in _NUMERIC:
    _NATIVE_READERS[_k] = _r_numeric(_k)
    _NATIVE_WRITERS[_k] = _w_numeric(_k)
