#!/usr/bin/env python3
"""Vendor a compact block-state-id -> name table from minecraft-data.

minecraft-data's blocks.json lists ~1000 block *types*, each owning a
contiguous range of state ids (one id per property combination, e.g. stair
facing/half/shape). For a "what block is here" lookup we don't need the
individual property values -- just which block type a state id belongs to --
so we vendor it as a sorted list of [minStateId, maxStateId, name] ranges
instead of copying the full upstream schema.

Usage: python tools/build_block_table.py <version> <mcdata_blocks_subpath>
  e.g. python tools/build_block_table.py 1.18.2 1.18
       python tools/build_block_table.py 1.21.11 1.21.11
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "mcbot", "data", "pc")
RAW = "https://raw.githubusercontent.com/PrismarineJS/minecraft-data/master/data/pc"


def build(version: str, mcdata_subpath: str):
    url = f"{RAW}/{mcdata_subpath}/blocks.json"
    blocks = json.loads(urllib.request.urlopen(url, timeout=60).read())

    ranges = sorted(
        ([b["minStateId"], b["maxStateId"], b["name"]] for b in blocks),
        key=lambda r: r[0],
    )
    # sanity: ranges should be contiguous and non-overlapping (id 0 is air)
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
    build(sys.argv[1], sys.argv[2])
