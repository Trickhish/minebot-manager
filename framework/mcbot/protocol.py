"""Loads a vendored protocol schema and turns it into working codecs.

`Protocol` ties a Minecraft version to its packet definitions. For each
(state, direction) it builds a `ProtoDef` over the merged global + local type
tables and exposes `decode` / `encode` for whole packets, which come back as
`(name, params_dict)`.

Directions are named from the *server's* point of view, matching
minecraft-data: `toClient` = we (the client) receive, `toServer` = we send.
"""

from __future__ import annotations

import json
import os

from .buffer import Buffer
from .types import ProtoDef

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "pc")

# minecraft-data folder names for each vendored version. Add a line here (and
# drop the JSON in data/pc/<name>/) to support another version.
STATES = ("handshaking", "status", "login", "configuration", "play")
TO_CLIENT = "toClient"
TO_SERVER = "toServer"


def _load_version_table():
    path = os.path.join(_DATA_DIR, "common", "protocolVersions.json")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    by_name = {}
    by_number = {}
    for row in rows:
        by_name[row["minecraftVersion"]] = row
        by_number.setdefault(row["version"], row)
    return by_name, by_number


_VERSIONS_BY_NAME, _VERSIONS_BY_NUMBER = _load_version_table()


def available_versions():
    """Version folder names actually vendored in data/pc/."""
    return sorted(
        name for name in os.listdir(_DATA_DIR)
        if os.path.isdir(os.path.join(_DATA_DIR, name)) and name != "common"
    )


class Protocol:
    def __init__(self, version: str):
        folder = os.path.join(_DATA_DIR, version)
        if not os.path.isdir(folder):
            raise ValueError(
                f"no vendored data for {version!r}; "
                f"available: {available_versions()}"
            )
        self.version = version
        row = _VERSIONS_BY_NAME.get(version, {})
        self.protocol_version = row.get("version")

        with open(os.path.join(folder, "protocol.json"), encoding="utf-8") as fh:
            self._schema = json.load(fh)
        self._global_types = self._schema.get("types", {})
        # whether this version has the 1.20.2+ configuration state
        self.has_configuration = "configuration" in self._schema

        # (state, direction) -> (ProtoDef, packet_type_def)
        self._codecs: dict = {}

    # -- codec access ------------------------------------------------------
    def _codec(self, state: str, direction: str):
        key = (state, direction)
        cached = self._codecs.get(key)
        if cached is not None:
            return cached
        if state not in self._schema:
            raise ValueError(f"version {self.version} has no state {state!r}")
        local = self._schema[state][direction]["types"]
        merged = {**self._global_types, **local}
        proto = ProtoDef(merged)
        self._codecs[key] = (proto, "packet")
        return self._codecs[key]

    def decode(self, state: str, direction: str, data: bytes):
        """Decode a full packet body (id + params) -> (name, params)."""
        proto, packet_type = self._codec(state, direction)
        buf = Buffer(data)
        result = proto.read(buf, packet_type)
        return result["name"], result.get("params")

    def encode(self, state: str, direction: str, name: str, params: dict) -> bytes:
        """Encode (name, params) into a full packet body (id + params)."""
        proto, packet_type = self._codec(state, direction)
        buf = Buffer()
        proto.write(buf, packet_type, {"name": name, "params": params or {}})
        return buf.getvalue()

    def id_to_name(self, state: str, direction: str) -> dict:
        """int packet id -> packet name for a (state, direction)."""
        proto, _ = self._codec(state, direction)
        mapper_args = proto.types["packet"][1][0]["type"][1]
        return {int(str(k), 0): v for k, v in mapper_args["mappings"].items()}

    def decode_name(self, state: str, direction: str, data: bytes) -> str:
        """Read only the leading packet id and map it to a name (cheap)."""
        buf = Buffer(data)
        pid = buf.read_varint()
        name = self.id_to_name(state, direction).get(pid)
        if name is None:
            raise KeyError(f"unknown {direction} packet id {pid} ({hex(pid)}) in state {state}")
        return name

    def packet_names(self, state: str, direction: str):
        """All packet names available for a (state, direction)."""
        proto, _ = self._codec(state, direction)
        packet_def = proto.types["packet"]
        # container -> field 0 is the mapper on 'name'
        mapper_args = packet_def[1][0]["type"][1]
        return sorted(mapper_args["mappings"].values())
