"""Checks the 1.2 tools by their results, not just that they run.

    python tests/test_v12.py <samples folder>
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from limewheel.app import Ctx, execute  # noqa: E402
from limewheel.ops.base import REGISTRY  # noqa: E402
from limewheel import settings as settings_mod  # noqa: E402

SRC = Path(sys.argv[1])
WORK = Path(tempfile.mkdtemp(prefix="v12-"))
ops = {o.id: o for o in REGISTRY}
ctx = Ctx.__new__(Ctx)
ctx.app, ctx.settings = None, settings_mod.load()
results = []
counter = [0]


def run(op_id, files, opts=None):
    counter[0] += 1
    case = WORK / f"{op_id}-{counter[0]}"
    case.mkdir()
    local = []
    for f in files:
        shutil.copy2(SRC / f, case / f)
        local.append(case / f)
    return execute(ops[op_id], local, opts or {}, ctx), local


def check(name, fn):
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, f"{type(e).__name__}: {e}"
    results.append(ok)
    print(f"{'OK  ' if ok else 'FAIL'} {name:34} {info}")


def tosize(op_id, f, target):
    def t():
        out, src = run(op_id, [f], {"bytes": target})
        size = out[0].stat().st_size
        return size <= target * 1.02, f"{src[0].stat().st_size // 1024} KB -> {size // 1024} KB (target {target // 1024} KB)"
    return t


from PIL import Image as _I  # noqa: E402
_I.open(SRC / "flower.jpg").convert("RGBA").resize((900, 1200)).save(SRC / "logo_big.png")
check("compress to size: JPEG", tosize("img.tosize", "flower.jpg", 25_000))
check("compress to size: PNG", tosize("img.tosize", "logo_big.png", 15_000))
check("compress to size: video", tosize("vid.tosize", "clip.mp4", 38_000))
check("compress to size: audio", tosize("aud.tosize", "tone.wav", 20_000))


def pdf_size():
    import fitz
    doc = fitz.open()
    from PIL import Image
    Image.open(SRC / "flower.jpg").resize((1800, 2400)).save(WORK / "huge.png")
    for i in range(3):
        page = doc.new_page()
        page.insert_image(page.rect, filename=str(WORK / "huge.png"))
    big = WORK / "big.pdf"
    doc.save(big)
    shutil.copy2(big, SRC / "big.pdf")
    return tosize("pdf.tosize", "big.pdf", 400_000)()


check("compress to size: PDF", pdf_size)


def meta_jpeg():
    out, src = run("img.meta", ["photo.jpg"], {"fields": {"title": "Lime test", "author": "Lime", "copyright": "(c) me",
                                                          "date": "2026:10:19 10:00:00", "comment": "hello"},
                                               "checks": {}, "strip": False})
    from limewheel.ops.extras import _image_meta
    _, v, _ = _image_meta(out[0])
    same_pixels = open(src[0], "rb").read()[-2000:] == open(out[0], "rb").read()[-2000:]
    return (v["title"] == "Lime test" and v["author"] == "Lime" and v["comment"] == "hello" and same_pixels,
            f"read back title={v['title']!r} author={v['author']!r}; image data untouched={same_pixels}")


check("metadata: JPEG edit (lossless)", meta_jpeg)


def meta_png():
    out, _ = run("img.meta", ["sample.png"], {"fields": {"title": "PNG title", "author": "A", "copyright": "",
                                                         "date": "", "comment": ""}, "checks": {}, "strip": False})
    from limewheel.ops.extras import _image_meta
    _, v, _ = _image_meta(out[0])
    return v["title"] == "PNG title", f"title={v['title']!r}"


check("metadata: PNG edit", meta_png)


def meta_media():
    out, _ = run("aud.meta", ["tone.wav"], {"fields": {"title": "My tone", "artist": "Lime"}, "strip": False})
    from limewheel.ops.extras import _media_meta
    _, v = _media_meta(out[0])
    out2, _ = run("vid.meta", ["clip.mp4"], {"fields": {"title": "Clip!", "comment": "test"}, "strip": False})
    _, v2 = _media_meta(out2[0])
    return v["title"] == "My tone" and v2["title"] == "Clip!", f"audio title={v['title']!r}, video title={v2['title']!r}"


check("metadata: audio + video edit", meta_media)


def meta_pdf():
    import fitz
    out, _ = run("pdf.meta", ["doc.pdf"], {"fields": {"title": "Doc", "author": "Me", "subject": "S", "keywords": "k"},
                                           "strip": False})
    m = fitz.open(out[0]).metadata
    out2, _ = run("pdf.meta", ["doc.pdf"], {"strip": True})
    m2 = fitz.open(out2[0]).metadata
    return m["title"] == "Doc" and m["author"] == "Me" and not m2.get("title"), f"title={m['title']!r}; stripped ok"


check("metadata: PDF edit + strip", meta_pdf)


def organize():
    import fitz
    out, _ = run("pdf.organize", ["doc.pdf"], {"order": [[1, 90], [0, 0], [1, 0]]})
    d = fitz.open(out[0])
    texts = [p.get_text().strip()[:20] for p in d]
    return (d.page_count == 3 and "page 2" in texts[0] and d[0].rotation == 90 and "page 1" in texts[1],
            f"{d.page_count} pages, first='{texts[0]}' rotated {d[0].rotation}")


check("reorganize PDF (reorder/rotate/dup)", organize)


def collage():
    outs = []
    for layout in ("Grid", "Row", "Column"):
        out, _ = run("img.collage", ["sample.png", "photo.jpg", "flower.jpg"],
                     {"layout": layout, "gap": 16, "rounded": True, "background": "#FFFFFF", "width": 1080})
        from PIL import Image
        outs.append(f"{layout} {Image.open(out[0]).size}")
    return all("1080" in o for o in outs), ", ".join(outs)


check("collage (grid/row/column)", collage)


def qr():
    out, _ = run("img.qr", ["qr.png"])
    text = out[0].read_text(encoding="utf-8")
    return "github.com/1949-2005/limewheel" in text, text.strip()


check("QR reader", qr)


def svg():
    out, _ = run("img.to.svg", ["flower.jpg"])
    from limewheel.ops.images import open_image
    im = open_image(out[0])
    data = out[0].read_text()
    return data.count("<path") >= 4 and im.width > 100, f"{data.count('<path')} vector layers, {len(data) // 1024} KB, renders {im.size}"


check("SVG output (vector trace)", svg)

print(f"\n{sum(results)}/{len(results)} passed  (work dir {WORK})")
sys.exit(0 if all(results) else 1)
