"""1.2 tools: compress to an exact size, metadata viewer/editor, reorganize PDF, collage,
QR/barcode reader and raster -> vector SVG."""
from __future__ import annotations

import io
import math
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .base import CREATE_NO_WINDOW, OpError, ext_of, out_path, register
from .docs import PDF, _fitz
from .images import IMG_IN, _flatten, open_image, save_image
from .media import AUDIO_IN, VIDEO_IN, ff, ffmpeg, probe

VID = VIDEO_IN - {"gif"}
LOSSY = {"jpg": "JPEG", "webp": "WEBP", "avif": "AVIF", "heic": "HEIF"}


def _human(n: float) -> str:
    return f"{n / (1 << 20):.1f} MB" if n >= 1 << 20 else f"{n / 1024:.0f} KB"


def _size_tag(n: int) -> str:
    return f"-{n / (1 << 20):.1f}MB" if n >= 1 << 20 else f"-{n // 1024}KB"


# =================================================================== compress to size

def _ask_size(kind):
    def prompt(paths):
        from ..tools_ui import target_size_dialog
        return target_size_dialog(paths[0].stat().st_size, kind, len(paths))
    return prompt


def _encode(im: Image.Image, ext: str, q: int) -> bytes:
    buf = io.BytesIO()
    if ext in LOSSY:
        img = _flatten(im) if ext == "jpg" else im
        img.save(buf, LOSSY[ext], quality=q, **({"optimize": True} if ext == "jpg" else {}))
    else:                                            # PNG & friends: palette first, then size
        img = im.convert("RGBA").quantize(colors=max(8, q * 256 // 100), method=Image.Quantize.FASTOCTREE)
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def fit_bytes(im: Image.Image, ext: str, target: int) -> bytes | None:
    """Highest quality (then, if needed, the largest size) whose encoding fits in `target` bytes."""
    best = None
    scale = 1.0
    while scale > 0.05:
        work = im if scale == 1 else im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                                               Image.Resampling.LANCZOS)
        lo, hi = 5, 95
        while lo <= hi:                              # largest quality that still fits
            q = (lo + hi) // 2
            data = _encode(work, ext, q)
            if len(data) <= target:
                best, lo = data, q + 1
            else:
                hi = q - 1
        if best:
            break
        scale *= 0.8                                 # even the lowest quality is too big: shrink and retry
    return best


def _img_to_size(p: Path, opts, ctx):
    target = opts["bytes"]
    if p.stat().st_size <= target:
        raise OpError(f"Already {p.stat().st_size / 1024:.0f} KB — smaller than the {target / 1024:.0f} KB target")
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    ext = ext_of(p) if ext_of(p) in LOSSY or ext_of(p) == "png" else "png"
    best = fit_bytes(im, ext, target)
    if not best:
        raise OpError(f"Couldn't get this image under {target // 1024} KB")
    dst = out_path(p, ext, _size_tag(target))
    dst.write_bytes(best)
    return dst


def _av_to_size(p: Path, opts, ctx):
    target = opts["bytes"]
    if p.stat().st_size <= target:
        raise OpError(f"Already {p.stat().st_size / 1024:.0f} KB — smaller than the {target / 1024:.0f} KB target")
    info = probe(p)
    dur = info["duration"]
    if not dur:
        raise OpError("Couldn't read the length of this file")
    total_kbps = target * 8 / 1000 / dur * 0.95          # leave room for the container
    if not info["video"]:
        kbps = int(min(320, total_kbps))
        if kbps < 16:
            raise OpError(f"{_human(target)} is too small for {dur:.0f} s of audio "
                          f"(try at least {_human(16000 * dur / 8 / 0.95)})")
        ext = "m4a" if ext_of(p) == "m4a" else "mp3"
        dst = out_path(p, ext, _size_tag(target))
        codec = ["-c:a", "aac"] if ext == "m4a" else ["-c:a", "libmp3lame"]
        ff("-i", p, "-vn", *codec, "-b:a", f"{kbps}k", dst)
        return dst
    a_kbps = 96 if info["audio"] else 0
    if a_kbps and total_kbps < 300:
        a_kbps = 48
    v_kbps = total_kbps - a_kbps
    if v_kbps < 40:
        raise OpError(f"{_human(target)} is too small for a {dur:.0f} s video "
                      f"(try at least {_human(88000 * dur / 8 / 0.95)})")
    w, h = info["size"]
    height = h
    for limit, kb in ((1080, 2500), (720, 1200), (480, 500), (360, 0)):
        if v_kbps < kb and h > limit:
            height = limit if h > limit else h
    vf = ["-vf", f"scale=-2:{min(h, height)}"] if height and height < h else []
    dst = out_path(p, "mp4", _size_tag(target))
    with tempfile.TemporaryDirectory() as tmp:
        log = str(Path(tmp) / "pass")
        for attempt in range(2):
            common = ["-i", p, *vf, "-c:v", "libx264", "-preset", "medium", "-b:v", f"{int(v_kbps)}k",
                      "-pix_fmt", "yuv420p", "-passlogfile", log]
            ff(*common, "-pass", "1", "-an", "-f", "mp4", "NUL")
            audio = ["-c:a", "aac", "-b:a", f"{a_kbps}k"] if a_kbps else ["-an"]
            ff(*common, "-pass", "2", *audio, "-movflags", "+faststart", dst)
            if dst.stat().st_size <= target * 1.02:
                break
            v_kbps *= target / dst.stat().st_size * 0.95
    return dst


def _pdf_to_size(p: Path, opts, ctx):
    target = opts["bytes"]
    if p.stat().st_size <= target:
        raise OpError(f"Already {p.stat().st_size / 1024:.0f} KB — smaller than the {target / 1024:.0f} KB target")
    fitz = _fitz()
    best = None
    for dpi, quality in ((200, 85), (150, 75), (120, 65), (96, 55), (72, 45), (60, 35), (48, 25)):
        doc = fitz.open(p)
        try:
            if hasattr(doc, "rewrite_images"):
                doc.rewrite_images(dpi_threshold=dpi + 10, dpi_target=dpi, quality=quality,
                                   lossy=True, lossless=dpi <= 96, bitonal=False)
            data = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)
        finally:
            doc.close()
        best = data if best is None or len(data) < len(best) else best
        if len(data) <= target:
            best = data
            break
    if len(best) > target:
        raise OpError(f"The smallest this PDF goes is {len(best) / 1024:.0f} KB (its text and fonts can't shrink further)")
    dst = out_path(p, "pdf", _size_tag(target))
    dst.write_bytes(best)
    return dst


