#!/usr/bin/env python3
"""Generate a protocol schema for a Minecraft version newer than minecraft-data.

minecraft-data lags the newest release by a version or two. This tool bridges
the gap by combining two sources:

  * field layouts  -- from an existing minecraft-data schema (the "base"), and
  * packet ordering -- from ViaVersion's Java packet enums for the new version,

which is enough to speak versions that only reordered / inserted packets (the
common case for a minor bump) without hand-writing anything.

How the name reconciliation works: ViaVersion uses vanilla packet names while
minecraft-data uses its own legacy names. Because ViaVersion's enum for the
*base* version and minecraft-data's base schema describe the same version in
the same wire order, zipping them yields a vanilla<->mcdata name dictionary for
free. We then translate the new version's enum through that dictionary.

Only the Play state is rebuilt; handshake/status/login/configuration are copied
from the base (those rarely move between adjacent versions -- verify per bump).

Currently wired up for: base 1.21.11 (774) -> target 26.2 (776).
Enum sources are cached under tools/_viaversion_cache/ so re-runs are offline.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "mcbot", "data", "pc")
CACHE = os.path.join(HERE, "_viaversion_cache")
VIA_RAW = ("https://raw.githubusercontent.com/ViaVersion/ViaVersion/master/"
           "common/src/main/java/com/viaversion/viaversion/protocols")

# Per target: base schema + the ViaVersion enum files (path relative to VIA_RAW)
# for the base version (old wire order) and the target version (new wire order).
CONFIG = {
    "26.2": {
        "base": "1.21.11",
        "protocol_number": 776,
        "enums": {
            "toServer": {
                "base": "v1_21_5to1_21_6/packet/ServerboundPackets1_21_6.java",
                "target": "v1_21_11to26_1/packet/ServerboundPackets26_1.java",
            },
            "toClient": {
                "base": "v1_21_9to1_21_11/packet/ClientboundPackets1_21_11.java",
                "target": "v1_21_11to26_1/packet/ClientboundPackets26_1.java",
            },
        },
    },
}


def fetch(rel_path: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    cache_file = os.path.join(CACHE, rel_path.replace("/", "__"))
    if os.path.exists(cache_file):
        return open(cache_file, encoding="utf-8").read()
    url = f"{VIA_RAW}/{rel_path}"
    text = urllib.request.urlopen(url, timeout=60).read().decode("utf-8")
    with open(cache_file, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def parse_enum(text: str) -> list[str]:
    """Ordered lower-cased enum constant names; ordinal == packet id."""
    body = text.split("{", 1)[1]
    names = []
    for line in body.splitlines():
        if re.match(r"\s*(public|private|protected|@|final|static|void|})", line):
            break
        m = re.match(r"\s*([A-Z][A-Z0-9_]+)\s*(,|;|\()", line)
        if m:
            names.append(m.group(1).lower())
    return names


def ordered_packets(schema, state, direction):
    mapper = schema[state][direction]["types"]["packet"][1][0]["type"][1]["mappings"]
    m = {int(str(k), 0): v for k, v in mapper.items()}
    return [m[i] for i in range(len(m))]


def build(target: str):
    cfg = CONFIG[target]
    base_dir = os.path.join(DATA, cfg["base"])
    with open(os.path.join(base_dir, "protocol.json"), encoding="utf-8") as fh:
        schema = json.load(fh)

    for direction in ("toServer", "toClient"):
        via_base = parse_enum(fetch(cfg["enums"][direction]["base"]))
        via_target = parse_enum(fetch(cfg["enums"][direction]["target"]))
        mcd_base = ordered_packets(schema, "play", direction)

        if len(via_base) != len(mcd_base):
            raise SystemExit(
                f"{direction}: base enum ({len(via_base)}) and mcdata "
                f"({len(mcd_base)}) disagree; cannot align")
        vanilla_to_mcd = dict(zip(via_base, mcd_base))

        _rebuild_play(schema, direction, via_target, vanilla_to_mcd)

    schema["_generated"] = {
        "note": "Play state rebuilt from ViaVersion enums; layouts from base.",
        "base_version": cfg["base"], "target_version": target,
        "protocol_version": cfg["protocol_number"],
    }
    out_dir = os.path.join(DATA, target)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "protocol.json"), "w", encoding="utf-8") as fh:
        json.dump(schema, fh)
    print(f"wrote {out_dir}/protocol.json")


def _rebuild_play(schema, direction, via_target, vanilla_to_mcd):
    types = schema["play"][direction]["types"]
    packet_container = types["packet"][1]
    mapper_args = packet_container[0]["type"][1]
    switch_args = packet_container[1]["type"][1]

    new_mappings = {}
    new = renamed = 0
    for pid, vanilla in enumerate(via_target):
        mcd_name = vanilla_to_mcd.get(vanilla)
        if mcd_name is None:
            # A packet that didn't exist in the base version. Keep it decodable
            # (identifiable by name) with a catch-all body.
            mcd_name = vanilla
            body_key = f"packet_{vanilla}"
            types[body_key] = ["container", [{"name": "data", "type": "restBuffer"}]]
            switch_args["fields"][mcd_name] = body_key
            new += 1
        else:
            renamed += 1
        new_mappings[hex(pid)] = mcd_name

    mapper_args["mappings"] = new_mappings
    print(f"  play/{direction}: {renamed} remapped, {new} new packets "
          f"({len(via_target)} total)")


if __name__ == "__main__":
    import sys
    build(sys.argv[1] if len(sys.argv) > 1 else "26.2")
