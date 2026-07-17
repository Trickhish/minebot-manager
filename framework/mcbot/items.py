"""Item-id -> item-name lookup. Same idea as `blocks.py` but items are simpler
(one id per item, no property-combination states) so a plain dict suffices.
"""

from __future__ import annotations

import json
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "pc")

# Versions without their own vendored table borrow the nearest one. New item
# types added after the borrowed version's release will resolve as "unknown".
_FALLBACK: dict[str, str] = {}

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

    @classmethod
    def from_registry(cls, version: str, entries: list[dict]) -> "ItemTable":
        """Build an item-id table from the server's configuration registry.

        Modern servers can reorder registries through data packs or protocol
        translation. Item stack IDs refer to the received entry order, not a
        client-side vanilla table.
        """
        table = cls.__new__(cls)
        table._by_id = {
            index: entry["key"].removeprefix("minecraft:")
            for index, entry in enumerate(entries)
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
        }
        table._by_name = {name: item_id for item_id, name in table._by_id.items()}
        table.version = version
        table.source_version = "server_registry"
        return table

    def name_for(self, item_id: int) -> str:
        return self._by_id.get(item_id, "unknown")

    def id_for(self, name: str) -> int | None:
        return self._by_name.get(name)
