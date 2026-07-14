#!/usr/bin/env python3
"""Vendor a block-state-id -> name table from a Minecraft server's own data
reports, for versions minecraft-data doesn't cover (e.g. 26.x).

Mojang's server jar can emit an authoritative block registry:

    java -DbundlerMainClass=net.minecraft.data.Main -jar <server>.jar --reports --server

which writes generated/reports/blocks.json, mapping each block to its states
(with the exact global state ids the server uses on the wire). This tool
collapses that into the same compact [minStateId, maxStateId, name] range table
build_block_table.py produces from minecraft-data, so mcbot can resolve blocks
for that exact version instead of borrowing a mismatched table.

Usage: python tools/build_block_table_from_reports.py <version> <reports_blocks.json>
  e.g. python tools/build_block_table_from_reports.py 26.2 /tmp/blocks.json
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "mcbot", "data", "pc")


def build(version: str, reports_path: str) -> None:
    with open(reports_path, encoding="utf-8") as fh:
        blocks = json.load(fh)

    ranges = []
    for key, blk in blocks.items():
        name = key.split(":", 1)[1] if ":" in key else key  # drop "minecraft:"
        ids = [state["id"] for state in blk["states"]]
        ranges.append([min(ids), max(ids), name])
    ranges.sort(key=lambda r: r[0])

    # sanity: contiguous, non-overlapping ranges (id 0 is air)
    for prev, cur in zip(ranges, ranges[1:]):
        assert prev[1] < cur[0], f"overlapping state ranges: {prev} / {cur}"

    out_dir = os.path.join(DATA, version)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "block_states.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(ranges, fh)
    print(f"wrote {out_path} ({len(ranges)} block types, "
          f"max state id {ranges[-1][1]})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2])
