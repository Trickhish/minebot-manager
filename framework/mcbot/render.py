"""Top-down minimap rendering from loaded world data, and a minimal PNG writer.

`render_top_down` is vectorized with numpy (per-chunk, not per-pixel) and
caches each chunk's computed surface tile, only recomputing chunks
`world.dirty_chunks` flags as changed (see `world.py`) -- most of a bot's
world is static between frames, so repeated renders of a mostly-unchanged
area become cheap canvas assembly rather than redoing the voxel scan every
call. See the benchmark in the README for what this buys in practice.

numpy is an optional dependency, imported lazily so the rest of the framework
has no hard dependency on it. PNG output (`encode_png`, re-exported here from
`png.py`) is hand-written with stdlib `zlib`/`struct` rather than depending on
Pillow.

Colors optionally come from a user-supplied `ResourcePack` (see
`resourcepack.py`) instead of the built-in approximate table -- pass
`resource_pack=` through to `render_top_down`.

Caches are keyed by `(id(world), id(resource_pack))` and assume one render
loop drives a given World (and resource pack choice) at a time -- true for
this library's expected use (a Client owns one World for its connection's
lifetime; switching packs mid-run just starts a fresh cache rather than
mixing colors from two packs).
"""

from __future__ import annotations

from .blocks import AIR_NAMES
from .colors import get_block_color
from .png import encode_png  # noqa: F401 (re-exported for callers/backcompat)

_air_id_cache: dict[int, "object"] = {}
_color_caches: dict[tuple, dict[int, tuple[int, int, int]]] = {}
_tile_caches: dict[tuple, dict[tuple[int, int], tuple]] = {}


def _air_state_ids(block_table, np):
    cached = _air_id_cache.get(id(block_table))
    if cached is not None:
        return cached
    ids = []
    for lo, hi, name in block_table._ranges:
        if name in AIR_NAMES:
            ids.extend(range(lo, hi + 1))
    arr = np.array(sorted(set(ids)), dtype=np.uint32)
    _air_id_cache[id(block_table)] = arr
    return arr


def _compute_chunk_tile(sections, air_ids, world, color_cache, resource_pack, np):
    """Surface color, height, and block-state ID grids for one chunk column.

    Height is -1 where the column is loaded but has no solid block.
    """
    chunk_ids = np.concatenate(
        [np.frombuffer(s, dtype=np.uint32).reshape(16, 16, 16) for s in sections],
        axis=0)  # (Y, Z, X), bottom section first
    non_air = ~np.isin(chunk_ids, air_ids)
    flipped_ids, flipped_nonair = chunk_ids[::-1], non_air[::-1]
    top_idx = np.argmax(flipped_nonair, axis=0)  # (Z, X)
    has_solid = np.take_along_axis(flipped_nonair, top_idx[None], axis=0)[0]
    surface_ids = np.take_along_axis(flipped_ids, top_idx[None], axis=0)[0]
    surface_y = (chunk_ids.shape[0] - 1) - top_idx + world.min_y

    uniq, inverse = np.unique(surface_ids, return_inverse=True)
    palette = np.empty((len(uniq), 3), dtype=np.uint8)
    for i, uid in enumerate(uniq.tolist()):
        color = color_cache.get(uid)
        if color is None:
            color = get_block_color(world.block_table.name_for(uid), resource_pack)
            color_cache[uid] = color
        palette[i] = color
    chunk_rgb = palette[inverse.reshape(surface_ids.shape)]
    height = np.where(has_solid, surface_y, -1).astype(np.int32)
    return chunk_rgb, height, surface_ids.astype(np.uint32)


def _get_chunk_tiles(world, resource_pack, np):
    cache_key = (id(world), id(resource_pack))
    tiles = _tile_caches.setdefault(cache_key, {})
    color_cache = _color_caches.setdefault(cache_key, {})
    air_ids = _air_state_ids(world.block_table, np)

    for coord in [c for c in tiles if c not in world.chunks]:
        del tiles[coord]  # chunk was unloaded

    # Claim only the entries that existed when this render began. The world
    # worker may mark a coordinate dirty again while its tile is being built;
    # popping first preserves that new mark for the next frame. Clearing the
    # whole set here loses chunk loads/edits that race with rendering.
    dirty_count = len(world.dirty_chunks)
    for _ in range(dirty_count):
        try:
            coord = world.dirty_chunks.pop()
        except KeyError:
            break
        sections = world.chunks.get(coord)
        if sections is not None:
            tiles[coord] = _compute_chunk_tile(sections, air_ids, world, color_cache, resource_pack, np)
    return tiles