register("img.tosize", "To size…", "tools", IMG_IN - {"svg"}, _img_to_size, prompt=_ask_size("image"))
register("vid.tosize", "To size…", "tools", VID, _av_to_size, prompt=_ask_size("video"))
register("aud.tosize", "To size…", "tools", AUDIO_IN, _av_to_size, prompt=_ask_size("audio"))
register("pdf.tosize", "To size…", "tools", PDF, _pdf_to_size, prompt=_ask_size("pdf"))


# =================================================================== metadata

EXIF_EDIT = [("title", "Title"), ("author", "Author"), ("copyright", "Copyright"),
             ("date", "Date taken (YYYY:MM:DD HH:MM:SS)"), ("comment", "Comment")]


def _xp(b) -> str:
    try:
        return bytes(b).decode("utf-16-le").rstrip("\x00")
    except Exception:
        return ""


def _image_meta(p: Path):
    from PIL.ExifTags import GPSTAGS, TAGS
    im = Image.open(p)
    exif = im.getexif()
    ifd = exif.get_ifd(0x8769)
    gps = exif.get_ifd(0x8825)
    info = [("Format", f"{im.format} · {im.width}×{im.height} px · {im.mode}"), ("File size", f"{p.stat().st_size / 1024:.0f} KB")]
    for tag, val in list(exif.items()) + list(ifd.items()):
        name = TAGS.get(tag, hex(tag))
        if isinstance(val, bytes) or name in ("ExifOffset", "GPSInfo", "MakerNote") or name.startswith("XP"):
            continue
        info.append((name, str(val)[:120]))
    if gps:
        def deg(v, ref):
            try:
                d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
                return -d if ref in ("S", "W") else d
            except Exception:
                return None
        lat, lon = deg(gps.get(2), gps.get(1)), deg(gps.get(4), gps.get(3))
        info.append(("GPS location", f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else "present"))
    values = {"title": _xp(exif.get(0x9C9B, b"")) or str(exif.get(0x010E, "")),
              "author": str(exif.get(0x013B, "")), "copyright": str(exif.get(0x8298, "")),
              "date": str(ifd.get(0x9003, exif.get(0x0132, ""))), "comment": _xp(exif.get(0x9C9C, b""))}
    return info, values, bool(gps)


