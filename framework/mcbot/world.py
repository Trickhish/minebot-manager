"""In-memory world model: loaded chunks + block lookups.

Stores each loaded chunk column as a list of 16x16x16 sections (bottom to
top), each an `array('I', 4096)` of global block state ids -- compact enough
to hold a real view distance in memory. `set_block_state` applies the
incremental updates from `block_change` / `multi_block_change` packets.

Only supports the modern (1.18+) chunk section format (see `chunk.py`); 1.8's
pre-flattening chunk format is not implemented.

The world's vertical origin (`min_y`) is assumed to be the vanilla default
(-64) since the actual dimension height isn't threaded through from the
registry data. Custom dimensions with a different height will resolve blocks
at the wrong Y -- a known limitation.
"""

from __future__ import annotations

from .blocks import AIR_NAMES as _AIR_NAMES
from .blocks import BlockTable
from .chunk import parse_chunk_column

SECTION_SIZE = 16


class World:
    def __init__(self, block_table: BlockTable | None, protocol_version: int, min_y: int = -64):
        self.block_table = block_table
        self.protocol_version = protocol_version
        self.min_y = min_y
        self.chunks: dict[tuple[int, int], list] = {}
        # Chunk coords touched since the last render (new load or block edit).
        # A renderer can cache per-chunk work and only redo it for these.
        self.dirty_chunks: set[tuple[int, int]] = set()

    # -- loading -------------------------------------------------------
    def load_chunk(self, chunk_x: int, chunk_z: int, raw_chunk_data: bytes) -> None:
        self.chunks[(chunk_x, chunk_z)] = parse_chunk_column(raw_chunk_data, self.protocol_version)
        self.dirty_chunks.add((chunk_x, chunk_z))

    def unload_chunk(self, chunk_x: int, chunk_z: int) -> None:
        self.chunks.pop((chunk_x, chunk_z), None)
        self.dirty_chunks.add((chunk_x, chunk_z))

    # -- indexing --------------------------------------------------------
    def _locate(self, x: int, y: int, z: int):
        """(sections, section_index, local_index) for a block, or None."""
        sections = self.chunks.get((x >> 4, z >> 4))
        if sections is None:
            return None
        section_index = (y - self.min_y) >> 4
        if not (0 <= section_index < len(sections)):
            return None
        lx, ly, lz = x & 15, (y - self.min_y) & 15, z & 15
        return sections, section_index, lx + lz * SECTION_SIZE + ly * SECTION_SIZE * SECTION_SIZE

    # -- queries -----------------------------------------------------------
    def block_state_at(self, x: int, y: int, z: int) -> int | None:
        loc = self._locate(x, y, z)
        if loc is None:
            return None
        sections, section_index, idx = loc
        return sections[section_index][idx]

    def block_name_at(self, x: int, y: int, z: int) -> str | None:
        state = self.block_state_at(x, y, z)
        if state is None or self.block_table is None:
            return None
        return self.block_table.name_for(state)

    def set_block_state(self, x: int, y: int, z: int, state_id: int) -> None:
        loc = self._locate(x, y, z)
        if loc is None:
            return  # chunk not loaded; drop the update
        sections, section_index, idx = loc
        sections[section_index][idx] = state_id
        self.dirty_chunks.add((x >> 4, z >> 4))

    def voxel_box(self, center_x: int, center_y: int, center_z: int,
                  radius: int, up: int, down: int):
        """A dense numpy grid of block-state ids for the axis-aligned box around
        (center_x, center_y, center_z).

        Returns ``(origin, dims, ids)`` where ``origin`` is the (min x, y, z)
        world corner, ``dims`` is ``(nx, ny, nz)``, and ``ids`` is a
        ``(ny, nz, nx)`` uint32 array (flatten C-order to iterate y, then z,
        then x). Unloaded/out-of-range cells read as state id 0 (air). Built by
        copying the overlapping chunk-section slices -- no per-block Python.
        """
        import numpy as np  # lazy: numpy is an optional dependency

        ox, oz = center_x - radius, center_z - radius
        oy = center_y - down
        nx = nz = 2 * radius + 1
        ny = up + down + 1
        ids = np.zeros((ny, nz, nx), dtype=np.uint32)

        cx0, cx1 = ox >> 4, (ox + nx - 1) >> 4
        cz0, cz1 = oz >> 4, (oz + nz - 1) >> 4
        for cx in range(cx0, cx1 + 1):
            for cz in range(cz0, cz1 + 1):
                sections = self.chunks.get((cx, cz))
                if sections is None:
                    continue
                base_x, base_z = cx * 16, cz * 16
                # horizontal overlap of this chunk column with the box
                gx0, gx1 = max(ox, base_x), min(ox + nx, base_x + 16)
                gz0, gz1 = max(oz, base_z), min(oz + nz, base_z + 16)
                if gx0 >= gx1 or gz0 >= gz1:
                    continue
                sx0, dx0 = gx0 - base_x, gx0 - ox
                sz0, dz0 = gz0 - base_z, gz0 - oz
                w, d = gx1 - gx0, gz1 - gz0
                for si, section in enumerate(sections):
                    sy_base = self.min_y + si * SECTION_SIZE
                    gy0, gy1 = max(oy, sy_base), min(oy + ny, sy_base + SECTION_SIZE)
                    if gy0 >= gy1:
                        continue
                    sy0, dy0 = gy0 - sy_base, gy0 - oy
                    h = gy1 - gy0
                    block = np.frombuffer(section, dtype=np.uint32).reshape(16, 16, 16)
                    ids[dy0:dy0 + h, dz0:dz0 + d, dx0:dx0 + w] = \
                        block[sy0:sy0 + h, sz0:sz0 + d, sx0:sx0 + w]
        return (ox, oy, oz), (nx, ny, nz), ids

    def nearby_blocks(self, x: float, y: float, z: float, radius: int,
                       include_air: bool = False) -> list[tuple[int, int, int, str]]:
        """Block name + position for every loaded block within a cube of the
        given radius around (x, y, z)."""
        if self.block_table is None:
            return []
        x0, y0, z0 = int(x), int(y), int(z)
        out = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    bx, by, bz = x0 + dx, y0 + dy, z0 + dz
                    state = self.block_state_at(bx, by, bz)
                    if state is None:
                        continue
                    name = self.block_table.name_for(state)
                    if not include_air and name in _AIR_NAMES:
                        continue
                    out.append((bx, by, bz, name))
        return out
