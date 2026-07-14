"""Inventory state tracking + slot encoding.

The wire "Slot" shape changed twice across the versions we support:
  * 1.8-ish        : {blockId, itemCount, itemDamage, nbtData}; blockId=-1 is empty
  * 1.9-1.20.4-ish  : {present, itemId, itemCount, nbtData}; present=False is empty
  * 1.20.5+         : {itemCount, itemId, addedComponentCount, ..., components};
                       itemCount=0 is empty

All three happen to use the literal field name "itemCount" for a non-empty
stack's size, and never populate it for an empty slot -- so `normalize_slot`
can detect "empty" the same way regardless of which shape it decoded from.
Building a slot to *send*, however, must match the exact shape the target
version expects, so `make_slot_value` branches on the resolved type's field
names (passed in by the caller, which has protocol access).
"""

from __future__ import annotations


def normalize_slot(raw: dict | None, item_table) -> dict | None:
    """A decoded Slot dict (any wire shape) -> {item_id, name, count, raw}, or
    None for an empty slot."""
    if raw is None or raw.get("itemCount", 0) == 0:
        return None
    item_id = raw.get("itemId", raw.get("blockId"))
    name = item_table.name_for(item_id) if item_table and item_id is not None else None
    return {"item_id": item_id, "name": name, "count": raw["itemCount"], "raw": raw}


def make_slot_value(field_names: set, item_id: int, count: int) -> dict:
    """Build a Slot-shaped dict to send, matching whichever wire shape
    `field_names` (the resolved type's outer field names) indicates."""
    if count <= 0:
        return make_empty_slot_value(field_names)
    if "blockId" in field_names:
        return {"blockId": item_id, "itemCount": count, "itemDamage": 0, "nbtData": None}
    if "present" in field_names:
        return {"present": True, "itemId": item_id, "itemCount": count, "nbtData": None}
    return {"itemCount": count, "itemId": item_id, "addedComponentCount": 0,
            "removedComponentCount": 0, "components": [], "removeComponents": []}


def make_empty_slot_value(field_names: set) -> dict:
    if "blockId" in field_names:
        return {"blockId": -1}
    if "present" in field_names:
        return {"present": False}
    return {"itemCount": 0}


class Inventory:
    HOTBAR_START = 36  # standard player-inventory window (id 0) slot layout

    def __init__(self, item_table):
        self.item_table = item_table
        self.windows: dict[int, dict[int, dict | None]] = {}
        self.window_id = 0  # the player's own inventory is always "window 0"
        self.state_id = 0
        self.cursor_item: dict | None = None
        self.held_slot = 0  # hotbar index, 0-8
        self.open_window_info: tuple | None = None  # (id, type, title) if non-0 open

    def set_window_items(self, window_id, state_id, items_raw, carried_raw) -> None:
        self.state_id = state_id
        self.windows[window_id] = {
            i: normalize_slot(raw, self.item_table) for i, raw in enumerate(items_raw)
        }
        self.cursor_item = normalize_slot(carried_raw, self.item_table)

    def set_slot(self, window_id, state_id, slot_index, item_raw) -> None:
        if window_id == -1 and slot_index == -1:
            self.cursor_item = normalize_slot(item_raw, self.item_table)
            return
        self.state_id = state_id
        self.windows.setdefault(window_id, {})[slot_index] = normalize_slot(
            item_raw, self.item_table)

    def open_window(self, window_id, inventory_type, title) -> None:
        self.window_id = window_id
        self.open_window_info = (window_id, inventory_type, title)

    def close_window(self) -> None:
        self.window_id = 0
        self.open_window_info = None

    def items_in(self, window_id: int | None = None) -> list[tuple[int, dict]]:
        """(slot_index, item) for every occupied slot in a window (default:
        whichever window is currently open)."""
        wid = self.window_id if window_id is None else window_id
        slots = self.windows.get(wid, {})
        return [(i, item) for i, item in sorted(slots.items()) if item is not None]

    def held_item(self) -> dict | None:
        return self.windows.get(0, {}).get(self.HOTBAR_START + self.held_slot)
