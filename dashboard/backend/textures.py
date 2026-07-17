"""Build a block-texture atlas from a user-supplied resource pack, for the
browser first-person view.

We don't ship Mojang textures (see mcbot.resourcepack); this reads a pack the
user owns and packs each block texture's first 16x16 frame into one atlas PNG,
plus a stem->tile-index map. The bot-host serves the atlas once; the voxels
endpoint tags each palette block with its top/side/bottom tile indices, and the
WebGL renderer UV-maps the cube faces. The atlas is cached to disk so it's only
built once.

Grayscale foliage textures (grass/leaves, tinted by biome at runtime in-game)
are given a fixed green tint here so they don't render as flat gray. Cutout
textures (leaves/glass) are flattened onto their own average color since the
atlas is opaque RGB.
"""

from __future__ import annotations

import json
import math
import os
import threading

from mcbot.resourcepack import ResourcePack, _SPECIAL_NAMES
from mcbot.png import decode_png, encode_png

TILE = 16

# Atlas on-disk format tag; bump to invalidate cached atlas.png/atlas_meta.json
# when the pixel format or contents change.
_ATLAS_FORMAT = "rgba2-items"

# "Shape" blocks share their material's texture (a slab/stairs/fence of oak
# uses oak_planks, a cobblestone_wall uses cobblestone, ...). Strip one of these
# to recover the material name, then resolve the material to a real texture stem.
_SHAPE_SUFFIXES = (
    "_slab", "_stairs", "_wall_hanging_sign", "_hanging_sign", "_wall_sign",
    "_sign", "_wall", "_fence_gate", "_fence", "_pressure_plate", "_button",
    "_trapdoor", "_door", "_carpet", "_pane",
)


# Prefixes that don't change a block's texture (waxed copper = copper texture,
# infested stone = stone texture): strip and resolve the remainder.
_TRANSPARENT_PREFIXES = ("waxed_", "infested_")


def _material_variants(m: str):
    """Texture-stem guesses for a material name, most specific first. Covers the
    common naming gaps: wood -> planks, _wood -> _log, _hyphae -> _stem,
    <color>_carpet -> <color>_wool, brick -> bricks (plural), purpur -> purpur_block,
    snow_block -> snow."""
    yield m
    if m.endswith("_wood"):
        yield m[:-5] + "_log"
    elif m.endswith("_hyphae"):
        yield m[:-7] + "_stem"
    if m.endswith("_block"):
        yield m[:-6]         # snow_block -> snow, magma_block -> magma
    if not m.endswith("_planks"):
        yield m + "_planks"
    yield m + "_block"       # purpur/quartz (slab/stairs) -> purpur_block/quartz_block
    yield m + "s"            # brick -> bricks, stone_brick -> stone_bricks
    yield m + "_wool"        # <color>_carpet material -> <color>_wool

# Foliage textures are grayscale in the pack (biome-tinted in-game). Multiply
# by a representative green so they don't come out gray.
_LEAF_TINT = (72, 105, 45)
_GRASS_TINT = (104, 160, 72)
# water_still/water_flow are grayscale in the pack (biome-tinted in-game).
# Multiply by the default water color (#3F76E4) so it looks like water, not gray.
_WATER_TINT = (63, 118, 228)
_FOLIAGE = {
    "grass_block_top": _GRASS_TINT, "short_grass": _GRASS_TINT, "grass": _GRASS_TINT,
    "tall_grass_top": _GRASS_TINT, "tall_grass_bottom": _GRASS_TINT,
    "fern": _GRASS_TINT, "large_fern_top": _GRASS_TINT, "large_fern_bottom": _GRASS_TINT,
    "lily_pad": _LEAF_TINT, "vine": _LEAF_TINT, "sugar_cane": _GRASS_TINT,
}


def _tint_for(stem: str):
    if stem in _FOLIAGE:
        return _FOLIAGE[stem]
    if stem.endswith("_leaves"):
        return _LEAF_TINT
    if stem.startswith("water_"):
        return _WATER_TINT
    return None


