#!/usr/bin/env python3
"""Regenerates the menu bar icon (assets/miraTemplate.png and @2x).

macOS template images are black-plus-alpha only: the system throws the colour
away and re-tints the glyph for light, dark and highlighted menu bars. So this
draws a mask, not a picture.

The mark is Mira's M with its dot. The reason it is drawn here rather than
exported from a design tool is that a menu bar icon is a 16-pixel problem --
it has to be tuned by looking at the pixels, and a script makes that a one-line
change instead of a round trip.

Sizing follows what the icon actually sits next to. Apple's own menu bar glyphs
fill most of a 16x16 frame with strokes around 1.5-2px at 1x; the previous
version used a half-height M with thin strokes, which read as faint and
undersized beside Wi-Fi and battery.

    python3 electron/scripts/generate_tray_icon.py
"""

import math
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# Geometry in a 16x16 design grid, reused at both scales.
# The M is one polyline stroked with round joins -- cheaper to tune than
# outlined shapes, and round joins survive downsampling better than mitres.
M_POINTS = [(3.0, 13.4), (3.0, 5.2), (8.0, 10.6), (13.0, 5.2), (13.0, 13.4)]
M_STROKE = 2.4          # full width; half of this is the distance threshold
DOT_CENTER = (8.0, 2.35)
DOT_RADIUS = 1.45

SUPERSAMPLE = 8         # rendered at 8x then box-filtered, for clean edges


def _dist_to_segment(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    seg_len_sq = vx * vx + vy * vy
    t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / seg_len_sq))
    dx, dy = ax + t * vx - px, ay + t * vy - py
    return math.hypot(dx, dy)


def _coverage(x, y):
    """1.0 inside the mark, 0.0 outside, at a point in the 16x16 grid."""
    half = M_STROKE / 2.0
    for i in range(len(M_POINTS) - 1):
        ax, ay = M_POINTS[i]
        bx, by = M_POINTS[i + 1]
        if _dist_to_segment(x, y, ax, ay, bx, by) <= half:
            return 1.0
    if math.hypot(x - DOT_CENTER[0], y - DOT_CENTER[1]) <= DOT_RADIUS:
        return 1.0
    return 0.0


def render(size):
    """Alpha values for a size x size icon, antialiased by supersampling."""
    scale = size / 16.0
    ss = SUPERSAMPLE
    alpha = bytearray(size * size)

    for py in range(size):
        for px in range(size):
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    # sample at subpixel centres, mapped back to the 16x16 grid
                    gx = (px + (sx + 0.5) / ss) / scale
                    gy = (py + (sy + 0.5) / ss) / scale
                    hits += _coverage(gx, gy)
            alpha[py * size + px] = int(round(255 * hits / (ss * ss)))
    return alpha


def write_png(path, size, alpha):
    """Minimal RGBA PNG writer -- avoids a Pillow dependency for one script."""
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None) for every scanline
        for x in range(size):
            a = alpha[y * size + x]
            # Black everywhere; only alpha carries the shape, which is exactly
            # what makes this a valid template image.
            raw += bytes((0, 0, 0, a))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main():
    for size, name in ((16, "miraTemplate.png"), (32, "miraTemplate@2x.png")):
        out = ASSETS / name
        write_png(out, size, render(size))
        print(f"wrote {out} ({size}x{size})")


if __name__ == "__main__":
    main()