def _media_meta(p: Path):
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True, creationflags=CREATE_NO_WINDOW)
    text = r.stderr.decode("utf-8", "replace")
    head = text.split("Stream #", 1)[0]
    tags = {}
    for m in re.finditer(r"^\s{4}(\w+)\s*:\s?(.*)$", head, re.M):
        tags.setdefault(m[1].lower(), m[2].strip())
    info = [("File size", f"{p.stat().st_size / (1 << 20):.2f} MB")]
    d = re.search(r"Duration: ([\d:.]+), .*?bitrate: (\S+ \S+)", text)
    if d:
        info.append(("Duration", d[1]))
        info.append(("Bitrate", d[2]))
    for s in re.findall(r"Stream #\S+: (\w+: [^\n]+)", text):
        info.append(("Stream", s[:140]))
    for k, v in tags.items():
        info.append((k, v))
    values = {k: tags.get(k, "") for k in ("title", "artist", "album", "date", "genre", "comment")}
    return info, values


def _pdf_meta(p: Path):
    fitz = _fitz()
    with fitz.open(p) as d:
        m = d.metadata or {}
        info = [("Pages", str(d.page_count)), ("Encrypted", "yes" if d.is_encrypted else "no"),
                ("File size", f"{p.stat().st_size / 1024:.0f} KB")]
        info += [(k, v) for k, v in m.items() if v and k not in ("title", "author", "subject", "keywords")]
    return info, {k: m.get(k, "") for k in ("title", "author", "subject", "keywords")}


def _ask_meta(paths):
    from ..tools_ui import metadata_dialog
    p = paths[0]
    e = ext_of(p)
    if e in IMG_IN:
        if e in ("svg", "gif", "ico", "bmp"):
            raise OpError(f"{e.upper()} files don't carry editable photo metadata")
        info, values, has_gps = _image_meta(p)
        return metadata_dialog("Photo Metadata", info, [(k, l, values[k]) for k, l in EXIF_EDIT],
                               [("drop_gps", "Remove GPS location", has_gps)] if has_gps else [])
    if e == "pdf":
        info, values = _pdf_meta(p)
        return metadata_dialog("PDF Metadata", info, [(k, k.capitalize(), values[k]) for k in values])
    info, values = _media_meta(p)
    return metadata_dialog("Media Metadata", info, [(k, k.capitalize(), values[k]) for k in values])


