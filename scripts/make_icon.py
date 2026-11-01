#!/usr/bin/env python3
"""Draw the viOSC icon as a PNG (stdlib only: struct + zlib).

Usage: python scripts/make_icon.py OUT.png [SIZE]
The build_macos_app.sh script feeds the 1024 px result to sips/iconutil.
"""

import struct
import sys
import zlib

BG = (24, 24, 26)
FG = (70, 230, 120)


def _near(x1, y1, x2, y2, px, py, thickness):
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length))
    qx, qy = x1 + t * dx, y1 + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2 <= thickness * thickness


def pixel(size, x, y):
    sx = x * 255.0 / max(1, size - 1)
    sy = y * 255.0 / max(1, size - 1)
    edge = sx < 10 or sx > 245 or sy < 10 or sy > 245
    thickness = 8.0 * 255.0 / size
    on_v = _near(52, 42, 122, 206, sx, sy, thickness) or _near(122, 206, 204, 42, sx, sy, thickness)
    return FG if (edge or on_v) else BG


def png(size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter: none
        for x in range(size):
            raw.extend(pixel(size, x, y))

    def chunk(tag, body):
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: make_icon.py OUT.png [SIZE]")
    size = int(sys.argv[2]) if len(sys.argv) == 3 else 1024
    with open(sys.argv[1], "wb") as handle:
        handle.write(png(size))


if __name__ == "__main__":
    main()
