#!/usr/bin/env python3
"""Draw Desireerr's PNG app icons from the logo's shapes (no image libraries needed).

The PNGs are for home-screen icons: iOS wants a full-bleed square (it rounds the corners
itself); the web manifest gets the rounded tile. Run from the repo root:

    python3 tools/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
STOPS = [(0.0, (0xFB, 0x71, 0x85)), (0.5, (0xBE, 0x12, 0x3C)), (1.0, (0x4C, 0x05, 0x19))]
HEART_COLOR = (0xBE, 0x12, 0x3C)
SS = 4  # supersampling per axis

FLAME = [("M", 60, 16), ("C", 66, 35, 87, 47, 87, 74), ("C", 87, 90, 75, 102, 60, 102),
         ("C", 45, 102, 33, 90, 33, 74), ("C", 33, 62, 39, 54, 47, 46),
         ("C", 48, 56, 52, 62, 57, 64), ("C", 53, 49, 55, 31, 60, 16)]
HEART = [("M", 60, 96), ("C", 32, 79, 22, 65, 22, 51), ("C", 22, 38, 32, 28, 45, 28),
         ("C", 52, 28, 57, 32, 60, 37), ("C", 63, 32, 68, 28, 75, 28),
         ("C", 88, 28, 98, 38, 98, 51), ("C", 98, 65, 88, 79, 60, 96)]
HEART_XFORM = (43, 60, 0.28333)  # translate x, y, then scale, as in logo.svg


def flatten(path, xform=None, steps=32):
    pts = []
    cur = (0, 0)
    for cmd in path:
        if cmd[0] == "M":
            cur = cmd[1:3]
            pts.append(cur)
        else:
            x1, y1, x2, y2, x3, y3 = cmd[1:]
            x0, y0 = cur
            for i in range(1, steps + 1):
                t = i / steps
                u = 1 - t
                pts.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                            u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
            cur = (x3, y3)
    if xform:
        tx, ty, s = xform
        pts = [(tx + x * s, ty + y * s) for x, y in pts]
    return pts


def rounded_rect(r, size=120, steps=12):
    pts = []
    for cx, cy, a0 in ((size - r, r, -90), (size - r, size - r, 0), (r, size - r, 90), (r, r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def coverage(poly, size):
    """Per-pixel coverage (0..1) of a polygon given in 0..120 units, at size x size."""
    n = size * SS
    scale = n / 120
    edges = []
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        if y0 != y1:
            edges.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    cov = [0.0] * (size * size)
    unit = 1 / (SS * SS)
    for sy in range(n):
        y = sy + 0.5
        xs = []
        for x0, y0, x1, y1 in edges:
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        row = (sy // SS) * size
        for a, b in zip(xs[::2], xs[1::2]):
            lo = max(0, math.ceil(a - 0.5))
            hi = min(n - 1, math.floor(b - 0.5))
            for sx in range(lo, hi + 1):
                cov[row + sx // SS] += unit
    return cov


def gradient(t):
    t = min(1, max(0, t))
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(c0[i] + (c1[i] - c0[i]) * f for i in range(3))
    return STOPS[-1][1]


def render(size, rounded):
    tile = coverage(rounded_rect(28), size) if rounded else [1.0] * (size * size)
    flame = coverage(flatten(FLAME), size)
    heart = coverage(flatten(HEART, HEART_XFORM), size)
    out = bytearray()
    for y in range(size):
        out.append(0)  # PNG filter: none
        for x in range(size):
            i = y * size + x
            r, g, b = gradient((x + y + 1) / (2 * size))
            a = tile[i]
            for cov, col in ((flame[i], (255, 255, 255)), (heart[i], HEART_COLOR)):
                cov = min(1.0, cov)
                r, g, b = (r + (col[0] - r) * cov, g + (col[1] - g) * cov, b + (col[2] - b) * cov)
            out += bytes((round(r), round(g), round(b), round(255 * min(1.0, a))))
    return bytes(out)


def png(size, pixels):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(pixels, 9)) + chunk(b"IEND", b""))


def main():
    for name, size, rounded in (("icon-180.png", 180, False), ("icon-192.png", 192, True),
                                ("icon-512.png", 512, True)):
        (WEB / name).write_bytes(png(size, render(size, rounded)))
        print("wrote", WEB / name)


if __name__ == "__main__":
    main()
