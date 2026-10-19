"""EPUB (read + write), RTF without Office, and slides -> images."""
from __future__ import annotations

import base64
import html as htmlmod
import mimetypes
import posixpath
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .base import OpError, out_dir, out_path, register
from .docs import (DOCX, HTML, MD, PDF, SLIDE_IN, TXT, _fitz, can_office, html_to_pdf, html_to_text, md_to_docx,
                   md_to_html, office_convert, read_text, text_to_html, wrap_html)

EPUB = frozenset({"epub"})
RTF = frozenset({"rtf"})

# ------------------------------------------------------------------ writer

CSS = ("body{font-family:serif;line-height:1.5;margin:0 4%}h1,h2,h3{font-family:sans-serif;line-height:1.2}"
       "img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #999;padding:2px 6px}"
       "pre{white-space:pre-wrap}")


def _xhtml(title: str, body: str) -> str:
    # EPUB needs well-formed XHTML: close void tags and drop stray control characters.
    body = re.sub(r"<(br|hr|img|meta|link|input)([^>]*?)(?<!/)>", r"<\1\2/>", body)
    body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body).replace("&nbsp;", "&#160;")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<head><meta charset="utf-8"/><title>{htmlmod.escape(title)}</title>'
            f'<link rel="stylesheet" href="style.css"/></head><body>{body}</body></html>')


def _split_chapters(body_html: str, fallback: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?=<h[12][ >])", body_html)
    chapters = []
    for part in parts:
        if not part.strip():
            continue
        m = re.match(r"<h[12][^>]*>(.*?)</h[12]>", part, re.S)
        title = re.sub(r"<[^>]+>", "", m[1]).strip() if m else ""
        chapters.append((title or (fallback if not chapters else f"Part {len(chapters) + 1}"), part))
    return chapters or [(fallback, body_html or "<p></p>")]


