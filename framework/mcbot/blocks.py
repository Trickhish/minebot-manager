"""Block-state-id -> block-name lookup.

Loads the vendored range table (see `tools/build_block_table.py`) for a
version and resolves a global block state id to its block name via binary
search. Property values (facing, half, waterlogged, ...) are not resolved --
only which block *type* a state id belongs to -- which is what "what block is
this" and "list of nearby blocks" need.
"""

from __future__ import annotations

import bisect
import json
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "pc")

# Versions without their own vendored table borrow the nearest one. New block
# types added after the borrowed version's release will resolve as "unknown".
_FALLBACK = {
    "26.2": "1.21.11",
}

AIR_NAMES = frozenset({"air", "cave_air", "void_air"})

_cache: dict[str, "BlockTable"] = {}


def get_block_table(version: str) -> "BlockTable":
    table = _cache.get(version)
    if table is None:
        table = BlockTable(version)
        _cache[version] = table
    return table


class BlockTable:
    def __init__(self, version: str):
        source_version = version
        path = os.path.join(_DATA_DIR, version, "block_states.json")
        if not os.path.exists(path):
            source_version = _FALLBACK.get(version)
            if source_version is None:
                raise ValueError(
                    f"no block-state table for {version!r} and no fallback "
                    f"registered; vendor one with tools/build_block_table.py")
            path = os.path.join(_DATA_DIR, source_version, "block_states.json")

        with open(path, encoding="utf-8") as fh:
            self._ranges = json.load(fh)  # sorted [minId, maxId, name] triples
        self._starts = [r[0] for r in self._ranges]
        self.version = version
        self.source_version = source_version

    def name_for(self, state_id: int) -> str:
        i = bisect.bisect_right(self._starts, state_id) - 1
        if i < 0:
            return "unknown"
        lo, hi, name = self._ranges[i]
        return name if lo <= state_id <= hi else "unknown"

    def is_air(self, state_id: int) -> bool:
        return self.name_for(state_id) in AIR_NAMES
