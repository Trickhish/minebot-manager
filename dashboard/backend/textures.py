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

from mcbot.resourcepack import ResourcePack, _SPECIAL_NAMES
from mcbot.png import decode_png, encode_png

TILE = 16

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
    return None


class TextureAtlas:
    def __init__(self, pack_path: str, cache_dir: str):
        self.pack = ResourcePack(pack_path)
        self.index = self.pack._texture_index          # stem -> arcname
        self.png_path = os.path.join(cache_dir, "atlas.png")
        self._meta_path = os.path.join(cache_dir, "atlas_meta.json")
        self.stem_to_tile: dict[str, int] = {}
        self.cols = self.rows = 0
        os.makedirs(cache_dir, exist_ok=True)
        self._build_or_load()

    # -- build / cache ------------------------------------------------------
    def _build_or_load(self) -> None:
        if os.path.exists(self.png_path) and os.path.exists(self._meta_path):
            meta = json.load(open(self._meta_path, encoding="utf-8"))
            self.stem_to_tile = meta["stems"]
            self.cols, self.rows = meta["cols"], meta["rows"]
            return
        self._build()

    def _build(self) -> None:
        import numpy as np

        stems = sorted(s for s, arc in self.index.items()
                       if "/textures/block/" in arc)
        cols = int(math.ceil(math.sqrt(len(stems)))) or 1
        atlas_rows = int(math.ceil(len(stems) / cols))
        atlas = np.zeros((atlas_rows * TILE, cols * TILE, 3), np.uint8)

        tile = 0
        for stem in stems:
            frame = self._decode_tile(stem, np)
            if frame is None:
                continue
            tint = _tint_for(stem)
            if tint is not None:
                frame = np.clip(frame.astype(np.float32)
                                * (np.array(tint, np.float32) / 255.0), 0, 255).astype(np.uint8)
            r, c = tile // cols, tile % cols
            atlas[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = frame
            self.stem_to_tile[stem] = tile
            tile += 1

        self.cols, self.rows = cols, atlas_rows
        with open(self.png_path, "wb") as fh:
            fh.write(encode_png(atlas))
        with open(self._meta_path, "w", encoding="utf-8") as fh:
            json.dump({"stems": self.stem_to_tile, "cols": cols, "rows": atlas_rows}, fh)

    def _decode_tile(self, stem: str, np):
        """A 16x16x3 uint8 RGB tile for a texture stem (first frame, alpha
        flattened onto the texture's own average color), or None."""
        try:
            w, h, bpp, px = decode_png(self.pack._read(self.index[stem]))
        except Exception:  # noqa: BLE001 - skip anything that won't decode
            return None
        if w < TILE or h < TILE:
            return None
        img = np.frombuffer(px, np.uint8).reshape(h, w, bpp)[:TILE, :TILE]
        rgb = img[:, :, :3].astype(np.float32)
        if bpp == 4:
            alpha = img[:, :, 3:4].astype(np.float32) / 255.0
            opaque = img[:, :, 3] >= 16
            if opaque.any():
                avg = img[:, :, :3][opaque].mean(axis=0)
            else:
                avg = np.array([128, 128, 128], np.float32)
            rgb = rgb * alpha + avg * (1.0 - alpha)
        return np.clip(rgb, 0, 255).astype(np.uint8)

    # -- lookup -------------------------------------------------------------
    def _stem_candidates(self, name: str):
        """Candidate texture stems for a block, most specific first."""
        order, seen = [], set()

        def add(s):
            if s and s not in seen:
                seen.add(s)
                order.append(s)

        add(_SPECIAL_NAMES.get(name, name))
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
        return top, side, bottom
