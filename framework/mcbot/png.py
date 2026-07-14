"""Minimal PNG encode/decode -- stdlib only (`zlib` + `struct`), no Pillow.

`encode_png` writes 8-bit truecolor with no scanline filtering (simple,
correct, no need to choose a filter heuristic for images we generate
ourselves). `decode_png` handles what real-world texture files need,
normalizing everything to 8-bit RGB/RGBA output: truecolor RGB/RGBA, indexed
(palette, with optional tRNS transparency), and grayscale/grayscale+alpha, at
bit depths 1/2/4/8 (grayscale/palette) or 8 (truecolor), non-interlaced, with
all five PNG filter types. Real texture packs lean heavily on palette and
grayscale PNGs, so expanding them here is what makes resource-pack colors and
the texture atlas work. Interlacing (rare, and absent from block textures)
still raises a clear error rather than producing wrong pixels.
"""

from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# color type -> samples (channels) per pixel: 0 gray, 2 rgb, 3 palette, 4 gray+a, 6 rgba
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def encode_png(rgb) -> bytes:
    """8-bit truecolor PNG, no compression filtering. `rgb` is a
    (H, W, 3) uint8 array-like (numpy array or nested lists)."""
    height, width = len(rgb), len(rgb[0])
    raw = bytearray()
    if hasattr(rgb, "tobytes"):  # numpy array: encode each row in one shot
        for row in rgb:
            raw.append(0)  # filter type: None
            raw += row.tobytes()
    else:
        for row in rgb:
            raw.append(0)
            raw += bytes(bytearray(int(v) for pixel in row for v in pixel))
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + \
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return _SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


class UnsupportedPNG(ValueError):
    """A structurally valid PNG this decoder doesn't handle (see module docstring)."""


def decode_png(data: bytes):
    """(width, height, bytes_per_pixel, pixel_bytes) for a non-interlaced PNG,
    normalized to 8-bit RGB (bpp 3) or RGBA (bpp 4). pixel_bytes is a single
    `bytes` of width*height*bpp, rows top-to-bottom, each row left-to-right
    RGB(A) tuples packed contiguously. Palette/grayscale/sub-8-bit inputs are
    expanded to this same form, so callers only ever see RGB(A).
    """
    if data[:8] != _SIGNATURE:
        raise UnsupportedPNG("not a PNG (bad signature)")

    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    palette = trns = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # length + tag + data + crc

        if tag == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break

    if width is None:
        raise UnsupportedPNG("no IHDR chunk found")
    if interlace != 0:
        raise UnsupportedPNG("interlaced PNGs are not supported")
    if color_type not in _CHANNELS:
        raise UnsupportedPNG(f"unknown color type {color_type}")
    if bit_depth not in (1, 2, 4, 8):
        raise UnsupportedPNG(f"unsupported bit depth {bit_depth}")
    if color_type in (2, 4, 6) and bit_depth != 8:
        raise UnsupportedPNG(
            f"truecolor/grayscale-alpha requires 8-bit depth, got {bit_depth}")
    if color_type == 3 and palette is None:
        raise UnsupportedPNG("indexed PNG has no PLTE chunk")

    channels = _CHANNELS[color_type]
    # PNG filtering operates on whole bytes; for sub-8-bit rows that's >=1 byte.
    filt_bpp = max(1, (channels * bit_depth) // 8)
    stride = (width * channels * bit_depth + 7) // 8

    raw = zlib.decompress(bytes(idat))
    has_alpha = color_type in (4, 6) or (color_type == 3 and trns is not None)
    out_bpp = 4 if has_alpha else 3
    pixels = bytearray(width * height * out_bpp)

    prior = bytes(stride)  # an all-zero "row before the first row"
    offset = 0
    o = 0
    for _y in range(height):
        filter_type = raw[offset]
        scanline = raw[offset + 1:offset + 1 + stride]
        offset += 1 + stride
        recon = _unfilter_row(filter_type, scanline, prior, filt_bpp)
        prior = recon
        samples = _unpack_samples(recon, width * channels, bit_depth)
        o = _expand_row(pixels, o, samples, width, color_type, bit_depth,
                        palette, trns, has_alpha)

    return width, height, out_bpp, bytes(pixels)


def _unpack_samples(recon, count, bit_depth):
    """Sample values (0..2^bit_depth-1) for one row. At bit depth 8 the bytes
    are the samples; otherwise unpack MSB-first."""
    if bit_depth == 8:
        return recon[:count]
    out = bytearray(count)
    mask = (1 << bit_depth) - 1
    per_byte = 8 // bit_depth
    for i in range(count):
        byte = recon[i // per_byte]
        shift = 8 - bit_depth * (i % per_byte + 1)
        out[i] = (byte >> shift) & mask
    return out


def _expand_row(pixels, o, samples, width, color_type, bit_depth, palette, trns, has_alpha):
    """Write one row of samples into `pixels` as RGB/RGBA; return new offset."""
    if color_type in (2, 6):  # truecolor: samples already 8-bit R,G,B(,A)
        nbytes = width * (4 if color_type == 6 else 3)
        pixels[o:o + nbytes] = samples[:nbytes]
        return o + nbytes

    maxval = (1 << bit_depth) - 1
    for x in range(width):
        if color_type == 0:  # grayscale
            g = samples[x] * 255 // maxval
            pixels[o] = pixels[o + 1] = pixels[o + 2] = g
            o += 3
        elif color_type == 4:  # grayscale + alpha (8-bit)
            g, a = samples[2 * x], samples[2 * x + 1]
            pixels[o] = pixels[o + 1] = pixels[o + 2] = g
            pixels[o + 3] = a
            o += 4
        else:  # palette index
            idx = samples[x]
            pixels[o] = palette[idx * 3]
            pixels[o + 1] = palette[idx * 3 + 1]
            pixels[o + 2] = palette[idx * 3 + 2]
            if has_alpha:
                pixels[o + 3] = trns[idx] if idx < len(trns) else 255
                o += 4
            else:
                o += 3
    return o


def _unfilter_row(filter_type: int, scanline: bytes, prior: bytes, bpp: int) -> bytearray:
    recon = bytearray(len(scanline))
    if filter_type == 0:  # None
        recon[:] = scanline
    elif filter_type == 1:  # Sub
        for x in range(len(scanline)):
            a = recon[x - bpp] if x >= bpp else 0
            recon[x] = (scanline[x] + a) & 0xFF
    elif filter_type == 2:  # Up
        for x in range(len(scanline)):
            recon[x] = (scanline[x] + prior[x]) & 0xFF
    elif filter_type == 3:  # Average
        for x in range(len(scanline)):
            a = recon[x - bpp] if x >= bpp else 0
            recon[x] = (scanline[x] + (a + prior[x]) // 2) & 0xFF
    elif filter_type == 4:  # Paeth
        for x in range(len(scanline)):
            a = recon[x - bpp] if x >= bpp else 0
            b = prior[x]
            c = prior[x - bpp] if x >= bpp else 0
            recon[x] = (scanline[x] + _paeth(a, b, c)) & 0xFF
    else:
        raise UnsupportedPNG(f"unknown scanline filter type {filter_type}")
    return recon


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c