def _write_image_meta(p: Path, opts) -> Path:
    import piexif
    e = ext_of(p)
    dst = out_path(p, e, "-meta")
    f, strip = opts.get("fields", {}), opts.get("strip")
    if e == "jpg":                                   # JPEG: rewrite the EXIF block only (no re-encode)
        data = p.read_bytes()
        if strip:
            out = io.BytesIO()
            piexif.remove(data, out)
            dst.write_bytes(out.getvalue())
            return dst
        try:
            ex = piexif.load(data)
        except Exception:
            ex = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        z, x = ex.setdefault("0th", {}), ex.setdefault("Exif", {})
        enc = lambda s: s.encode("utf-8")  # noqa: E731
        xp = lambda s: tuple((s + "\x00").encode("utf-16-le"))  # noqa: E731
        for key, tag, conv in (("title", piexif.ImageIFD.ImageDescription, enc), ("author", piexif.ImageIFD.Artist, enc),
                               ("copyright", piexif.ImageIFD.Copyright, enc)):
            if f.get(key):
                z[tag] = conv(f[key])
            else:
                z.pop(tag, None)
        for key, tag in (("title", piexif.ImageIFD.XPTitle), ("comment", piexif.ImageIFD.XPComment),
                         ("author", piexif.ImageIFD.XPAuthor)):
            if f.get(key):
                z[tag] = xp(f[key])
            else:
                z.pop(tag, None)
        if f.get("date"):
            x[piexif.ExifIFD.DateTimeOriginal] = f["date"].encode()
        if opts.get("checks", {}).get("drop_gps"):
            ex["GPS"] = {}
        ex.pop("thumbnail", None) if ex.get("thumbnail") is None else None
        try:
            blob = piexif.dump(ex)
        except Exception:
            ex.pop("Interop", None)
            ex["1st"], ex["thumbnail"] = {}, None
            blob = piexif.dump(ex)
        out = io.BytesIO()
        piexif.insert(blob, data, out)
        dst.write_bytes(out.getvalue())
        return dst
    im = Image.open(p)
    im.load()
    exif = Image.Exif() if strip else im.getexif()
    if not strip:
        for key, tag in (("title", 0x010E), ("author", 0x013B), ("copyright", 0x8298)):
            if f.get(key):
                exif[tag] = f[key]
            elif tag in exif:
                del exif[tag]
        for key, tag in (("title", 0x9C9B), ("comment", 0x9C9C), ("author", 0x9C9D)):
            if f.get(key):
                exif[tag] = (f[key] + "\x00").encode("utf-16-le")
            elif tag in exif:
                del exif[tag]
        if f.get("date"):
            exif.get_ifd(0x8769)[0x9003] = f["date"]
        if opts.get("checks", {}).get("drop_gps") and 0x8825 in exif:
            del exif[0x8825]
    kw = {"exif": exif.tobytes()}
    if e in ("webp", "avif", "heic"):
        kw["quality"] = 95
    if e == "png":
        kw["optimize"] = True
    if e == "tiff":
        kw["compression"] = "tiff_lzw"
    im.save(dst, {"png": "PNG", "webp": "WEBP", "avif": "AVIF", "heic": "HEIF", "tiff": "TIFF"}[e], **kw)
    return dst


def _meta(p: Path, opts, ctx):
    e = ext_of(p)
    if e in IMG_IN:
        return _write_image_meta(p, opts)
    if e == "pdf":
        fitz = _fitz()
        dst = out_path(p, "pdf", "-meta")
        with fitz.open(p) as d:
            if opts.get("strip"):
                d.set_metadata({})
                try:
                    d.del_xml_metadata()
                except Exception:
                    pass
            else:
                m = dict(d.metadata or {})
                m.update({k: v for k, v in opts.get("fields", {}).items()})
                d.set_metadata(m)
            d.save(dst, garbage=3, deflate=True)
        return dst
    dst = out_path(p, ext_of(p), "-meta")
    if opts.get("strip"):
        ff("-i", p, "-map", "0", "-map_metadata", "-1", "-map_chapters", "-1", "-c", "copy", dst)
    else:
        args = []
        for k, v in opts.get("fields", {}).items():
            args += ["-metadata", f"{k}={v}"]
        ff("-i", p, "-map", "0", "-c", "copy", *args, dst)
    return dst


