"""PNG export of a chart scene, with no imaging library: a small raster (numpy), glyph bitmaps cut from
DejaVu Sans (see ``tools/make_font.py``) and a PNG writer (zlib + struct). Deterministic: the same scene
always gives the same bytes (no timestamps or metadata). Characters outside ASCII print as '?'.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from functools import lru_cache
from typing import Any

import numpy as np

from sie.workspace.scene import Scene

SCALE = 2  # PNG pixels per scene unit
MAX_PIXELS = 4_000_000


@lru_cache(maxsize=1)
def _fonts() -> dict[str, Any]:
    from sie.workspace._font_data import DATA

    return json.loads(zlib.decompress(base64.b64decode(DATA)))  # type: ignore[no-any-return]


def png_bytes(pixels: np.ndarray) -> bytes:
    """A truecolour PNG from an (h, w, 3) uint8 array."""
    h, w, _ = pixels.shape
    raw = b"".join(b"\x00" + pixels[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _fill(
    img: np.ndarray, x0: float, y0: float, x1: float, y1: float, rgb: tuple[int, int, int]
) -> None:
    h, w, _ = img.shape
    a, b = max(0, round(x0)), max(0, round(y0))
    c, d = min(w, max(round(x1), a + 1)), min(h, max(round(y1), b + 1))
    if a < c and b < d:
        img[b:d, a:c] = rgb


def _line(
    img: np.ndarray,
    p: tuple[float, float],
    q: tuple[float, float],
    rgb: tuple[int, int, int],
    width: float,
) -> None:
    half = width * SCALE / 2
    steps = int(max(abs(q[0] - p[0]), abs(q[1] - p[1])) * SCALE) + 1
    for k in range(steps + 1):
        t = k / steps
        x, y = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        _fill(img, x * SCALE - half, y * SCALE - half, x * SCALE + half, y * SCALE + half, rgb)


def _dot(img: np.ndarray, cx: float, cy: float, r: float, rgb: tuple[int, int, int]) -> None:
    h, w, _ = img.shape
    rr = r * SCALE
    ys, xs = np.ogrid[:h, :w]
    mask = (xs - cx * SCALE) ** 2 + (ys - cy * SCALE) ** 2 <= rr * rr
    img[mask] = rgb


def _ascii(text: str) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)


def _text(
    img: np.ndarray,
    x: float,
    y: float,
    text: str,
    size: str,
    anchor: str,
    rgb: tuple[int, int, int],
) -> None:
    font = _fonts()["bold" if size == "t" else "regular"]
    glyphs = font["g"]
    text = _ascii(text)
    width = sum(glyphs[c]["adv"] for c in text)
    px = x * SCALE - (width if anchor == "end" else width / 2 if anchor == "middle" else 0)
    top = round(y * SCALE) - font["ascent"]
    h, w, _ = img.shape
    for c in text:
        g = glyphs[c]
        for r, bits in enumerate(g["rows"]):
            yy = top + r
            if not 0 <= yy < h:
                continue
            for cx, bit in enumerate(bits):
                xx = round(px) + cx - 1
                if bit == "1" and 0 <= xx < w:
                    img[yy, xx] = rgb
        px += g["adv"]


def render_png(scene: Scene) -> bytes:
    """The scene as a PNG on a white background."""
    if scene.width * scene.height * SCALE * SCALE > MAX_PIXELS:
        raise ValueError("chart is too large to export as an image")
    img = np.full((scene.height * SCALE, scene.width * SCALE, 3), 255, dtype=np.uint8)
    for item in scene.items:
        kind = item[0]
        if kind == "rect":
            _, x, y, w, h, rgb = item
            _fill(img, x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE, rgb)
        elif kind == "line":
            _, x1, y1, x2, y2, rgb, wd = item
            _line(img, (x1, y1), (x2, y2), rgb, wd)
        elif kind == "poly":
            _, pts, rgb, wd = item
            for p, q in zip(pts, pts[1:], strict=False):
                _line(img, p, q, rgb, wd)
        elif kind == "dot":
            _, cx, cy, r, rgb = item
            _dot(img, cx, cy, r, rgb)
        elif kind == "text":
            _, x, y, s, size, anchor, rgb = item
            _text(img, x, y, s, size, anchor, rgb)
    return png_bytes(img)
