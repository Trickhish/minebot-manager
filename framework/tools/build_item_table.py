#!/usr/bin/env python3
"""Vendor a compact item-id -> name table from minecraft-data.

Stored as a plain {id: name} dict (JSON keys are strings; loaded back as
ints). Unlike blocks, items.json ids are one-per-item rather than a range per
type -- but they aren't always contiguous (1.8's legacy numbering has gaps),
so a dict is used rather than assuming a dense array.

Usage: python tools/build_item_table.py <version> <mcdata_items_subpath>
  e.g. python tools/build_item_table.py 1.18.2 1.18
       python tools/build_item_table.py 1.21.11 1.21.11
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
    url = f"{RAW}/{mcdata_subpath}/items.json"
    items = json.loads(urllib.request.urlopen(url, timeout=60).read())

    by_id = {str(item["id"]): item["name"] for item in items}

    out_dir = os.path.join(DATA, version)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "items.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(by_id, fh)
    print(f"wrote {out_path} ({len(by_id)} items)")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