META_IN = (IMG_IN - {"svg", "gif", "ico", "bmp"}) | VID | AUDIO_IN | PDF
for _kind, _set in (("img", IMG_IN - {"svg", "gif", "ico", "bmp"}), ("vid", VID), ("aud", AUDIO_IN), ("pdf", PDF)):
    register(f"{_kind}.meta", "Metadata…", "tools", _set, _meta, prompt=_ask_meta, max_files=1)


# =================================================================== reorganize PDF

def _ask_organize(paths):
    from PySide6.QtGui import QImage, QPixmap
    from ..tools_ui import organize_dialog
    fitz = _fitz()
    thumbs = []
    with fitz.open(paths[0]) as d:
        if d.page_count > 400:
            raise OpError("This PDF has more than 400 pages; split it first")
        for page in d:
            pix = page.get_pixmap(matrix=fitz.Matrix(160 / max(page.rect.width, page.rect.height),
                                                     160 / max(page.rect.width, page.rect.height)))
            img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                         QImage.Format_RGBA8888 if pix.alpha else QImage.Format_RGB888).copy()
            thumbs.append(QPixmap.fromImage(img))
    return organize_dialog(thumbs)


def _organize(p: Path, opts, ctx):
    fitz = _fitz()
    order = opts["order"]
    dst = out_path(p, "pdf", "-organized")
    with fitz.open(p) as src:
        out = fitz.open()
        for idx, rot in order:
            out.insert_pdf(src, from_page=int(idx), to_page=int(idx))
            page = out[-1]
            page.set_rotation((page.rotation + int(rot)) % 360)
        out.save(dst, garbage=3, deflate=True)
        out.close()
    return dst


register("pdf.organize", "Reorganize…", "tools", PDF, _organize, prompt=_ask_organize, max_files=1)


# =================================================================== collage

