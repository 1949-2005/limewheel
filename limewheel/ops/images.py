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
    jpeg = None
    if im.format == "JPEG":
        from PIL import JpegImagePlugin
        jpeg = (im.quantization, JpegImagePlugin.get_sampling(im))
    im = ImageOps.exif_transpose(im)
    if jpeg:
        im.info["src_jpeg"] = jpeg            # survives crop/edit: lets save reuse the source's own quality
    return im


def _normalize_mode(im: Image.Image) -> Image.Image:
    """16-bit greyscale (I;16) would clip to white when converted naively: scale it down properly."""
    if im.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        lo, hi = im.getextrema()
        scale = 1 / 257 if hi > 255 else 1
        out = im.convert("I").point(lambda v: v * scale).convert("L")
        out.info = dict(im.info)
        return out
    return im


def _flatten(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB") if im.mode not in ("RGB", "L") else im


# Microsoft's icon size table (incl. 20/40/96 for 125%/250% scaling); 128 etc. are scaled from 256.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 256)
ICO_SMALL = (16, 20, 24, 32, 48)
_ICO_SHARPEN = {16: 40, 20: 35, 24: 30, 32: 25, 40: 20, 48: 15}     # gentle, colour only, small sizes


def _pad_square(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    if im.width == im.height:
        return im
    side = max(im.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    # no mask: a masked paste onto transparent squares the alpha and darkens soft edges
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return sq


def _ico_frame_png(f: Image.Image) -> bytes:
    """One icon frame as full-colour 32-bit RGBA PNG (no palette reduction: that was the main
    quality loss, and Windows rejects palette PNGs in .ico anyway). Fully transparent pixels get
    their colour zeroed so they compress better."""
    f = Image.composite(f, Image.new("RGBA", f.size, 0), f.getchannel("A").point(lambda v: 255 if v else 0))
    buf = io.BytesIO()
    f.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _ico_frame_bmp(f: Image.Image) -> bytes:
    """One icon frame as a classic 32-bit DIB (BGRA, bottom-up, plus the 1-bit AND mask). Every
    Explorer code path reads these; some shell caches show a blank page for small PNG frames."""
    import struct
    s = f.width
    xor = f.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    row = ((s + 31) // 32) * 4
    alpha = f.getchannel("A").transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
    mask = bytearray()
    for y in range(s):
        bits = bytearray(row)
        for x in range(s):
            if alpha[y * s + x] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    head = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    return head + xor + bytes(mask)


def save_ico(im: Image.Image, dst: Path, sizes=ICO_SIZES, bmp_below: int = 0) -> Path:
    """Write a Windows .ico: square frames, each size rendered from the source with Lanczos
    (small sizes lightly sharpened) and stored as compressed PNG. Frames smaller than
    `bmp_below` are stored as BMP instead (bigger, but safest for app and shortcut icons)."""
    import struct
    from PIL import ImageFilter
    sq = _pad_square(im)
    side = sq.width
    sizes = [s for s in sizes if s <= max(side, 16)] or [16]
    if side < sizes[-1]:
        sizes = [s for s in sizes if s <= side] or [16]
    blobs = []
    for s in sizes:
        f = sq.resize((s, s), Image.Resampling.LANCZOS)          # Pillow premultiplies RGBA itself
        pct = _ICO_SHARPEN.get(s)
        if pct:                                                  # sharpen colour only, never the alpha
            *rgb, a = f.split()
            rgb = Image.merge("RGB", rgb).filter(ImageFilter.UnsharpMask(radius=0.5, percent=pct, threshold=0))
            f = Image.merge("RGBA", (*rgb.split(), a))
        blobs.append((s, _ico_frame_bmp(f) if s < bmp_below else _ico_frame_png(f)))
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for s, data in blobs:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in blobs:
        out += data
    dst.write_bytes(bytes(out))
    return dst


GRAPHIC_SRC = {"png", "gif", "bmp", "tiff", "ico", "svg"}


def save_image(im: Image.Image, dst: Path, ext: str, quality: int = 85, src_ext: str | None = None,
               dpi: int | None = None) -> Path:
    """Save with the best settings for the target, keeping colour profile and EXIF.
    src_ext tells whether the picture came from a graphic (screenshot/logo) or a photo;
    dpi sets the print resolution tag (JPG/PNG/TIFF)."""
    fmt = TARGETS[ext]
    kw = {"dpi": (dpi, dpi)} if dpi and ext in ("jpg", "png", "tiff") else {}
    info = dict(im.info)
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
    if ext == "ico":
        return save_ico(im, dst, ICO_SIZES)
    im = _normalize_mode(im)
    src_jpeg = info.get("src_jpeg")
    icc = info.get("icc_profile")
    exif = im.getexif()
    if 0x0112 in exif:
        del exif[0x0112]                          # pixels are already upright
    if ext in ("jpg", "pdf", "bmp"):
        im = _flatten(im)
    elif im.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
        im = im.convert("RGBA")
    graphic = (src_ext in GRAPHIC_SRC) or (src_ext is None and src_jpeg is None and im.mode in ("RGBA", "LA", "P"))
    if icc and ext in ("jpg", "png", "webp", "avif", "heic", "tiff"):
        kw["icc_profile"] = icc                   # wide-gamut photos keep their colours
    if len(exif) and ext in ("jpg", "png", "webp", "avif", "tiff"):
        kw["exif"] = exif                         # camera, date, GPS… ("Strip metadata" removes it on purpose)
    if ext == "jpg":
        kw.update(optimize=True, progressive=True)
        if src_jpeg and im.mode == "RGB" and len(src_jpeg[0]) >= 2 and src_ext in (None, "jpg"):
            # re-saving an edited JPEG: reuse its own quantisation tables and chroma subsampling,
            # so a crop/edit doesn't add a second generation of loss
            kw.update(qtables=src_jpeg[0], subsampling=max(0, src_jpeg[1]))
        else:
            kw.update(quality=quality, subsampling=0 if (graphic or quality >= 90) else 2)  # 4:4:4 keeps text crisp
    elif ext == "webp":
        kw["method"] = 6
        if graphic:
            kw.update(lossless=True, quality=100, exact=im.mode == "RGBA")   # screenshots/logos: lossless
        else:
            kw["quality"] = quality
    elif ext == "avif":
        kw.update(quality=quality, subsampling="4:4:4", speed=4)
    elif ext == "heic":
        kw.update(quality=quality, chroma=444 if graphic else 420)
    elif ext == "png":
        kw["optimize"] = True
    elif ext == "gif" and im.mode == "RGBA" and im.getextrema()[3][0] == 255:
        im = im.convert("RGB")                    # opaque: Pillow then uses median cut instead of octree
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
        if ext == "pdf":
            return images_to_pdf([p], out_path(p, "pdf"))
        return save_image(open_image(p), out_path(p, ext), ext, _q(ctx), src_ext=ext_of(p))
    return run


for _ext in TARGETS:
    register(f"img.to.{_ext}", f"to {_ext.upper()}", "convert", IMG_IN, _convert(_ext),
             badge=_ext.upper(), skip_same=_ext)
register("img.to.icosmall", "ICO small", "convert", IMG_IN,
         lambda p, o, c: save_ico(open_image(p), out_path(p, "ico", "-small"), ICO_SMALL), badge="ICO S")


# ------------------------------------------------------------------- tools

def _same_ext(p: Path) -> str:
    e = ext_of(p)
    return "png" if e == "svg" else e


def _fast_ssim(a, b) -> float:
    """SSIM on luma over 8x8 blocks (numpy, ~20 ms for 2 MP)."""
    h, w = (a.shape[0] // 8) * 8, (a.shape[1] // 8) * 8
    a = a[:h, :w].reshape(h // 8, 8, w // 8, 8).swapaxes(1, 2).reshape(-1, 64)
    b = b[:h, :w].reshape(h // 8, 8, w // 8, 8).swapaxes(1, 2).reshape(-1, 64)
    ma, mb = a.mean(1), b.mean(1)
    va, vb = a.var(1), b.var(1)
    cov = ((a - ma[:, None]) * (b - mb[:, None])).mean(1)
    c1, c2 = 6.5025, 58.5225
    return float((((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2))).mean())


def _luma(im):
    import numpy as np
    return np.asarray(im.convert("L"), dtype=np.float32)


def quantize_png(im: Image.Image, min_quality: int = 70) -> Image.Image | None:
    """pngquant-quality palette (libimagequant). None when 256 colours can't look right (a photo)."""
    rgba = im.convert("RGBA")
    try:
        import imagequant
        return imagequant.quantize_pil_image(rgba, dithering_level=0.0, max_colors=256, min_quality=min_quality)
    except ImportError:
        if rgba.getextrema()[3][0] == 255:        # opaque: median cut beats octree
            return rgba.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                dither=Image.Dither.FLOYDSTEINBERG)
        return rgba.quantize(256, method=Image.Quantize.FASTOCTREE)
    except RuntimeError:
        return None


def _compress(p: Path, opts, ctx):
    """Smaller file that still looks like the original: PNG via pngquant (or stays lossless),
    JPEG at the lowest quality whose SSIM stays >= 0.985, plus a lossless mozjpeg pass.
    Never writes a file that is bigger than the source."""
    ext = _same_ext(p)
    im = open_image(p)
    dst = out_path(p, ext, "-compressed")
    src_size = p.stat().st_size
    if ext == "png" and getattr(im, "n_frames", 1) == 1:
        q = quantize_png(im)
        cands = []
        for cand in ([q] if q is not None else []) + [im]:
            buf = io.BytesIO()
            cand.save(buf, "PNG", optimize=True)
            cands.append(buf.getvalue())
        data = min(cands, key=len)
        if len(data) >= src_size:
            raise OpError("This PNG is already as small as it can get without visible loss")
        dst.write_bytes(data)
        return dst
    if ext == "jpg" and getattr(im, "n_frames", 1) == 1:
        rgb = _flatten(im)
        ref = _luma(rgb)
        best = None
        lo, hi = 60, 92                           # lowest quality that still looks like the original
        while lo <= hi:
            q = (lo + hi) // 2
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=q, optimize=True, progressive=True, icc_profile=im.info.get("icc_profile"))
            if _fast_ssim(ref, _luma(Image.open(buf))) >= 0.985:
                best, hi = buf.getvalue(), q - 1
            else:
                lo = q + 1
        lossless = None
        try:                                      # lossless repack of the original (mozjpeg) never hurts
            import mozjpeg_lossless_optimization as moz
            lossless = moz.optimize(p.read_bytes(), moz.COPY_MARKERS.ALL)
            best = moz.optimize(best, moz.COPY_MARKERS.ALL) if best else None
        except (ImportError, ValueError):
            pass
        options = [d for d in (best, lossless) if d]
        data = min(options, key=len) if options else p.read_bytes()
        if len(data) >= src_size:
            raise OpError("This JPEG is already as small as it can get without visible loss")
        dst.write_bytes(data)
        return dst
    return save_image(im, dst, ext, quality=80, src_ext=ext_of(p))


# ------------------------------------------------------------------- lossless JPEG rotate / flip

def _orient_table():
    """(transpose op, current EXIF orientation) -> new orientation, derived from Pillow itself."""
    base = Image.frombytes("L", (3, 2), bytes(range(6)))

    def view(o):
        im = base.copy()
        ex = im.getexif()
        ex[0x0112] = o
        im.info["exif"] = ex.tobytes()
        v = ImageOps.exif_transpose(im)
        return v.size, v.tobytes()

    views = {o: view(o) for o in range(1, 9)}
    tab = {}
    for op in Image.Transpose:
        for o in range(1, 9):
            w = Image.frombytes("L", views[o][0], views[o][1]).transpose(op)
            tab[op, o] = next(n for n, v in views.items() if v == (w.size, w.tobytes()))
    return tab


_OT = None


def jpeg_transpose_lossless(src: Path, dst: Path, op) -> Path:
    """Rotate/flip a JPEG with zero quality loss: only the EXIF Orientation tag is rewritten;
    the compressed picture (and ICC/XMP) is copied byte for byte."""
    import struct
    global _OT
    if _OT is None:
        _OT = _orient_table()
    data = src.read_bytes()
    with Image.open(src) as im:
        ex = im.getexif()
    ex[0x0112] = _OT[op, ex.get(0x0112, 1) or 1]
    payload = ex.tobytes()
    seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] not in (0xDA, 0xD9):
        ln = struct.unpack(">H", data[i + 2:i + 4])[0]
        chunk = data[i:i + 2 + ln]
        if seg and data[i + 1] != 0xE0:           # new EXIF goes right after the JFIF header
            out += seg
            seg = b""
        if not (data[i + 1] == 0xE1 and chunk[4:10] == b"Exif\x00\x00"):
            out += chunk
        i += 2 + ln
    out += seg + data[i:]
    dst.write_bytes(bytes(out))
    return dst


def _transpose(op, suffix):
    def run(p: Path, opts, ctx):
        ext = _same_ext(p)
        if ext == "jpg":
            return jpeg_transpose_lossless(p, out_path(p, "jpg", suffix), op)
        im = open_image(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
            im = im.copy()
        return save_image(im.transpose(op), out_path(p, ext, suffix), ext, _q(ctx), src_ext=ext_of(p))
    return run


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


A4 = (595.0, 842.0)          # points
PDF_MARGIN = 36.0            # half an inch


def _pdf_image_bytes(p: Path) -> tuple[bytes, int, int]:
    """Image data for a PDF page with no quality loss where possible:
    an upright JPEG is embedded byte-for-byte; anything else is stored lossless (PNG),
    unless it's a photo where lossless would be far bigger — then high-quality JPEG."""
    if ext_of(p) == "jpg":
        with Image.open(p) as im:
            if getattr(im, "n_frames", 1) == 1 and im.getexif().get(0x0112, 1) == 1 and im.mode in ("RGB", "L", "CMYK"):
                return p.read_bytes(), im.width, im.height
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    im = im.convert("RGBA") if ("A" in im.getbands() or im.mode == "P") else im.convert("RGB")
    png = io.BytesIO()
    im.save(png, "PNG", optimize=True)
    jpg = io.BytesIO()
    _flatten(im).save(jpg, "JPEG", quality=92, optimize=True, subsampling=0)
    data = png.getvalue() if len(png.getvalue()) <= 2.5 * len(jpg.getvalue()) else jpg.getvalue()
    return data, im.width, im.height


def images_to_pdf(paths: list[Path], dst: Path) -> Path:
    """One A4 page per image (portrait or landscape to suit it), image fitted inside the margins."""
    import pymupdf as fitz
    doc = fitz.open()
    for p in paths:
        data, w, h = _pdf_image_bytes(p)
        pw, ph = A4 if h >= w else (A4[1], A4[0])
        page = doc.new_page(width=pw, height=ph)
        k = min((pw - 2 * PDF_MARGIN) / w, (ph - 2 * PDF_MARGIN) / h)
        iw, ih = w * k, h * k
        x, y = (pw - iw) / 2, (ph - ih) / 2
        page.insert_image(fitz.Rect(x, y, x + iw, y + ih), stream=data, keep_proportion=True)
    doc.set_metadata({"title": paths[0].stem, "producer": "", "creator": ""})
    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return dst


def _merge_pdf(paths: list[Path], opts, ctx):
    return images_to_pdf(paths, out_path(paths[0], "pdf", "-merged"))


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
    src = _first_frame(p)
    res = adjust(src, opts["values"])
    res.info.update({k: v for k, v in src.info.items() if k in ("src_jpeg", "icc_profile", "exif")})
    return save_image(res, out_path(p, ext, "-edited"), ext, _q(ctx))


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
        src = _first_frame(p)
        res = render_markup(src, opts["shapes"])
        res.info.update({k: v for k, v in src.info.items() if k in ("src_jpeg", "icc_profile", "exif")})
        return save_image(res, out_path(p, ext, suffix), ext, _q(ctx))
    return run


register("img.edit", "Edit…", "tools", IMG_IN, _edit, prompt=_ask_edit, max_files=1)
register("img.crop", "Crop…", "tools", IMG_IN, _crop, prompt=_ask_crop, max_files=1)
register("img.redact", "Redact…", "tools", IMG_IN, _markup("-redacted"), prompt=_ask_markup("redact"),
         max_files=1, main_thread=True)
register("img.annotate", "Annotate…", "tools", IMG_IN, _markup("-annotated"), prompt=_ask_markup("annotate"),
         max_files=1, main_thread=True)
register("img.background", "Add BG…", "tools", IMG_IN, _background, prompt=_ask_background)
register("img.compress", "Compress", "tools", IMG_IN - {"svg"}, _compress)
register("img.resize", "Resize…", "tools", IMG_IN, _resize, prompt=_ask_resize)
register("img.rotate", "Rotate 90°", "tools", IMG_IN, _transpose(Image.Transpose.ROTATE_270, "-rotated"))
register("img.flip", "Flip", "tools", IMG_IN, _transpose(Image.Transpose.FLIP_LEFT_RIGHT, "-flipped"))
register("img.gray", "Grayscale", "tools", IMG_IN, _simple(_grayscale, "-gray"))
register("img.strip", "Strip metadata", "tools", IMG_IN, _simple(_strip, "-clean"))
register("img.mergepdf", "Merge to PDF", "tools", IMG_IN, _merge_pdf, batch=True, min_files=2)
register("img.gif", "Make GIF", "tools", IMG_IN, _make_gif, batch=True, min_files=2)