def write_epub(dst: Path, title: str, chapters: list[tuple[str, str]], images: dict[str, bytes] | None = None,
               author: str = "") -> Path:
    images = images or {}
    book_id = f"urn:uuid:{uuid.uuid4()}"
    with zipfile.ZipFile(dst, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf = lambda name, data: z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)  # noqa: E731
        zf("META-INF/container.xml",
           '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
           '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
           '</rootfiles></container>')
        zf("OEBPS/style.css", CSS)
        manifest, spine, nav = [], [], []
        for i, (ct, body) in enumerate(chapters, 1):
            name = f"ch{i:03d}.xhtml"
            zf(f"OEBPS/{name}", _xhtml(ct, body))
            manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            nav.append(f'<li><a href="{name}">{htmlmod.escape(ct)}</a></li>')
        for j, (name, data) in enumerate(images.items(), 1):
            zf(f"OEBPS/{name}", data)
            mt = mimetypes.guess_type(name)[0] or "image/png"
            manifest.append(f'<item id="i{j}" href="{name}" media-type="{mt}"/>')
        zf("OEBPS/nav.xhtml", _xhtml("Contents", f'<nav epub:type="toc"><h1>Contents</h1><ol>{"".join(nav)}</ol></nav>'))
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        manifest.append('<item id="css" href="style.css" media-type="text/css"/>')
        zf("OEBPS/content.opf",
           '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           'unique-identifier="bid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="bid">{book_id}</dc:identifier><dc:title>{htmlmod.escape(title)}</dc:title>'
           f'<dc:language>en</dc:language>{f"<dc:creator>{htmlmod.escape(author)}</dc:creator>" if author else ""}'
           '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
           f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>')
    return dst


def _pull_data_images(body: str) -> tuple[str, dict[str, bytes]]:
    """Move data: URI images into real files (EPUB readers handle files far better)."""
    images: dict[str, bytes] = {}

    def repl(m):
        mime, data = m[1], m[2]
        ext = (mimetypes.guess_extension(mime) or ".png").replace(".jpe", ".jpg")
        name = f"img{len(images) + 1:03d}{ext}"
        images[name] = base64.b64decode(data)
        return f'src="{name}"'

    return re.sub(r'src="data:([\w/+.-]+);base64,([^"]+)"', repl, body), images


def _to_epub(get_html):
    def run(p: Path, opts, ctx):
        body, images = _pull_data_images(get_html(p))
        return write_epub(out_path(p, "epub"), p.stem, _split_chapters(body, p.stem), images)
    return run


def _docx_html(p: Path) -> str:
    import mammoth
    with open(p, "rb") as f:
        return mammoth.convert_to_html(f).value


def _pdf_epub(p: Path, opts, ctx):
    fitz = _fitz()
    doc = fitz.open(p)
    chapters, images, buf = [], {}, []
    try:
        for i, page in enumerate(doc, 1):
            parts = []
            for b in page.get_text("dict")["blocks"]:
                if b["type"] == 1:
                    ext = b.get("ext", "png")
                    name = f"img{len(images) + 1:03d}.{ext}"
                    images[name] = b["image"]
                    parts.append(f'<p><img src="{name}" alt=""/></p>')
                    continue
                spans = [s for l in b["lines"] for s in l["spans"]]
                text = " ".join("".join(s["text"] for s in l["spans"]) for l in b["lines"]).strip()
                if not text:
                    continue
                big = max(s["size"] for s in spans) >= 15
                parts.append(f"<h2>{htmlmod.escape(text)}</h2>" if big and len(text) < 120 else
                             f"<p>{htmlmod.escape(text)}</p>")
            buf.append("".join(parts))
            if i % 10 == 0 or i == doc.page_count:
                start = i - len(buf) + 1
                chapters.append((f"Pages {start}–{i}" if len(buf) > 1 else f"Page {i}", "".join(buf) or "<p></p>"))
                buf = []
        title = (doc.metadata or {}).get("title") or p.stem
        author = (doc.metadata or {}).get("author") or ""
    finally:
        doc.close()
    return write_epub(out_path(p, "epub"), title, chapters, images, author)


register("pdf.epub", "to EPUB", "convert", PDF, _pdf_epub, badge="EPUB")
register("docx.epub", "to EPUB", "convert", DOCX, _to_epub(_docx_html), badge="EPUB")
register("md.epub", "to EPUB", "convert", MD, _to_epub(lambda p: md_to_html(read_text(p))), badge="EPUB")
register("txt.epub", "to EPUB", "convert", TXT, _to_epub(lambda p: text_to_html(read_text(p))), badge="EPUB")
register("html.epub", "to EPUB", "convert", HTML,
         _to_epub(lambda p: re.sub(r"(?is).*<body[^>]*>|</body>.*", "", read_text(p))), badge="EPUB")


# ------------------------------------------------------------------ reader

def read_epub(p: Path, inline_images: bool = True) -> tuple[str, str]:
    """(title, combined body HTML) in reading order. Images become data: URIs when inline_images."""
    with zipfile.ZipFile(p) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        items = {i.get("id"): i for i in opf.iter("{http://www.idpf.org/2007/opf}item")}
        title_el = opf.find(".//{http://purl.org/dc/elements/1.1/}title")
        title = (title_el.text or "").strip() if title_el is not None else p.stem
        bodies = []
        for ref in opf.iter("{http://www.idpf.org/2007/opf}itemref"):
            item = items.get(ref.get("idref"))
            if item is None or "html" not in (item.get("media-type") or ""):
                continue
            href = posixpath.normpath(posixpath.join(base, item.get("href")))
            raw = z.read(href).decode("utf-8", "replace")
            m = re.search(r"(?is)<body[^>]*>(.*)</body>", raw)
            body = m[1] if m else raw
            if inline_images:
                def repl(mm, hdir=posixpath.dirname(href)):
                    src = posixpath.normpath(posixpath.join(hdir, mm[2]))
                    try:
                        data = z.read(src)
                    except KeyError:
                        return mm[0]
                        # (missing image: keep the original reference)
                    mt = mimetypes.guess_type(src)[0] or "image/png"
                    return f'{mm[1]}="data:{mt};base64,{base64.b64encode(data).decode()}"'
                body = re.sub(r'(src|xlink:href)="(?!data:|https?:)([^"]+)"', repl, body)
            bodies.append(body)
    if not bodies:
        raise OpError("This EPUB has no readable chapters")
    return title or p.stem, "<hr/>".join(bodies)


def _epub_to(kind):
    def run(p: Path, opts, ctx):
        title, body = read_epub(p, inline_images=kind in ("html", "pdf"))
        if kind == "html":
            dst = out_path(p, "html")
            dst.write_text(wrap_html(body, title), encoding="utf-8")
            return dst
        if kind == "pdf":
            return html_to_pdf(f"<h1>{htmlmod.escape(title)}</h1>{body}", out_path(p, "pdf"))
        text = html_to_text(body)
        if kind == "txt":
            dst = out_path(p, "txt")
            dst.write_text(text, encoding="utf-8")
            return dst
        md = _html_to_md(body)
        if kind == "md":
            dst = out_path(p, "md")
            dst.write_text(md, encoding="utf-8")
            return dst
        return md_to_docx(md, out_path(p, "docx"))
    return run


def _html_to_md(body: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", body)
    for n in range(1, 7):
        s = re.sub(rf"(?is)<h{n}[^>]*>(.*?)</h{n}>", lambda m, n=n: "\n\n" + "#" * n + " " + m[1].strip() + "\n\n", s)
    s = re.sub(r"(?is)<(b|strong)[^>]*>(.*?)</\1>", r"**\2**", s)
    s = re.sub(r"(?is)<(i|em)[^>]*>(.*?)</\1>", r"*\2*", s)
    s = re.sub(r"(?is)<li[^>]*>(.*?)</li>", r"\n- \1", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>|</div>|<hr\s*/?>", "\n\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = htmlmod.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip() + "\n"


register("epub.pdf", "to PDF", "convert", EPUB, _epub_to("pdf"), badge="PDF", main_thread=True)
register("epub.docx", "to DOCX", "convert", EPUB, _epub_to("docx"), badge="DOCX")
register("epub.md", "to Markdown", "convert", EPUB, _epub_to("md"), badge="MD")
register("epub.txt", "to TXT", "convert", EPUB, _epub_to("txt"), badge="TXT")
register("epub.html", "to HTML", "convert", EPUB, _epub_to("html"), badge="HTML")


# ------------------------------------------------------------------ RTF without Office

def _rtf_text(p: Path) -> str:
    from striprtf.striprtf import rtf_to_text
    return rtf_to_text(p.read_text(encoding="latin-1", errors="replace"), errors="ignore")


def _rtf_to(kind):
    def run(p: Path, opts, ctx):
        text = _rtf_text(p)
        if kind == "txt":
            dst = out_path(p, "txt")
            dst.write_text(text, encoding="utf-8")
            return dst
        if kind == "md":
            dst = out_path(p, "md")
            dst.write_text(text, encoding="utf-8")
            return dst
        if kind == "html":
            dst = out_path(p, "html")
            dst.write_text(wrap_html(text_to_html(text), p.stem), encoding="utf-8")
            return dst
        if kind == "pdf":
            return html_to_pdf(text_to_html(text), out_path(p, "pdf"))
        if kind == "epub":
            return write_epub(out_path(p, "epub"), p.stem, [(p.stem, text_to_html(text))])
        return md_to_docx(text, out_path(p, "docx"))
    return run


register("rtf.txt", "to TXT", "convert", RTF, _rtf_to("txt"), badge="TXT")
register("rtf.md", "to Markdown", "convert", RTF, _rtf_to("md"), badge="MD")
register("rtf.html", "to HTML", "convert", RTF, _rtf_to("html"), badge="HTML")
register("rtf.epub", "to EPUB", "convert", RTF, _rtf_to("epub"), badge="EPUB")
register("rtf.pdf", "to PDF", "convert", RTF, _rtf_to("pdf"), badge="PDF", main_thread=True,
         available=lambda: not can_office("word"))
register("rtf.docx", "to DOCX", "convert", RTF, _rtf_to("docx"), badge="DOCX",
         available=lambda: not can_office("word"))


# ------------------------------------------------------------------ slides -> images (needs Office/LibreOffice)

def _slides_to(ext):
    def run(p: Path, opts, ctx):
        fitz = _fitz()
        pdf = office_convert(p, "pdf", "slide")
        try:
            folder = out_dir(p, "-slides")
            with fitz.open(pdf) as d:
                for i, page in enumerate(d, 1):
                    page.get_pixmap(dpi=200).save(folder / f"{p.stem}-slide{i:03d}.{ext}")
            return folder
        finally:
            pdf.unlink(missing_ok=True)
    return run


register("slide.jpg", "to JPG", "convert", SLIDE_IN, _slides_to("jpg"), badge="JPG",
         available=lambda: can_office("slide"))
register("slide.png", "to PNG", "convert", SLIDE_IN, _slides_to("png"), badge="PNG",
         available=lambda: can_office("slide"))
