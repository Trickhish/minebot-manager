"""Hand-parser for chunk section data (the one part of the protocol minecraft-
data leaves as a raw byte buffer -- see `map_chunk`'s `chunkData` field).

Format (1.18+, no cross-long bit packing): the buffer is a sequence of 16x16x16
sections, bottom to top, with no length prefix -- you parse sections until the
buffer is exhausted. Each section is:

    block count : i16   (non-air blocks; informational, skipped)
    fluid count : i16   (waterlogged/water/lava blocks; informational, skipped)
    block states : PalettedContainer(4096 entries, indirect <= 8 bits)
    biomes        : PalettedContainer(64 entries,   indirect <= 3 bits)

A PalettedContainer is:
    bits per entry : u8
    palette:
      - bitsPerEntry == 0            -> single value (one VarInt, empty data array)
      - bitsPerEntry <= threshold    -> indirect: VarInt-prefixed VarInt palette,
                                         entries are indices into it
      - bitsPerEntry >  threshold    -> direct: no palette, entries already are
                                         global ids
    data array: that many big-endian i64 longs, each packed with
    floor(64 / bitsPerEntry) entries (no cross-long packing -- leftover bits in
    a long are unused padding). Before protocol 770 (1.21.5) the long count is
    sent explicitly as a leading VarInt; from 1.21.5 on it's omitted and
    computed as ceil(entries * bitsPerEntry / 64).

Reference: https://minecraft.wiki/w/Java_Edition_protocol/Chunk_format
"""

from __future__ import annotations

from array import array

from .buffer import Buffer

BLOCKS_PER_SECTION = 16 * 16 * 16
BIOMES_PER_SECTION = 4 * 4 * 4
_BLOCK_INDIRECT_THRESHOLD = 8
_BIOME_INDIRECT_THRESHOLD = 3

# Protocol number of 1.21.5, the version that dropped the explicit data-array
# length field in favor of computing it from bitsPerEntry * entries.
EXPLICIT_LENGTH_REMOVED_AT_PROTOCOL = 770


def _unpack_entries(longs: list[int], bits_per_entry: int, count: int) -> list[int]:
    entries_per_long = 64 // bits_per_entry
    mask = (1 << bits_per_entry) - 1
    out = []
    for value in longs:
        unsigned = value & 0xFFFFFFFFFFFFFFFF
        for i in range(entries_per_long):
            if len(out) >= count:
                return out
            out.append((unsigned >> (i * bits_per_entry)) & mask)
    return out


def _read_longs(buf: Buffer, bits_per_entry: int, entries_count: int, explicit_length: bool) -> list[int]:
    if explicit_length:
        n = buf.read_varint()
    else:
        # Entries never span a long boundary, so a long holds floor(64/bits)
        # of them and any leftover bits are padding -- NOT simply
        # ceil(entries*bits/64), which ignores that per-long waste.
        entries_per_long = 64 // bits_per_entry
        n = -(-entries_count // entries_per_long)  # ceil
    return [buf.read_num("i64") for _ in range(n)]


def read_paletted_container(buf: Buffer, entries_count: int, indirect_threshold: int,
                             explicit_length: bool) -> list[int]:
    bits_per_entry = buf.read_bytes(1)[0]

    if bits_per_entry == 0:
        value = buf.read_varint()
        if explicit_length:
            n = buf.read_varint()  # always 0 for single-valued containers
            for _ in range(n):
                buf.read_num("i64")
        return [value] * entries_count

    if bits_per_entry <= indirect_threshold:
        palette_len = buf.read_varint()
        palette = [buf.read_varint() for _ in range(palette_len)]
        longs = _read_longs(buf, bits_per_entry, entries_count, explicit_length)
        local_ids = _unpack_entries(longs, bits_per_entry, entries_count)
        return [palette[i] for i in local_ids]

    longs = _read_longs(buf, bits_per_entry, entries_count, explicit_length)
    return _unpack_entries(longs, bits_per_entry, entries_count)


def parse_chunk_column(data: bytes, protocol_version: int) -> list:
    """Parse the raw chunkData buffer into a list of sections (bottom to top).

    Each section is an `array('I', ...)` of 4096 global block state ids,
    indexed `x + z*16 + y*256` within the section. Biome data is parsed (to
    keep the cursor correct) but discarded -- not needed for block lookups.
    """
    explicit_length = protocol_version < EXPLICIT_LENGTH_REMOVED_AT_PROTOCOL
    buf = Buffer(data)
    sections = []
    while buf.remaining > 0:
        buf.read_num("i16")  # block count; informational only
        buf.read_num("i16")  # fluid count; informational only
        block_states = read_paletted_container(
            buf, BLOCKS_PER_SECTION, _BLOCK_INDIRECT_THRESHOLD, explicit_length)
        read_paletted_container(
            buf, BIOMES_PER_SECTION, _BIOME_INDIRECT_THRESHOLD, explicit_length)
        sections.append(array("I", block_states))
    return sections
