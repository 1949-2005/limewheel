"""Image conversions and tools (Pillow + pillow-heif, SVG via Qt)."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from .base import OpError, ask_int, ext_of, out_path, register

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:  # pragma: no cover
    HEIF = False

IMG_IN = frozenset("jpg png webp heic tiff avif bmp gif ico svg".split())
TARGETS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "heic": "HEIF", "avif": "AVIF",
           "tiff": "TIFF", "bmp": "BMP", "gif": "GIF", "ico": "ICO", "pdf": "PDF"}
ANIMATED_OK = {"gif", "webp", "png"}


def _svg(p: Path) -> Image.Image:
    from PySide6.QtCore import QBuffer, QIODevice, QSize
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    r = QSvgRenderer(str(p))
    if not r.isValid():
        raise OpError("Could not read the SVG")
    size = r.defaultSize()
    if size.isEmpty():
        size = QSize(1024, 1024)
    scale = max(1.0, 1024 / max(size.width(), size.height()))
    img = QImage(int(size.width() * scale), int(size.height() * scale), QImage.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    r.render(painter)
    painter.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data())))


def open_image(p: Path) -> Image.Image:
    if ext_of(p) == "svg":
        return _svg(p)
    im = Image.open(p)
    if getattr(im, "n_frames", 1) > 1:
        return im
    return ImageOps.exif_transpose(im)


def _flatten(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB") if im.mode not in ("RGB", "L") else im


def save_image(im: Image.Image, dst: Path, ext: str, quality: int = 85) -> Path:
    fmt = TARGETS[ext]
    kw = {}
    animated = getattr(im, "n_frames", 1) > 1
    if animated and ext in ANIMATED_OK:
        kw.update(save_all=True, loop=im.info.get("loop", 0))
        if ext == "webp":
            kw["quality"] = quality
        im.save(dst, fmt, **kw)
        return dst
    if animated:
        im.seek(0)
        im = im.copy()
    if ext in ("jpg", "pdf", "bmp"):
        im = _flatten(im)
    elif im.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
        im = im.convert("RGBA")
    if ext in ("jpg", "webp", "avif", "heic"):
        kw["quality"] = quality
    if ext == "jpg":
        kw.update(optimize=True, progressive=True)
    if ext == "png":
        kw["optimize"] = True
    if ext == "ico":
        im = im.convert("RGBA")
        kw["sizes"] = [s for s in [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
                       if s[0] <= max(im.size)] or [(16, 16)]
    if ext == "tiff":
        kw["compression"] = "tiff_lzw"
    if ext == "pdf":
        kw["resolution"] = 150.0
    if ext == "heic" and not HEIF:
        raise OpError("HEIC support (pillow-heif) is not installed")
    im.save(dst, fmt, **kw)
    return dst


def _q(ctx) -> int:
    return int(ctx.settings.get("image_quality", 85))


def _convert(ext):
    def run(p: Path, opts, ctx):
        return save_image(open_image(p), out_path(p, ext), ext, _q(ctx))
    return run


for _ext in TARGETS:
    register(f"img.to.{_ext}", f"to {_ext.upper()}", "convert", IMG_IN, _convert(_ext),
             badge=_ext.upper(), skip_same=_ext)


# ------------------------------------------------------------------- tools

def _same_ext(p: Path) -> str:
    e = ext_of(p)
    return "png" if e == "svg" else e


def _compress(p: Path, opts, ctx):
    ext = _same_ext(p)
    im = open_image(p)
    dst = out_path(p, ext, "-compressed")
    if ext == "png" and getattr(im, "n_frames", 1) == 1:
        im = im.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        im.save(dst, "PNG", optimize=True)
        return dst
    return save_image(im, dst, ext, quality=65)


def _ask_resize(paths):
    from ..dialogs import slider_dialog
    try:
        w, h = open_image(paths[0]).size
    except Exception:
        w = h = 0

    def describe(v):
        dims = f"{w} × {h} → {max(1, round(w * v / 100))} × {max(1, round(h * v / 100))} px" if w else ""
        return f"{v}%", dims + (f"  ·  {len(paths)} images" if len(paths) > 1 else "")

    pct = slider_dialog("Resize Image", "Scale", 5, 200, 50, describe)
    return None if pct is None else {"pct": pct}


def _resize(p: Path, opts, ctx):
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    pct = opts["pct"] / 100
    im = im.resize((max(1, round(im.width * pct)), max(1, round(im.height * pct))), Image.Resampling.LANCZOS)
    return save_image(im, out_path(p, _same_ext(p), f"-{opts['pct']}pct"), _same_ext(p), _q(ctx))


def _simple(fn, suffix):
    def run(p: Path, opts, ctx):
        im = open_image(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
            im = im.copy()
        return save_image(fn(im), out_path(p, _same_ext(p), suffix), _same_ext(p), _q(ctx))
    return run


def _square(im):
    s = min(im.size)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((left, top, left + s, top + s))


def _strip(im):
    clean = im.copy()
    clean.info = {k: v for k, v in im.info.items() if k in ("transparency", "loop", "duration")}
    return clean


def _grayscale(im):
    return im.convert("LA") if "A" in im.getbands() else im.convert("L")


def _merge_pdf(paths: list[Path], opts, ctx):
    pages = [_flatten(open_image(p)) for p in paths]
    dst = out_path(paths[0], "pdf", "-merged")
    pages[0].save(dst, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return dst


def _make_gif(paths: list[Path], opts, ctx):
    frames = [open_image(p).convert("RGBA") for p in paths]
    w, h = frames[0].size
    frames = [f.resize((w, h), Image.Resampling.LANCZOS) for f in frames]
    dst = out_path(paths[0], "gif", "-animated")
    frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:], duration=500, loop=0, disposal=2)
    return dst


register("img.compress", "Compress", "tools", IMG_IN, _compress)
register("img.resize", "Resize…", "tools", IMG_IN, _resize, prompt=_ask_resize)
register("img.rotate", "Rotate 90°", "tools", IMG_IN,
         _simple(lambda im: im.transpose(Image.Transpose.ROTATE_270), "-rotated"))
register("img.flip", "Flip", "tools", IMG_IN,
         _simple(lambda im: im.transpose(Image.Transpose.FLIP_LEFT_RIGHT), "-flipped"))
register("img.square", "Crop square", "tools", IMG_IN, _simple(_square, "-square"))
register("img.gray", "Grayscale", "tools", IMG_IN, _simple(_grayscale, "-gray"))
register("img.strip", "Strip metadata", "tools", IMG_IN, _simple(_strip, "-clean"))
register("img.mergepdf", "Merge to PDF", "tools", IMG_IN, _merge_pdf, batch=True, min_files=2)
register("img.gif", "Make GIF", "tools", IMG_IN, _make_gif, batch=True, min_files=2)
