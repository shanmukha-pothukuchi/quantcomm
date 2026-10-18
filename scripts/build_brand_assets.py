#!/usr/bin/env python
"""Build the Quantcomm logo mark and favicons from web/assets/brand/quantcomm-logo.png.

    .venv/bin/python scripts/build_brand_assets.py

The source is the full logo (circle-and-arrow mark above the QUANTCOMM wordmark on navy). Outputs, all in
web/assets/brand/:
  mark.png              the mark alone on a transparent background (page headers)
  favicon.ico           16/32/48 px, mark on the logo's navy (browser tab)
  favicon-32.png        32 px PNG of the same
  apple-touch-icon.png  180 px, for home-screen bookmarks
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

BRAND = Path(__file__).resolve().parents[1] / "web" / "assets" / "brand"
SOURCE = BRAND / "quantcomm-logo.png"
NAVY = np.array([11, 17, 63], dtype=float)  # logo background
KEY_LO, KEY_HI = 40.0, 140.0                # colour distance from navy: <= LO fully transparent, >= HI fully opaque
FG_DIST = 60.0                              # distance that counts as artwork when finding the mark's bounds
EDGE_PX = 4                                 # ignore the outer pixels (the screenshot has a 1 px grey border)
MARK_SIZE = 256
PAD = 0.06                                  # transparent margin around the mark, as a share of its size
ICON_INSET = 0.10                           # margin inside the navy favicon square
ICON_RADIUS = 0.22                          # corner radius of the favicon square, as a share of its size


def mark_bounds(rgb: np.ndarray) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1) of the mark: the artwork block above the first empty band of rows."""
    fg = np.sqrt(((rgb - NAVY) ** 2).sum(-1)) > FG_DIST
    fg[:EDGE_PX, :] = fg[-EDGE_PX:, :] = False
    fg[:, :EDGE_PX] = fg[:, -EDGE_PX:] = False
    rows = np.where(fg.any(1))[0]
    if rows.size == 0:
        raise ValueError(f"no artwork found in {SOURCE}")
    gaps = np.where(np.diff(rows) > 1)[0]
    y0, y1 = int(rows[0]), int(rows[gaps[0]] if gaps.size else rows[-1])
    cols = np.where(fg[y0:y1 + 1].any(0))[0]
    return int(cols[0]), y0, int(cols[-1]), y1


def transparent_mark(rgb: np.ndarray, box: tuple[int, int, int, int]) -> Image.Image:
    x0, y0, x1, y1 = box
    crop = rgb[y0:y1 + 1, x0:x1 + 1]
    dist = np.sqrt(((crop - NAVY) ** 2).sum(-1))
    alpha = np.clip((dist - KEY_LO) / (KEY_HI - KEY_LO), 0, 1)
    # un-mix the navy from anti-aliased edges so they don't look dark-blue on other backgrounds
    a = np.maximum(alpha, 1e-3)[..., None]
    color = np.clip((crop - (1 - a) * NAVY) / a, 0, 255)
    rgba = np.dstack([color, alpha * 255]).astype("uint8")
    img = Image.fromarray(rgba, "RGBA")
    side = int(max(img.size) * (1 + 2 * PAD))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def icon(mark: Image.Image, size: int) -> Image.Image:
    scale = 4  # draw large, then downsample for smooth corners
    big = size * scale
    bg = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    radius = int(big * ICON_RADIUS)
    from PIL import ImageDraw
    ImageDraw.Draw(bg).rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=tuple(int(c) for c in NAVY) + (255,))
    inner = int(big * (1 - 2 * ICON_INSET))
    m = mark.resize((inner, inner), Image.LANCZOS)
    bg.alpha_composite(m, ((big - inner) // 2, (big - inner) // 2))
    return bg.resize((size, size), Image.LANCZOS)


def main() -> None:
    rgb = np.asarray(Image.open(SOURCE).convert("RGB")).astype(float)
    box = mark_bounds(rgb)
    mark = transparent_mark(rgb, box)
    mark.resize((MARK_SIZE, MARK_SIZE), Image.LANCZOS).save(BRAND / "mark.png", optimize=True)
    icon(mark, 32).save(BRAND / "favicon-32.png", optimize=True)
    icon(mark, 180).save(BRAND / "apple-touch-icon.png", optimize=True)
    icon(mark, 48).save(BRAND / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"mark bounds {box}; wrote mark.png, favicon.ico, favicon-32.png, apple-touch-icon.png to {BRAND}")


if __name__ == "__main__":
    main()