class TextureAtlas:
    def __init__(self, pack_path: str, cache_dir: str):
        self.pack = ResourcePack(pack_path)
        self.index = self.pack._texture_index          # stem -> arcname
        self.png_path = os.path.join(cache_dir, "atlas.png")
        self._meta_path = os.path.join(cache_dir, "atlas_meta.json")
        self.stem_to_tile: dict[str, int] = {}
        self.item_to_tile: dict[str, int] = {}
        self._face_tiles_cache: dict[str, tuple[int, int, int]] = {}
        self.cols = self.rows = 0
        self._atlas_rgba = None
        self._atlas_lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)
        self._build_or_load()
        self.version = os.stat(self.png_path).st_mtime_ns

    # -- build / cache ------------------------------------------------------
    def _build_or_load(self) -> None:
        if os.path.exists(self.png_path) and os.path.exists(self._meta_path):
            meta = json.load(open(self._meta_path, encoding="utf-8"))
            if meta.get("format") == _ATLAS_FORMAT:
                self.stem_to_tile = meta["stems"]
                self.item_to_tile = meta.get("items", {})
                self.cols, self.rows = meta["cols"], meta["rows"]
                return
        self._build()

    def _build(self) -> None:
        import numpy as np

        stems = sorted(s for s, arc in self.index.items()
                       if "/textures/block/" in arc)
        item_index = {}
        for arc in self.pack._all_names():
            if not arc.lower().endswith(".png") or "/textures/item/" not in arc:
                continue
            item_index.setdefault(os.path.splitext(os.path.basename(arc))[0], arc)
        item_stems = sorted(item_index)
        tile_count = len(stems) + len(item_stems)
        cols = int(math.ceil(math.sqrt(tile_count))) or 1
        atlas_rows = int(math.ceil(tile_count / cols))
        atlas = np.zeros((atlas_rows * TILE, cols * TILE, 4), np.uint8)

        tile = 0
        for stem in stems:
            frame = self._decode_tile(stem, np)
            if frame is None:
                continue
            tint = _tint_for(stem)
            if tint is not None:
                rgb = np.clip(frame[:, :, :3].astype(np.float32)
                              * (np.array(tint, np.float32) / 255.0), 0, 255)
                frame = np.dstack([rgb.astype(np.uint8), frame[:, :, 3]])
            r, c = tile // cols, tile % cols
            atlas[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = frame
            self.stem_to_tile[stem] = tile
            tile += 1

        for stem in item_stems:
            frame = self._decode_arc(item_index[stem], np)
            if frame is None:
                continue
            r, c = tile // cols, tile % cols
            atlas[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = frame
            self.item_to_tile[stem] = tile
            tile += 1

        self.cols, self.rows = cols, atlas_rows
        with open(self.png_path, "wb") as fh:
            fh.write(encode_png(atlas))
        with open(self._meta_path, "w", encoding="utf-8") as fh:
            json.dump({"stems": self.stem_to_tile, "items": self.item_to_tile,
                       "cols": cols,
                       "rows": atlas_rows, "format": _ATLAS_FORMAT}, fh)

    def _decode_tile(self, stem: str, np):
        """A 16x16x4 uint8 RGBA tile for a texture stem (first frame), or None.
        Alpha is preserved so the WebGL shader can cut out transparent pixels
        (torches, rails, plants); fully opaque textures get alpha 255."""
        try:
            return self._decode_arc(self.index[stem], np)
        except Exception:  # noqa: BLE001 - skip anything that won't decode
            return None

    def _decode_arc(self, arc: str, np):
        w, h, bpp, px = decode_png(self.pack._read(arc))
        if w < TILE or h < TILE:
            return None
        img = np.frombuffer(px, np.uint8).reshape(h, w, bpp)[:TILE, :TILE]
        rgba = np.empty((TILE, TILE, 4), np.uint8)
        rgba[:, :, :3] = img[:, :, :3]
        rgba[:, :, 3] = img[:, :, 3] if bpp == 4 else 255
        return rgba

    # -- lookup -------------------------------------------------------------
    def _stem_candidates(self, name: str):
        """Candidate texture stems for a block, most specific first."""
        order, seen = [], set()

        def add(s):
            if s and s not in seen:
                seen.add(s)
                order.append(s)

        add(_SPECIAL_NAMES.get(name, name))
        # Wall torches have no texture of their own -- they reuse the standing
        # torch texture (wall_torch -> torch, soul_wall_torch -> soul_torch).
        if name == "wall_torch" or name.endswith("_wall_torch"):
            add(name.replace("wall_torch", "torch"))
        # Resolve the name and any texture-preserving-prefix-stripped form.
        bases = [name]
        for pfx in _TRANSPARENT_PREFIXES:
            if name.startswith(pfx):
                bases.append(name[len(pfx):])
        for bn in bases:
            for suf in _SHAPE_SUFFIXES:   # slab/stairs/fence/... -> material texture
                if bn.endswith(suf):
                    for v in _material_variants(bn[: -len(suf)]):
                        add(v)
                    break
            for v in _material_variants(bn):   # also handles _wood/_hyphae directly
                add(v)
        return order

    def face_tiles(self, name: str):
        """(top, side, bottom) atlas tile indices for a block name; -1 where no
        texture resolves (renderer falls back to the flat color there)."""
        cached = self._face_tiles_cache.get(name)
        if cached is not None:
            return cached
        cands = self._stem_candidates(name)

        def pick(suffixes):
            for c in cands:
                for suf in suffixes:
                    tile = self.stem_to_tile.get(c + suf)
                    if tile is not None:
                        return tile
            return -1

        top = pick(("_top", ""))
        side = pick(("_side", ""))
        if name == "grass_block":  # bottom of grass is dirt, not the green top
            bottom = self.stem_to_tile.get("dirt", -1)
        else:
            bottom = pick(("_bottom", "_top", ""))
        result = (top, side, bottom)
        self._face_tiles_cache[name] = result
        return result

    def tile_rgba(self, tile: int):
        """Return one atlas tile as a cached 16x16x4 numpy view."""
        if tile < 0:
            return None
        if self._atlas_rgba is None:
            with self._atlas_lock:
                if self._atlas_rgba is None:
                    import numpy as np
                    with open(self.png_path, "rb") as fh:
                        width, height, bpp, pixels = decode_png(fh.read())
                    image = np.frombuffer(pixels, np.uint8).reshape(height, width, bpp)
                    if bpp == 3:
                        alpha = np.full((height, width, 1), 255, np.uint8)
                        image = np.concatenate((image, alpha), axis=2)
                    self._atlas_rgba = image
        row, col = divmod(tile, self.cols)
        return self._atlas_rgba[
            row * TILE:(row + 1) * TILE,
            col * TILE:(col + 1) * TILE,
        ]