def compose_collage(images: list[Image.Image], o: dict, width: int) -> Image.Image:
    from PIL import ImageColor
    gap = o.get("gap", 16)
    n = len(images)
    ims = [im.convert("RGBA") for im in images]
    if o["layout"] == "Row":
        inner = width - gap * (n + 1)
        ratios = [im.width / im.height for im in ims]
        h = max(1, int(inner / sum(ratios)))
        cells = [(int(h * r), h) for r in ratios]
        H = h + 2 * gap
        boxes, x = [], gap
        for w, hh in cells:
            boxes.append((x, gap, w, hh))
            x += w + gap
    elif o["layout"] == "Column":
        w = width - 2 * gap
        cells = [(w, max(1, int(w * im.height / im.width))) for im in ims]
        H = sum(c[1] for c in cells) + gap * (n + 1)
        boxes, y = [], gap
        for ww, hh in cells:
            boxes.append((gap, y, ww, hh))
            y += hh + gap
    else:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cell = max(1, (width - gap * (cols + 1)) // cols)
        H = rows * cell + gap * (rows + 1)
        boxes = [(gap + (i % cols) * (cell + gap), gap + (i // cols) * (cell + gap), cell, cell) for i in range(n)]
    bg = o.get("background", "#FFFFFF")
    canvas = Image.new("RGBA", (width, H), (0, 0, 0, 0) if bg == "transparent" else ImageColor.getrgb(bg) + (255,))
    for im, (x, y, w, h) in zip(ims, boxes):
        k = max(w / im.width, h / im.height)                   # cover the cell, then centre-crop
        r = im.resize((max(1, math.ceil(im.width * k)), max(1, math.ceil(im.height * k))), Image.Resampling.LANCZOS)
        left, top = (r.width - w) // 2, (r.height - h) // 2
        r = r.crop((left, top, left + w, top + h))
        mask = r.getchannel("A")
        if o.get("rounded"):
            m = Image.new("L", (w, h), 0)
            ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=max(4, min(w, h) // 14), fill=255)
            from PIL import ImageChops
            mask = ImageChops.multiply(mask, m)
        canvas.paste(r, (x, y), mask)
    return canvas


def _ask_collage(paths):
    from ..tools_ui import collage_dialog
    ims = []
    for p in paths:
        im = open_image(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
            im = im.copy()
        ims.append(im)
    return collage_dialog(ims, compose_collage)


def _collage(paths: list[Path], opts, ctx):
    ims = []
    for p in paths:
        im = open_image(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
            im = im.copy()
        ims.append(im)
    out = compose_collage(ims, opts, opts.get("width", 2048))
    if opts.get("background") == "transparent":
        return save_image(out, out_path(paths[0], "png", "-collage"), "png")
    return save_image(out, out_path(paths[0], "jpg", "-collage"), "jpg", 92)


register("img.collage", "Collage…", "tools", IMG_IN, _collage, prompt=_ask_collage, batch=True, min_files=2)


# =================================================================== QR / barcodes

def read_codes(p: Path) -> list[tuple[str, str]]:
    import zxingcpp
    pages = []
    if ext_of(p) == "pdf":
        fitz = _fitz()
        with fitz.open(p) as d:
            for page in list(d)[:50]:
                pix = page.get_pixmap(dpi=200)
                pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    else:
        im = open_image(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        pages.append(_flatten(im.copy()).convert("RGB"))
    found = []
    for img in pages:
        for scale in (1.0, 0.5, 2.0):                         # tiny or huge codes read better rescaled
            work = img if scale == 1 else img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
            res = zxingcpp.read_barcodes(work)
            if res:
                for r in res:
                    item = (r.format.name.replace("QRCode", "QR code"), r.text)
                    if item not in found:
                        found.append(item)
                break
    return found


def _qr(p: Path, opts, ctx):
    codes = read_codes(p)
    if not codes:
        raise OpError("No QR code or barcode found")
    if getattr(getattr(ctx, "app", None), "wheel", None) is not None:      # the real app: show a window
        from ..tools_ui import codes_dialog
        codes_dialog(codes, p.name)
        return []
    dst = out_path(p, "txt", "-codes")                       # headless (tests): write them out
    dst.write_text("\n".join(f"{f}: {t}" for f, t in codes), encoding="utf-8")
    return dst


register("img.qr", "Read QR", "tools", IMG_IN, _qr, main_thread=True, max_files=1)
register("pdf.qr", "Read QR", "tools", PDF, _qr, main_thread=True, max_files=1)


# =================================================================== raster -> SVG

def _lab(rgb):
    """sRGB (N,3 uint8) -> CIE Lab (D65)."""
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192], [0.1805, 0.0722, 0.9505]])
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def _palette(rgb, colors, min_delta_e=6.0):
    """Over-quantize to 64 colours, then Ward-merge clusters in Lab space. Ward weighs colour
    distance by area, so a small but distinct colour (an eye, a logo dot, a thin stem) survives
    while the many steps of a smooth gradient get merged first. Returns (labels HxW, rgb palette)."""
    q = rgb.quantize(64, method=Image.Quantize.MEDIANCUT, kmeans=2, dither=Image.Dither.NONE)
    idx = np.asarray(q)
    pal = np.array(q.getpalette()[:192], dtype=np.float64).reshape(-1, 3)
    n = np.bincount(idx.ravel(), minlength=len(pal)).astype(np.float64)
    live = [i for i in range(len(pal)) if n[i] > 0]
    lab = {i: _lab(pal[i:i + 1])[0] for i in live}
    col = {i: pal[i].copy() for i in live}
    cnt = {i: n[i] for i in live}
    parent = np.arange(len(pal))
    while len(live) > 1:
        best = None
        for a_ in range(len(live)):
            for b_ in range(a_ + 1, len(live)):
                a, b = live[a_], live[b_]
                d2 = float(((lab[a] - lab[b]) ** 2).sum())
                cost = d2 * cnt[a] * cnt[b] / (cnt[a] + cnt[b])
                if best is None or cost < best[0]:
                    best = (cost, a, b, d2)
        _, a, b, d2 = best
        if len(live) <= colors and d2 > min_delta_e ** 2:
            break
        t = cnt[a] + cnt[b]
        lab[a] = (lab[a] * cnt[a] + lab[b] * cnt[b]) / t
        col[a] = (col[a] * cnt[a] + col[b] * cnt[b]) / t
        cnt[a] = t
        parent[parent == b] = a
        live.remove(b)
    remap = np.zeros(len(pal), dtype=np.int32)
    out_pal = []
    for k, i in enumerate(sorted(live, key=lambda i: -cnt[i])):   # biggest area first
        remap[parent == i] = k
        out_pal.append(tuple(int(round(v)) for v in col[i]))
    return remap[idx], out_pal


def trace_svg(im: Image.Image, colors: int = 16, max_side: int = 800) -> str:
    """Colour vector tracing: perceptual palette, then potrace on *stacked* layers - each layer
    also covers every layer drawn above it, so neighbouring shapes overlap instead of leaving
    hairline gaps (no outline stroke needed, so fine details keep their true width)."""
    import potrace
    W, H = im.size
    alpha = np.asarray(im.convert("RGBA").getchannel("A")) if "A" in im.getbands() or im.mode == "P" else None
    rgb = _flatten(im).convert("RGB")
    k = min(1.0, max_side / max(rgb.size))
    if k < 1:
        size = (max(1, round(W * k)), max(1, round(H * k)))
        rgb = rgb.resize(size, Image.Resampling.LANCZOS)
        if alpha is not None:
            alpha = np.asarray(Image.fromarray(alpha).resize(size, Image.Resampling.LANCZOS))
    rgb = rgb.filter(ImageFilter.MedianFilter(3))           # drop JPEG noise / anti-alias specks
    labels, pal = _palette(rgb, colors)
    if alpha is not None:
        labels = np.where(alpha > 127, labels, -1)
    h, w = labels.shape
    turd = max(2, int(w * h / 150000))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{W}" height="{H}">']
    for c, rgbc in enumerate(pal):
        mask = labels >= c                                     # this colour + every layer stacked above it
        if not (labels == c).any():
            continue
        ys, xs = np.nonzero(mask.any(1))[0], np.nonzero(mask.any(0))[0]
        y0, x0 = max(0, ys[0] - 1), max(0, xs[0] - 1)       # trace only the bounding box (much faster)
        sub = mask[y0:ys[-1] + 2, x0:xs[-1] + 2]
        d = []
        for curve in potrace.Bitmap(~sub).trace(turdsize=turd, alphamax=1.0, opticurve=True, opttolerance=0.2):
            s = curve.start_point
            d.append(f"M{s.x + x0:.1f},{s.y + y0:.1f}")
            for seg in curve.segments:
                e = seg.end_point
                if seg.is_corner:
                    d.append(f"L{seg.c.x + x0:.1f},{seg.c.y + y0:.1f}L{e.x + x0:.1f},{e.y + y0:.1f}")
                else:
                    d.append(f"C{seg.c1.x + x0:.1f},{seg.c1.y + y0:.1f} {seg.c2.x + x0:.1f},{seg.c2.y + y0:.1f} "
                             f"{e.x + x0:.1f},{e.y + y0:.1f}")
            d.append("Z")
        if d:
            parts.append(f'<path fill="#%02x%02x%02x" fill-rule="evenodd" d="{"".join(d)}"/>' % rgbc)
    parts.append("</svg>")
    return "".join(parts)


def _to_svg(p: Path, opts, ctx):
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    dst = out_path(p, "svg")
    dst.write_text(trace_svg(im), encoding="utf-8")
    return dst


register("img.to.svg", "to SVG (vector)", "convert", IMG_IN - {"svg"}, _to_svg, badge="SVG")
