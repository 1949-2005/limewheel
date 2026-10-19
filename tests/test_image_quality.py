"""Before/after image quality: 1.2.0 (limewheel package in the repo copy) vs the current code.

    python tests/test_image_quality.py <old repo dir> <screenshot> <samples dir>
"""
import io
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OLD, SHOT, SRC = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(OLD))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from PIL import Image, ImageFilter  # noqa: E402
import limewheel.ops.images as old  # noqa: E402
import limewheel.ops.images as new  # noqa: E402

W = Path(tempfile.mkdtemp(prefix="iq-"))


def ssim(a, b):
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    r = np.arange(-5, 6)
    k = np.exp(-(r ** 2) / (2 * 1.5 ** 2))
    k /= k.sum()

    def g(x):                                     # separable Gaussian blur (sigma 1.5)
        x = np.apply_along_axis(lambda v: np.convolve(np.pad(v, 5, mode="edge"), k, "valid"), 0, x)
        return np.apply_along_axis(lambda v: np.convolve(np.pad(v, 5, mode="edge"), k, "valid"), 1, x)
    c1, c2 = 6.5025, 58.5225
    out = []
    for ch in range(a.shape[2]):
        x, y = a[..., ch], b[..., ch]
        mx, my = g(x), g(y)
        sx, sy, sxy = g(x * x) - mx * mx, g(y * y) - my * my, g(x * y) - mx * my
        out.append((((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sx + sy + c2))).mean())
    return float(np.mean(out))


def psnr(a, b):
    mse = np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def on_bg(im, bg):
    base = Image.new("RGBA", im.size, bg)
    return np.asarray(Image.alpha_composite(base, im.convert("RGBA")).convert("RGB"))


def frame(ico_path, size):
    ico = Image.open(ico_path)
    ico.size = (size, size)
    return ico.convert("RGBA")


def win_decodes(path):
    ps = (f"Add-Type -AssemblyName PresentationCore; $s=[IO.File]::OpenRead('{path}'); "
          f"try {{ ([System.Windows.Media.Imaging.IconBitmapDecoder]::new($s,'None','OnLoad').Frames | % PixelWidth) -join ',' }} "
          f"catch {{ 'FAIL' }} finally {{ $s.Close() }}")
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()


rows = []
src = Image.open(SHOT).convert("RGBA")
side = max(src.size)
sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
sq.paste(src, ((side - src.width) // 2, (side - src.height) // 2))
for label, mod, sizes in (("ICO (old)", old, old.ICO_SIZES), ("ICO (new)", new, new.ICO_SIZES),
                          ("ICO small (old)", old, old.ICO_SMALL), ("ICO small (new)", new, new.ICO_SMALL)):
    dst = W / f"{label.replace(' ', '_').replace('(', '').replace(')', '')}.ico"
    mod.save_ico(src, dst, sizes)
    scores = []
    for s in (16, 32, 48, 256):
        if s not in sizes:
            continue
        ref = sq.resize((s, s), Image.Resampling.LANCZOS)
        f = frame(dst, s)
        scores.append(f"{s}px {ssim(on_bg(ref, 'white'), on_bg(f, 'white')):.4f}")
    rows.append((label, f"{dst.stat().st_size / 1024:.0f} KB", "  ".join(scores), win_decodes(dst)))

print("== ICO from your screenshot (SSIM vs ideal resize, 1.0 = identical)")
for r in rows:
    print(f"  {r[0]:16} {r[1]:>7}  {r[2]}   Windows reads: {r[3]}")

# ---- other formats
photo = SRC / "flower.jpg"
cases = []


def conv(mod, p, ext):
    dst = W / f"{mod.__name__.split('.')[0]}-{p.stem}.{ext}"
    if mod is new:
        mod.save_image(mod.open_image(p), dst, ext, 85, src_ext=new.ext_of(p))
    else:
        mod.save_image(mod.open_image(p), dst, ext, 85)
    return dst


def score(p, ref_path):
    a = np.asarray(Image.open(ref_path).convert("RGB"))
    b = np.asarray(Image.open(p).convert("RGB"))
    return ssim(a, b), psnr(a, b)


shot_png = W / "screenshot.png"
Image.open(SHOT).convert("RGB").save(shot_png)
print("\n== Conversions (SSIM / PSNR vs source)")
for name, p, ext in (("screenshot -> JPG", shot_png, "jpg"), ("screenshot -> WEBP", shot_png, "webp"),
                     ("screenshot -> AVIF", shot_png, "avif"), ("photo -> WEBP", photo, "webp"),
                     ("photo -> AVIF", photo, "avif")):
    o, n_ = conv(old, p, ext), conv(new, p, ext)
    so, sn = score(o, p), score(n_, p)
    print(f"  {name:20} old {so[0]:.4f} / {so[1]:5.1f} dB  {o.stat().st_size // 1024:>4} KB   "
          f"new {sn[0]:.4f} / {sn[1]:5.1f} dB  {n_.stat().st_size // 1024:>4} KB")

print("\n== Tools")
jp = W / "rot.jpg"
shutil.copy2(photo, jp)
ctx = type("C", (), {"settings": {"image_quality": 85}, "app": None})()
rot = new._transpose(Image.Transpose.ROTATE_270, "-rotated")(jp, {}, ctx)
from PIL import ImageOps  # noqa: E402
a = np.asarray(ImageOps.exif_transpose(Image.open(rot)).convert("RGB"))
b = np.asarray(Image.open(jp).convert("RGB").transpose(Image.Transpose.ROTATE_270))
print(f"  rotate JPG: pixels identical to a perfect rotation = {np.array_equal(a, b)} (old re-encoded at q85)")
pngc = W / "shot_c.png"
shutil.copy2(shot_png, pngc)
for label, fn in (("old", old._compress), ("new", new._compress)):
    try:
        out = fn(pngc, {}, ctx)
        s = score(out, pngc)
        print(f"  compress PNG ({label}): {out.stat().st_size * 100 // pngc.stat().st_size}% of size, SSIM {s[0]:.4f}")
        out.unlink()
    except Exception as e:
        print(f"  compress PNG ({label}): {e}")
print(f"\n(work dir {W})")