def chunk_surface(world, chunk_x: int, chunk_z: int, resource_pack=None):
    """Return cached ``(state_ids, heights)`` for one loaded chunk column."""
    try:
        import numpy as np
    except ImportError as exc:
        raise ImportError("chunk_surface requires numpy: pip install numpy") from exc
    if world.block_table is None:
        raise ValueError("chunk_surface requires a world with a block table")
    tile = _get_chunk_tiles(world, resource_pack, np).get((chunk_x, chunk_z))
    if tile is None:
        return None
    return tile[2], tile[1]


def render_top_down(world, center_x: int, center_z: int, radius: int,
                     bot_position=None, background=(15, 15, 20), void_color=(5, 5, 8),
                     resource_pack=None):
    """A numpy uint8 (H, W, 3) top-down map of the loaded world.

    H = W = 2*radius + 1; pixel (z, x) is the topmost non-air block at that
    column (vanilla-map style -- water/leaves/glass count as opaque, only
    air/cave_air/void_air are see-through), with simple height-difference
    shading between neighboring columns for terrain relief. Unloaded chunks
    render as `background`; loaded columns with no solid block (e.g. void)
    render as `void_color`. `resource_pack`: an optional `ResourcePack` to
    source colors from real textures instead of the built-in approximations.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise ImportError(
            "render_top_down requires numpy: pip install numpy") from exc
    if world.block_table is None:
        raise ValueError("render_top_down requires a world with a block table")

    size = 2 * radius + 1
    rgb_grid = np.empty((size, size, 3), dtype=np.uint8)
    rgb_grid[:, :] = background
    height_grid = np.full((size, size), -2, dtype=np.int32)  # -2 = unloaded

    tiles = _get_chunk_tiles(world, resource_pack, np)
    min_x, max_x = center_x - radius, center_x + radius
    min_z, max_z = center_z - radius, center_z + radius

    for cx in range(min_x >> 4, (max_x >> 4) + 1):
        for cz in range(min_z >> 4, (max_z >> 4) + 1):
            tile = tiles.get((cx, cz))
            if tile is None:
                continue
            chunk_rgb, chunk_height, _surface_ids = tile

            base_x, base_z = cx * 16, cz * 16
            clip_x0, clip_x1 = max(0, min_x - base_x), min(16, max_x - base_x + 1)
            clip_z0, clip_z1 = max(0, min_z - base_z), min(16, max_z - base_z + 1)
            if clip_x0 >= clip_x1 or clip_z0 >= clip_z1:
                continue

            gz0, gx0 = base_z + clip_z0 - min_z, base_x + clip_x0 - min_x
            gz1, gx1 = gz0 + (clip_z1 - clip_z0), gx0 + (clip_x1 - clip_x0)

            h_slice = chunk_height[clip_z0:clip_z1, clip_x0:clip_x1]
            solid = h_slice > -1
            rgb_grid[gz0:gz1, gx0:gx1] = np.where(
                solid[:, :, None], chunk_rgb[clip_z0:clip_z1, clip_x0:clip_x1], void_color)
            height_grid[gz0:gz1, gx0:gx1] = np.where(solid, h_slice, -1)

    _apply_height_shading(rgb_grid, height_grid, np)
    if bot_position is not None:
        _draw_marker(rgb_grid, bot_position, min_x, min_z, size)
    return rgb_grid


def _apply_height_shading(rgb_grid, height_grid, np) -> None:
    """Lighten columns taller than their northward neighbor, darken shorter
    ones -- cheap terrain relief, same idea vanilla in-game maps use."""
    valid = height_grid > -2
    neighbor_height = np.roll(height_grid, shift=1, axis=0)
    neighbor_valid = np.roll(valid, shift=1, axis=0)
    compare_ok = valid & neighbor_valid
    diff = height_grid - neighbor_height

    shade = np.ones(height_grid.shape, dtype=np.float32)
    shade[compare_ok & (diff > 0)] = 1.15
    shade[compare_ok & (diff < 0)] = 0.85
    shaded = np.clip(rgb_grid.astype(np.float32) * shade[:, :, None], 0, 255)
    rgb_grid[:] = shaded.astype(np.uint8)


def _draw_marker(rgb_grid, bot_position, min_x, min_z, size, color=(255, 0, 0)) -> None:
    gx = int(bot_position[0]) - min_x
    gz = int(bot_position[2]) - min_z
    for dz, dx in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        y, x = gz + dz, gx + dx
        if 0 <= y < size and 0 <= x < size:
            rgb_grid[y, x] = color
