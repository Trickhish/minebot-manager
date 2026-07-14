"""Load block colors from a user-supplied Minecraft resource pack instead of
the built-in approximate color table.

This is the answer to "don't infringe copyrights": we don't vendor Mojang's
textures. Instead, point this at a resource pack *you* legally own (extracted
from your own game install, or any Creative-Commons pack) -- a zip file or an
already-extracted directory, same as the game itself accepts -- and colors are
computed from its actual texture files at runtime. Nothing from the pack is
copied or redistributed; it's just read locally, like the vanilla client does.

Per block, the *average* color of its texture is used (alpha-weighted: fully
transparent pixels are excluded) since rendering is a flat per-block-color
minimap, not textured 3D -- this is a summary color, not the texture itself.
Animated textures (water, lava, ...) are stored as multiple frames stacked
vertically in one PNG; averaging the whole file blends all frames together,
which is a reasonable stand-in color but not a single frame's exact color --
a deliberate simplification, not a bug.
"""

from __future__ import annotations

import os
import zipfile

from .png import UnsupportedPNG, decode_png

# Blocks whose texture file doesn't share the block's own name.
_SPECIAL_NAMES = {"water": "water_still", "lava": "lava_still"}

# Block-name suffixes that don't have their own texture -- strip and retry
# against the base material (e.g. "oak_stairs" -> "oak_planks" via "oak").
_STRIP_SUFFIXES = (
    "_stairs", "_slab", "_wall", "_fence_gate", "_fence", "_door", "_trapdoor",
    "_pressure_plate", "_button", "_sign", "_hanging_sign",
)


class ResourcePack:
    def __init__(self, path: str):
        self.path = path
        self._zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        self._texture_index = self._build_index()
        self._color_cache: dict[str, tuple[int, int, int] | None] = {}

    def _all_names(self):
        if self._zip is not None:
            return self._zip.namelist()
        names = []
        for root, _dirs, files in os.walk(self.path):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.path)
                names.append(rel.replace(os.sep, "/"))
        return names

    def _build_index(self) -> dict[str, str]:
        index = {}
        for name in self._all_names():
            if not name.lower().endswith(".png"):
                continue
            if "/textures/block/" not in name and "/textures/item/" not in name:
                continue
            stem = os.path.splitext(os.path.basename(name))[0]
            index.setdefault(stem, name)  # first match wins on a name collision
        return index

    def _read(self, arcname: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(arcname)
        with open(os.path.join(self.path, arcname), "rb") as fh:
            return fh.read()

    def get_average_color(self, block_name: str) -> tuple[int, int, int] | None:
        """The texture's average color for a block name, or None if this
        pack has no matching texture (or it failed to decode)."""
        if block_name in self._color_cache:
            return self._color_cache[block_name]
        color = None
        texture_name = self._resolve_texture_name(block_name)
        if texture_name is not None:
            try:
                decoded = decode_png(self._read(self._texture_index[texture_name]))
                color = _average_color(decoded)
            except (UnsupportedPNG, OSError, KeyError):
                color = None
        self._color_cache[block_name] = color
        return color

    def _resolve_texture_name(self, block_name: str) -> str | None:
        base = _SPECIAL_NAMES.get(block_name, block_name)
        candidates = [base + "_top", base]
        for suffix in _STRIP_SUFFIXES:
            if block_name.endswith(suffix):
                stripped = block_name[: -len(suffix)]
                candidates += [stripped + "_top", stripped]
        for candidate in candidates:
            if candidate in self._texture_index:
                return candidate
        return None


def _average_color(decoded) -> tuple[int, int, int] | None:
    width, height, bpp, pixels = decoded
    r_sum = g_sum = b_sum = count = 0
    for i in range(0, len(pixels), bpp):
        if bpp == 4 and pixels[i + 3] < 16:  # skip (near-)fully-transparent pixels
            continue
        r_sum += pixels[i]
        g_sum += pixels[i + 1]
        b_sum += pixels[i + 2]
        count += 1
    if count == 0:
        return None
    return (r_sum // count, g_sum // count, b_sum // count)
