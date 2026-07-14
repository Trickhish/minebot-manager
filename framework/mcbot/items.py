"""Item-id -> item-name lookup. Same idea as `blocks.py` but items are simpler
(one id per item, no property-combination states) so a plain dict suffices.
"""

from __future__ import annotations

import json
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "pc")

# Versions without their own vendored table borrow the nearest one. New item
# types added after the borrowed version's release will resolve as "unknown".
_FALLBACK = {
    "26.2": "1.21.11",
}

_cache: dict[str, "ItemTable"] = {}


def get_item_table(version: str) -> "ItemTable":
    table = _cache.get(version)
    if table is None:
        table = ItemTable(version)
        _cache[version] = table
    return table


class ItemTable:
    def __init__(self, version: str):
        source_version = version
        path = os.path.join(_DATA_DIR, version, "items.json")
        if not os.path.exists(path):
            source_version = _FALLBACK.get(version)
            if source_version is None:
                raise ValueError(
                    f"no item table for {version!r} and no fallback "
                    f"registered; vendor one with tools/build_item_table.py")
            path = os.path.join(_DATA_DIR, source_version, "items.json")

        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)  # {"id_str": name}
        self._by_id = {int(k): v for k, v in raw.items()}
        self._by_name = {v: k for k, v in self._by_id.items()}
        self.version = version
        self.source_version = source_version

    def name_for(self, item_id: int) -> str:
        return self._by_id.get(item_id, "unknown")

    def id_for(self, name: str) -> int | None:
        return self._by_name.get(name)
