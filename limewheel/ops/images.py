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
        # Windows wants square icons: centre the picture on a transparent square first,
        # then render every size ourselves (Lanczos + a light sharpen on the small ones).
        from PIL import ImageFilter
        im = im.convert("RGBA")
        side = max(im.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
        sizes = [s for s in (16, 24, 32, 48, 64, 128, 256) if s <= max(side, 16)] or [16]
        frames = []
        for s in sizes:
            f = square.resize((s, s), Image.Resampling.LANCZOS)
            if s <= 64:
                f = f.filter(ImageFilter.UnsharpMask(radius=0.8, percent=60, threshold=1))
            frames.append(f)
        big = frames[-1]
        big.save(dst, "ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
        return dst
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


def _ask_background(paths):
    from ..dialogs import background_dialog
    return background_dialog()


def _background(p: Path, opts, ctx):
    from PIL import ImageColor, ImageDraw, ImageFilter
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    im = im.convert("RGBA")
    w, h = im.size
    pad = int(round(max(w, h) * opts["pad"]))
    cw, ch = w + 2 * pad, h + 2 * pad
    if opts.get("square"):
        cw = ch = max(cw, ch)
    transparent = opts["color"] == "transparent"
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0) if transparent else ImageColor.getrgb(opts["color"]) + (255,))
    ox, oy = (cw - w) // 2, (ch - h) // 2
    radius = int(min(w, h) * 0.06) if opts.get("rounded") else 0
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    mask = Image.composite(im.getchannel("A"), Image.new("L", (w, h), 0), mask)
    if opts.get("shadow") and pad:
        blur = max(4, pad // 3)
        sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        shadow_mask = Image.new("L", (cw, ch), 0)
        shadow_mask.paste(mask, (ox, oy + blur // 2))
        sh.putalpha(shadow_mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * 0.45)))
        canvas = Image.alpha_composite(canvas, sh)
    canvas.paste(im, (ox, oy), mask)
    ext = "png" if transparent or _same_ext(p) in ("gif", "ico", "svg") else _same_ext(p)
    return save_image(canvas, out_path(p, ext, "-bg"), ext, _q(ctx))


def _first_frame(p: Path) -> Image.Image:
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    return im


def _ask_edit(paths):
    from ..editors import photo_editor
    return photo_editor(_first_frame(paths[0]))


def _edit(p: Path, opts, ctx):
    from ..editors import adjust
    ext = _same_ext(p)
    return save_image(adjust(_first_frame(p), opts["values"]), out_path(p, ext, "-edited"), ext, _q(ctx))


def _ask_crop(paths):
    from ..editors import crop_editor, pil_to_qimage, preview_copy
    im = _first_frame(paths[0])
    small, _ = preview_copy(im)
    return crop_editor(pil_to_qimage(small), im.size, "Crop Image")


def _crop(p: Path, opts, ctx):
    x, y, w, h = opts["box"]
    ext = _same_ext(p)
    return save_image(_first_frame(p).crop((x, y, x + w, y + h)), out_path(p, ext, "-cropped"), ext, _q(ctx))


def _ask_markup(mode):
    def prompt(paths):
        from ..editors import markup_editor
        return markup_editor(_first_frame(paths[0]), mode)
    return prompt


def _markup(suffix):
    def run(p: Path, opts, ctx):
        from ..editors import render_markup
        ext = _same_ext(p)
        return save_image(render_markup(_first_frame(p), opts["shapes"]), out_path(p, ext, suffix), ext, _q(ctx))
    return run


register("img.edit", "Edit…", "tools", IMG_IN, _edit, prompt=_ask_edit, max_files=1)
register("img.crop", "Crop…", "tools", IMG_IN, _crop, prompt=_ask_crop, max_files=1)
register("img.redact", "Redact…", "tools", IMG_IN, _markup("-redacted"), prompt=_ask_markup("redact"),
         max_files=1, main_thread=True)
register("img.annotate", "Annotate…", "tools", IMG_IN, _markup("-annotated"), prompt=_ask_markup("annotate"),
         max_files=1, main_thread=True)
register("img.background", "Add BG…", "tools", IMG_IN, _background, prompt=_ask_background)
register("img.compress", "Compress", "tools", IMG_IN, _compress)
register("img.resize", "Resize…", "tools", IMG_IN, _resize, prompt=_ask_resize)
register("img.rotate", "Rotate 90°", "tools", IMG_IN,
         _simple(lambda im: im.transpose(Image.Transpose.ROTATE_270), "-rotated"))
register("img.flip", "Flip", "tools", IMG_IN,
         _simple(lambda im: im.transpose(Image.Transpose.FLIP_LEFT_RIGHT), "-flipped"))
register("img.gray", "Grayscale", "tools", IMG_IN, _simple(_grayscale, "-gray"))
register("img.strip", "Strip metadata", "tools", IMG_IN, _simple(_strip, "-clean"))
register("img.mergepdf", "Merge to PDF", "tools", IMG_IN, _merge_pdf, batch=True, min_files=2)
register("img.gif", "Make GIF", "tools", IMG_IN, _make_gif, batch=True, min_files=2)
