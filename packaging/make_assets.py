"""Generate the full set of MSIX tile/logo assets from one square PNG.

Produces, under <outdir>, for every logo:
  * the unqualified base file (e.g. Square150x150Logo.png) at scale-100 size,
  * the .scale-100/125/150/200/400 variants,
and for Square44x44Logo additionally:
  * .targetsize-16/24/32/48/256 and their _altform-unplated siblings.

Tile logos get transparent padding around the mark (Microsoft's tile icon
guidance); target-size (app list / taskbar) icons are drawn full-bleed.

Usage:  python -I make_assets.py <source.png> <outdir>
Dependency: Pillow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# Base (scale-100) sizes.
SQUARE_LOGOS = {
    "Square44x44Logo": 44,
    "Square71x71Logo": 71,
    "Square150x150Logo": 150,
    "StoreLogo": 50,
}
WIDE_LOGO = ("Wide310x150Logo", 310, 150)

SCALES = [100, 125, 150, 200, 400]
TARGET_SIZES = [16, 24, 32, 48, 256]

# How much of the canvas the mark fills (rest is transparent padding).
TILE_PAD = 0.66      # square/wide tiles
STORE_PAD = 0.80     # store logo a touch fuller
TARGET_PAD = 1.0     # app-list/taskbar icons: full bleed


def _scaled(n: int, scale: int) -> int:
    return max(1, int(round(n * scale / 100.0)))


def _square(src: Image.Image, size: int, pad: float) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    content = max(1, int(round(size * pad)))
    resized = src.resize((content, content), Image.LANCZOS)
    off = (size - content) // 2
    canvas.paste(resized, (off, off), resized)
    return canvas


def _wide(src: Image.Image, w: int, h: int, pad: float) -> Image.Image:
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    content = max(1, int(round(h * pad)))
    resized = src.resize((content, content), Image.LANCZOS)
    canvas.paste(resized, ((w - content) // 2, (h - content) // 2), resized)
    return canvas


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: make_assets.py <source.png> <outdir>")
        return 2
    source = Path(sys.argv[1])
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    src = Image.open(source).convert("RGBA")
    count = 0

    for name, base in SQUARE_LOGOS.items():
        pad = STORE_PAD if name == "StoreLogo" else TILE_PAD
        # Unqualified base (scale-100) so the manifest reference resolves even
        # without MRT indexing.
        _square(src, base, pad).save(outdir / f"{name}.png")
        count += 1
        for s in SCALES:
            img = _square(src, _scaled(base, s), pad)
            img.save(outdir / f"{name}.scale-{s}.png")
            count += 1

    name, w, h = WIDE_LOGO
    _wide(src, w, h, TILE_PAD).save(outdir / f"{name}.png")
    count += 1
    for s in SCALES:
        _wide(src, _scaled(w, s), _scaled(h, s), TILE_PAD).save(
            outdir / f"{name}.scale-{s}.png")
        count += 1

    # Square44x44Logo target-size variants (plated + unplated).
    for t in TARGET_SIZES:
        icon = _square(src, t, TARGET_PAD)
        icon.save(outdir / f"Square44x44Logo.targetsize-{t}.png")
        icon.save(outdir / f"Square44x44Logo.targetsize-{t}_altform-unplated.png")
        count += 2

    print(f"make_assets: wrote {count} files to {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
