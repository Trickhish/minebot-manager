"""Approximate block -> RGB color, for map rendering.

Minecraft doesn't publish a machine-readable block-color table (in-game maps
derive colors from block textures, which are copyrighted assets we don't
vendor). Instead:

  1. A curated table covers common terrain/ore/liquid blocks by exact name.
  2. Blocks whose name is prefixed with one of the 16 standard dye colors
     (wool, concrete, terracotta, stained glass, banners, beds, candles, ...)
     get that color automatically -- covers hundreds of blocks for free.
  3. Anything else gets a color hashed from its name: deterministic and
     visually distinct block-to-block, so the map doesn't collapse unknown
     blocks into a single flat gray, but the color itself carries no meaning.

These are approximations for a readable minimap, not accurate render colors.
"""

from __future__ import annotations

import hashlib

# Minecraft's 16 standard dye colors (approximate wool RGB values).
DYE_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (233, 236, 236), "orange": (240, 118, 19), "magenta": (189, 68, 179),
    "light_blue": (58, 175, 217), "yellow": (248, 198, 39), "lime": (112, 185, 25),
    "pink": (237, 141, 172), "gray": (62, 68, 71), "light_gray": (142, 142, 134),
    "cyan": (21, 119, 136), "purple": (121, 42, 172), "blue": (53, 57, 157),
    "brown": (114, 71, 40), "green": (84, 109, 27), "red": (160, 39, 34),
    "black": (20, 21, 25),
}

_BLOCK_COLORS: dict[str, tuple[int, int, int]] = {
    "grass_block": (91, 153, 61), "dirt": (134, 96, 67), "coarse_dirt": (117, 85, 60),
    "podzol": (89, 68, 46), "mycelium": (111, 97, 97), "farmland": (107, 74, 43),
    "stone": (125, 125, 125), "cobblestone": (122, 122, 122), "mossy_cobblestone": (110, 122, 100),
    "andesite": (136, 136, 137), "diorite": (188, 188, 188), "granite": (149, 105, 86),
    "deepslate": (79, 79, 83), "cobbled_deepslate": (75, 75, 79), "tuff": (108, 109, 102),
    "bedrock": (85, 85, 85), "sand": (219, 207, 163), "red_sand": (190, 101, 34),
    "sandstone": (216, 203, 155), "red_sandstone": (169, 91, 32),
    "gravel": (136, 126, 122), "clay": (160, 166, 178), "snow": (249, 254, 254),
    "snow_block": (249, 254, 254), "ice": (158, 195, 253), "packed_ice": (141, 180, 250),
    "blue_ice": (116, 168, 253), "obsidian": (20, 18, 29),
    "water": (63, 118, 228), "lava": (207, 92, 15),
    "oak_log": (109, 85, 51), "oak_planks": (162, 130, 78), "oak_leaves": (72, 111, 40),
    "spruce_log": (58, 42, 25), "spruce_planks": (114, 84, 48), "spruce_leaves": (56, 91, 55),
    "birch_log": (216, 210, 165), "birch_planks": (192, 175, 121), "birch_leaves": (94, 124, 57),
    "jungle_log": (85, 67, 33), "jungle_planks": (160, 116, 79), "jungle_leaves": (65, 105, 34),
    "acacia_log": (103, 100, 95), "acacia_planks": (169, 92, 52), "acacia_leaves": (99, 124, 42),
    "dark_oak_log": (60, 46, 26), "dark_oak_planks": (66, 43, 20), "dark_oak_leaves": (57, 84, 30),
    "mangrove_log": (117, 54, 51), "mangrove_leaves": (66, 118, 43),
    "cherry_log": (91, 62, 62), "cherry_leaves": (234, 175, 200),
    "coal_ore": (95, 95, 95), "deepslate_coal_ore": (75, 75, 77),
    "iron_ore": (175, 158, 143), "deepslate_iron_ore": (140, 130, 122),
    "gold_ore": (196, 172, 87), "deepslate_gold_ore": (154, 140, 94),
    "diamond_ore": (110, 199, 191), "deepslate_diamond_ore": (95, 155, 150),
    "emerald_ore": (65, 173, 105), "deepslate_emerald_ore": (67, 140, 95),
    "lapis_ore": (52, 90, 166), "deepslate_lapis_ore": (63, 88, 133),
    "redstone_ore": (163, 41, 22), "deepslate_redstone_ore": (128, 51, 38),
    "copper_ore": (156, 112, 84), "deepslate_copper_ore": (120, 97, 80),
    "netherrack": (110, 53, 51), "soul_sand": (81, 63, 51), "soul_soil": (75, 58, 46),
    "glowstone": (177, 137, 82), "nether_gold_ore": (114, 63, 40),
    "end_stone": (219, 219, 165), "grass": (60, 92, 40), "tall_grass": (60, 92, 40),
    "fern": (58, 95, 58), "cactus": (60, 100, 40), "pumpkin": (192, 108, 21),
    "melon": (108, 148, 40), "glass": (220, 236, 236), "bookshelf": (140, 106, 63),
    "torch": (226, 177, 76), "wall_torch": (226, 177, 76),
    "soul_torch": (94, 168, 178), "soul_wall_torch": (94, 168, 178),
    "redstone_torch": (176, 54, 43), "redstone_wall_torch": (176, 54, 43),
    "chest": (126, 82, 35), "trapped_chest": (134, 80, 38),
    "ender_chest": (45, 70, 78), "hopper": (76, 78, 82),
    "ladder": (139, 104, 58), "rail": (135, 119, 96), "powered_rail": (181, 143, 68),
    "detector_rail": (155, 116, 87), "activator_rail": (157, 122, 91),
    "chain": (78, 78, 82), "iron_chain": (78, 78, 82),
    "end_rod": (220, 209, 188), "cauldron": (75, 78, 82),
    "water_cauldron": (75, 78, 82), "lava_cauldron": (75, 78, 82),
    "powder_snow_cauldron": (75, 78, 82), "campfire": (117, 74, 42),
    "soul_campfire": (83, 86, 76), "candle": (221, 214, 190),
}


def get_block_color(name: str, resource_pack=None) -> tuple[int, int, int]:
    """A block's color: from `resource_pack` (a `ResourcePack`) if given and it
    has a matching texture, else the curated table, else a dye-name match,
    else a deterministic hash color."""
    if resource_pack is not None:
        color = resource_pack.get_average_color(name)
        if color is not None:
            return color

    color = _BLOCK_COLORS.get(name)
    if color is not None:
        return color

    for suffix in ("_wall_hanging_sign", "_hanging_sign", "_wall_sign", "_sign"):
        if name.endswith(suffix):
            wood = name[: -len(suffix)]
            color = _BLOCK_COLORS.get(f"{wood}_planks")
            if color is not None:
                return color

    for dye_name, rgb in DYE_COLORS.items():
        if name == dye_name or name.startswith(dye_name + "_") or name.endswith("_" + dye_name):
            return rgb

    return _hash_color(name)


def _hash_color(name: str) -> tuple[int, int, int]:
    digest = hashlib.md5(name.encode()).digest()
    # Keep it mid-brightness (avoid near-black/near-white) so unknown blocks
    # stay visually distinct from the background/fog colors.
    return (80 + digest[0] % 140, 80 + digest[1] % 140, 80 + digest[2] % 140)
