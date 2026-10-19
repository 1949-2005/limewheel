"""PDF, document, spreadsheet, slide and subtitle conversions."""
from __future__ import annotations

import base64
import csv
import io
import html as htmlmod
import json
import re
import shutil
import tempfile
from functools import cache
from html.parser import HTMLParser
from pathlib import Path

from .base import OpError, ext_of, out_dir, out_path, register, run_proc

# ------------------------------------------------------------ office backends

WORD_IN = frozenset("doc docx odt rtf".split())
SHEET_OFFICE_IN = frozenset("xls xlsx ods".split())
SLIDE_IN = frozenset("ppt pptx odp".split())


def _has_progid(progid: str) -> bool:
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid))
        return True
    except OSError:
        return False


@cache
def soffice() -> str | None:
    for c in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if Path(c).exists():
            return c
    return shutil.which("soffice")


@cache
def ms_office(app: str) -> bool:
    return _has_progid(f"{app}.Application")


def can_office(kind: str) -> bool:
    app = {"word": "Word", "sheet": "Excel", "slide": "PowerPoint"}[kind]
    return bool(soffice()) or ms_office(app)


def office_convert(src: Path, ext: str, kind: str) -> Path:
    dst = out_path(src, ext)
    app = {"word": "Word", "sheet": "Excel", "slide": "PowerPoint"}[kind]
    if ms_office(app):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            o = win32com.client.DispatchEx(f"{app}.Application")
            try:
                if app == "Word":
                    o.Visible = False
                    d = o.Documents.Open(str(src), ReadOnly=True)
                    d.SaveAs2(str(dst), FileFormat={"pdf": 17, "docx": 16}[ext])
                    d.Close(False)
                elif app == "Excel":
                    o.DisplayAlerts = False
                    wb = o.Workbooks.Open(str(src), ReadOnly=True)
                    wb.ExportAsFixedFormat(0, str(dst))
                    wb.Close(False)
                else:
                    pr = o.Presentations.Open(str(src), ReadOnly=True, WithWindow=False)
                    pr.SaveAs(str(dst), 32)
                    pr.Close()
            finally:
                o.Quit()
        finally:
            pythoncom.CoUninitialize()
        return dst
    with tempfile.TemporaryDirectory() as tmp:
        run_proc([soffice(), "--headless", "--norestore", "--convert-to", ext, "--outdir", tmp, src], timeout=300)
        produced = next(Path(tmp).glob(f"*.{ext}"), None)
        if not produced:
            raise OpError("LibreOffice produced no output")
        shutil.move(produced, dst)
    return dst


# ---------------------------------------------------------------- html utils

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip() + "\n"


CSS = ("body{font-family:'Segoe UI',Arial;font-size:11pt;line-height:1.4}"
       "table{border-collapse:collapse}td,th{border:1px solid #999;padding:3px 6px}"
       "pre,code{font-family:Consolas,monospace;background:#f4f4f4}h1,h2,h3{color:#222}")


QT_CSS = ("body{font-size:11pt}"
       "p,li{line-height:135%} h1{font-size:20pt} h2{font-size:15pt} h3{font-size:12.5pt}"
       "table{border-collapse:collapse;border-color:#999;margin:4px 0}"
       "th{background:#f0f0f0;font-weight:bold}"
       "b,strong,h1,h2,h3,h4{font-weight:bold}"
       "pre{font-family:Consolas;font-size:9.5pt;background:#f4f4f4;padding:6px}"
       "code{font-family:Consolas;background:#f4f4f4}"
       "blockquote{margin-left:12px;color:#555}"
       "a{color:#1a56c4}")


def _img_px(src: str, base: Path | None):
    from PIL import Image
    try:
        if src.startswith("data:"):
            data = base64.b64decode(src.split(",", 1)[1])
        else:
            f = Path(src) if Path(src).is_absolute() or not base else base / src
            data = f.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def fit_images(html: str, base: Path | None, max_px: float, sizes: list | None = None) -> str:
    """Give every <img> an explicit width that fits the text column (Qt lays images out at
    96 px/inch, so a 2400-px photo would be 63 cm wide and get cut off). The bitmap itself
    is embedded at full resolution, so it stays sharp when printed or zoomed."""
    it = iter(sizes or [])

    def repl(m):
        tag = m.group(0)
        src = re.search(r'src\s*=\s*["\']([^"\']+)', tag)
        want = next(it, None)                          # DOCX: the size Word displays it at (px)
        if re.search(r"\swidth\s*=", tag) and not want:
            return tag
        nat = _img_px(htmlmod.unescape(src.group(1)), base) if src else None
        if not nat:
            return tag
        w, h = want or nat
        k = min(1.0, max_px / w)
        tag = re.sub(r'\s(width|height)\s*=\s*["\']?[\d.]+%?["\']?', "", tag)
        return tag[:4] + f' width="{w * k:.0f}" height="{h * k:.0f}"' + tag[4:]
    return re.sub(r"<img\b[^>]*>", repl, html, flags=re.I)


def html_to_pdf(html: str, dst: Path, base: Path | None = None, landscape=False, img_sizes=None) -> Path:
    from PySide6.QtCore import QMarginsF, QUrl
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    w = QPdfWriter(str(dst))
    w.setPageSize(QPageSize(QPageSize.A4))
    w.setPageOrientation(QPageLayout.Landscape if landscape else QPageLayout.Portrait)
    w.setPageMargins(QMarginsF(18, 16, 18, 16), QPageLayout.Millimeter)
    w.setTitle(dst.stem)
    text_mm = w.pageLayout().paintRect(QPageLayout.Millimeter).width()
    html = fit_images(html, base, text_mm / 25.4 * 96, img_sizes)
    html = re.sub(r"<table(?![^>]*\bcellspacing)", '<table border="1" cellspacing="0" cellpadding="4"', html, flags=re.I)
    doc = QTextDocument()
    font = QFont("Calibri", 11)        # Qt ignores body{font-family} in the default stylesheet;
    for tag in ("liga", "clig", "dlig"):  # Segoe UI bold renders as Black in Qt PDFs, so use Calibri
        font.setFeature(QFont.Tag(tag), 0)   # ligatures off, else "ti"/"fi" become unsearchable glyphs
    doc.setDefaultFont(font)
    if base:
        doc.setBaseUrl(QUrl.fromLocalFile(str(base) + "/"))
    doc.setDefaultStyleSheet(QT_CSS)
    doc.setHtml(html)
    doc.print_(w)
    return dst


def docx_html_with_sizes(p: Path):
    """mammoth HTML + the on-page size of every picture (mammoth drops it)."""
    import mammoth
    from docx import Document
    sizes = []
    try:
        for s in Document(p).inline_shapes:
            sizes.append((s.width / 914400 * 96, s.height / 914400 * 96))
    except Exception:
        sizes = []
    style_map = "p[style-name='Title'] => h1.title:fresh\np[style-name='Subtitle'] => h2:fresh"
    with open(p, "rb") as f:
        html = mammoth.convert_to_html(f, style_map=style_map).value
    return html, sizes


def wrap_html(body: str, title: str = "") -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{htmlmod.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def md_to_html(text: str) -> str:
    import markdown
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])


def text_to_html(text: str) -> str:
    return "".join(f"<p>{htmlmod.escape(par).replace(chr(10), '<br>')}</p>"
                   for par in re.split(r"\n\s*\n", text) if par.strip())


def read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


class _DocxBuilder(HTMLParser):
    """Walk markdown-generated (or simple) HTML and emit real Word structure:
    headings, nested bullet/number lists, tables, code blocks, quotes, links, images."""
    ENDS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "div", "td", "th"}

    def __init__(self, doc, base, html):
        super().__init__(convert_charrefs=True)
        self.doc, self.base = doc, base
        s = doc.sections[0]
        self.usable = s.page_width - s.left_margin - s.right_margin
        # table shapes, found up front so each table is created at its full size
        self.shapes = []
        for t in re.findall(r"(?is)<table\b.*?</table>", html):
            rows = re.findall(r"(?is)<tr\b.*?</tr>", t)
            self.shapes.append((len(rows), max((len(re.findall(r"(?i)<t[dh]\b", r)) for r in rows), default=1)))
        self.par = None; self.fmt = []; self.lists = []; self.href = None
        self.table = None; self.r = -1; self.c = -1; self.pre = False; self.quote = 0

    def _new_par(self, style=None):
        from docx.shared import Pt
        if self.table is not None and self.r >= 0 and self.c >= 0:
            cell = self.table.cell(self.r, self.c)
            self.par = cell.paragraphs[0] if not cell.paragraphs[0].text and len(cell.paragraphs) == 1 else cell.add_paragraph()
        else:
            self.par = self.doc.add_paragraph(style=style) if style else self.doc.add_paragraph()
        if self.quote:
            self.par.paragraph_format.left_indent = Pt(18 * self.quote)
        return self.par

    def handle_starttag(self, tag, attrs):
        from docx.enum.text import WD_ALIGN_PARAGRAPH as AL
        a = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.par = self.doc.add_heading(level=int(tag[1]))
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
        elif tag == "li":
            kind = "List Number" if self.lists and self.lists[-1] == "ol" else "List Bullet"
            depth = min(len(self.lists), 3)
            self._new_par(kind if depth == 1 else f"{kind} {depth}")
        elif tag == "p":
            if not (self.lists and self.par is not None and not self.par.text):   # <li><p>…
                self._new_par()
        elif tag == "pre":
            self.pre = True
            self._new_par()
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "table":
            rows, cols = self.shapes.pop(0) if self.shapes else (1, 1)
            self.table = self.doc.add_table(rows=max(rows, 1), cols=max(cols, 1))
            self.table.style = "Table Grid"
            self.r = -1
        elif tag == "tr" and self.table is not None:
            self.r += 1; self.c = -1
        elif tag in ("td", "th") and self.table is not None:
            self.c += 1
            self._new_par()
            if tag == "th":
                self.fmt.append("b")
            al = (a.get("style") or "") + (a.get("align") or "")
            self.par.alignment = AL.RIGHT if "right" in al else AL.CENTER if "center" in al else None
        elif tag in ("b", "strong"): self.fmt.append("b")
        elif tag in ("i", "em"): self.fmt.append("i")
        elif tag == "code": self.fmt.append("code")
        elif tag in ("s", "del", "strike"): self.fmt.append("s")
        elif tag == "a": self.href = a.get("href")
        elif tag == "br" and self.par is not None: self.par.add_run().add_break()
        elif tag == "img": self._image(a.get("src", ""))

    def handle_endtag(self, tag):
        alias = {"strong": "b", "em": "i", "th": "b", "del": "s", "strike": "s"}
        if tag in ("ul", "ol") and self.lists: self.lists.pop()
        elif tag == "pre": self.pre = False
        elif tag == "blockquote": self.quote = max(0, self.quote - 1)
        elif tag == "table":
            self.table, self.r, self.c = None, -1, -1
            self.doc.add_paragraph()
        elif tag == "a": self.href = None
        f = alias.get(tag, tag)
        if f in ("b", "i", "code", "s") and f in self.fmt:
            del self.fmt[len(self.fmt) - 1 - self.fmt[::-1].index(f)]
        if tag in self.ENDS:
            self.par = None

    def handle_data(self, data):
        from docx.shared import Pt
        if self.pre:
            for i, line in enumerate(data.rstrip("\n").split("\n")):
                if i:
                    self.par.add_run().add_break()
                r = self.par.add_run(line)
                r.font.name, r.font.size = "Consolas", Pt(9.5)
            return
        data = re.sub(r"\s+", " ", data)
        if self.par is None:
            if not data.strip():
                return
            self._new_par()
        if not self.par.text:
            data = data.lstrip()
        if not data:
            return
        run = self._link_run(data) if self.href else self.par.add_run(data)
        run.bold = "b" in self.fmt or None
        run.italic = "i" in self.fmt or None
        run.font.strike = "s" in self.fmt or None
        if "code" in self.fmt:
            run.font.name = "Consolas"

    def _link_run(self, text):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import RGBColor
        rid = self.par.part.relate_to(
            self.href, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), rid)
        self.par._p.append(link)
        run = self.par.add_run(text)
        link.append(run._r)                       # move the run inside <w:hyperlink>
        run.font.underline = True
        run.font.color.rgb = RGBColor(0x1A, 0x56, 0xC4)
        return run

    def _image(self, src):
        from docx.shared import Emu
        from PIL import Image
        try:
            if src.startswith("data:"):
                data = base64.b64decode(src.split(",", 1)[1])
            else:
                f = Path(src)
                data = (f if f.is_absolute() or not self.base else self.base / f).read_bytes()
            with Image.open(io.BytesIO(data)) as im:
                w = Emu(int(im.width / 96 * 914400))
            (self.par or self._new_par()).add_run().add_picture(io.BytesIO(data), width=min(w, self.usable))
        except Exception:
            pass


def html_to_docx(html: str, dst: Path, base: Path | None = None) -> Path:
    from docx import Document
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", html)
    doc = Document()
    _DocxBuilder(doc, base, html).feed(html)
    doc.save(dst)
    return dst


def md_to_docx(text: str, dst: Path, base: Path | None = None) -> Path:
    import markdown
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return html_to_docx(html, dst, base)


# ------------------------------------------------------------------------ PDF

def _fitz():
    import pymupdf as fitz
    return fitz


def _pdf_images(ext):
    def run(p: Path, opts, ctx):
        doc = _fitz().open(p)
        try:
            if doc.page_count == 1:
                dst = out_path(p, ext)
                doc[0].get_pixmap(dpi=300).save(dst)
                return dst
            folder = out_dir(p, "-pages")
            for i, page in enumerate(doc, 1):
                page.get_pixmap(dpi=300).save(folder / f"{p.stem}-p{i:03d}.{ext}")
            return folder
        finally:
            doc.close()
    return run


def _pdf_text(p: Path) -> list[str]:
    doc = _fitz().open(p)
    try:
        return [page.get_text() for page in doc]
    finally:
        doc.close()


def _pdf_to_txt(p, opts, ctx):
    dst = out_path(p, "txt")
    dst.write_text("\n\f\n".join(_pdf_text(p)), encoding="utf-8")
    return dst


def _pdf_to_md(p, opts, ctx):
    try:
        import pymupdf4llm
        md = pymupdf4llm.to_markdown(str(p))
    except ImportError:
        pages = _pdf_text(p)
        md = "\n\n---\n\n".join(re.sub(r"[ \t]+\n", "\n", t).strip() for t in pages)
    dst = out_path(p, "md")
    dst.write_text(md, encoding="utf-8")
    return dst


def _pdf_to_html(p, opts, ctx):
    doc = _fitz().open(p)
    try:
        body = "".join(f"<section>{page.get_text('xhtml')}</section><hr>" for page in doc)
    finally:
        doc.close()
    dst = out_path(p, "html")
    dst.write_text(wrap_html(body, p.stem), encoding="utf-8")
    return dst


_CTRL = re.compile("[%s]" % "".join(map(chr, [*range(9), 11, 12, *range(14, 32)])))  # XML-illegal control chars

_PDF_FONTS = {"helvetica": "Arial", "arial": "Arial", "times": "Times New Roman", "courier": "Courier New",
              "symbol": "Symbol", "zapfdingbats": "Wingdings"}


def _font_family(name: str) -> str:
    base = re.sub(r"^[A-Z]{6}\+", "", name)                    # drop subset tag ABCDEF+
    base = re.split(r"[-,]", base)[0]
    base = re.sub(r"(MT|PS|Std|Pro)$", "", base)
    fam = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base).strip()     # TimesNewRoman -> Times New Roman
    return _PDF_FONTS.get(base.lower().replace(" ", ""), fam) or "Calibri"


def _merge_paragraphs(blocks, page_rect):
    fitz = _fitz()
    """Many PDF producers write one block per visual line. Join consecutive text blocks into one
    paragraph when they share the left edge and font size, sit one line apart, and the previous
    line ran (nearly) to the right edge. Also drops bare page numbers in the header/footer band."""
    def first_size(b):
        return max(s["size"] for l in b["lines"] for s in l["spans"])
    text = [b for b in blocks if b["type"] == 0 and any(s["text"].strip() for l in b["lines"] for s in l["spans"])]
    text = [b for b in text if not (re.fullmatch(r"\s*(page\s*)?\d+(\s*(/|of)\s*\d+)?\s*", " ".join(
        s["text"] for l in b["lines"] for s in l["spans"]), re.I) and (b["bbox"][1] > page_rect.height * 0.86 or b["bbox"][3] < page_rect.height * 0.08))]
    right = {}
    for b in text:
        for l in b["lines"]:
            k = round(b["bbox"][0] / 4)
            right[k] = max(right.get(k, 0), l["bbox"][2])
    out = []
    for b in sorted(text, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        prev = out[-1] if out else None
        if prev is not None:
            L1, L2 = prev["lines"][-1], b["lines"][0]
            h = L1["bbox"][3] - L1["bbox"][1]
            x0 = prev["bbox"][0]
            edge = right.get(round(x0 / 4), L1["bbox"][2])
            starts_item = re.match(r"\s*([\u2022\u25cf\u25aa\u2013\-*]|\d+[.)])\s", "".join(s["text"] for s in L2["spans"]))
            if (abs(b["bbox"][0] - x0) < 3 and abs(first_size(b) - first_size(prev)) < 0.6
                    and -2 < L2["bbox"][1] - L1["bbox"][3] < 0.8 * h
                    and L1["bbox"][2] >= x0 + 0.85 * (edge - x0) and not starts_item):
                prev["lines"].extend(b["lines"])
                prev["bbox"] = tuple(fitz.Rect(prev["bbox"]) | fitz.Rect(b["bbox"]))
                continue
        out.append({**b, "lines": list(b["lines"])})
    return out + [b for b in blocks if b["type"] == 1]


def _pdf_to_docx(p, opts, ctx):
    """Rebuild a PDF as an editable Word file: headings, fonts, bold/italic/colour runs,
    tables, images and vector graphics (charts/diagrams rendered as 300-dpi pictures)."""
    from docx import Document
    from docx.enum.text import WD_BREAK, WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Emu

    fitz = _fitz()
    pdf = fitz.open(p)
    doc = Document()
    sec = doc.sections[0]
    r0 = pdf[0].rect
    sec.page_width, sec.page_height = Emu(int(r0.width * 12700)), Emu(int(r0.height * 12700))
    # margins = where the content actually starts/ends (clamped 0.4"-1.2")
    boxes = [fitz.Rect(b["bbox"]) for pg in pdf for b in pg.get_text("dict")["blocks"]]
    if boxes:
        clamp = lambda v: Emu(int(min(max(v, 29), 86) * 12700))
        sec.left_margin = clamp(min(b.x0 for b in boxes))
        sec.right_margin = clamp(r0.width - max(b.x1 for b in boxes))
        sec.top_margin = clamp(min(b.y0 for b in boxes) - 6)
        sec.bottom_margin = clamp(r0.height - max(b.y1 for b in boxes) - 6)
    usable = sec.page_width - sec.left_margin - sec.right_margin
    normal = doc.styles["Normal"]
    normal.paragraph_format.space_after = Pt(4)

    sizes: dict[float, int] = {}
    for page in pdf:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(s["text"])
    body = max(sizes, key=sizes.get) if sizes else 11.0

    def add_picture(data, width_pt):
        try:
            doc.add_picture(io.BytesIO(data), width=min(usable, Emu(int(width_pt * 12700))))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        except Exception:
            pass

    for pi, page in enumerate(pdf):
        if pi:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        try:
            tables = page.find_tables().tables
        except Exception:
            tables = []
        tboxes = [fitz.Rect(t.bbox) for t in tables]
        # vector graphics (charts, diagrams, logos) that aren't table grid lines
        vboxes = []
        try:
            for r in page.cluster_drawings():
                r = fitz.Rect(r)
                if r.width < 24 or r.height < 24 or any((r & tb).get_area() > 0.5 * r.get_area() for tb in tboxes):
                    continue
                vboxes.append(r)
        except Exception:
            pass
        mostly_in = lambda rect, boxes: any((rect & bx).get_area() > 0.6 * max(rect.get_area(), 1) for bx in boxes)
        blocks = _merge_paragraphs(page.get_text("dict")["blocks"], page.rect)
        # grow each graphic to include the labels that sit on/around it, then render it
        for i, r in enumerate(vboxes):
            for b in blocks:
                br = fitz.Rect(b["bbox"])
                if b["type"] == 0 and br.intersects(r + (-4, -4, 4, 12)) and br.get_area() < r.get_area() / 3:
                    r |= br
            vboxes[i] = r
        items = [(box.y0, "table", t) for t, box in zip(tables, tboxes)]
        items += [(r.y0, "vector", r) for r in vboxes]
        for b in blocks:
            rect = fitz.Rect(b["bbox"])
            if mostly_in(rect, tboxes) or mostly_in(rect, vboxes):
                continue
            items.append((rect.y0, "image" if b["type"] == 1 else "text", b))
        for _, kind, b in sorted(items, key=lambda x: x[0]):
            if kind == "table":
                rows = [[("" if c is None else str(c)) for c in row] for row in b.extract()]
                if not rows:
                    continue
                tbl = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                tbl.style = "Table Grid"
                for i, row in enumerate(rows):
                    for j, c in enumerate(row):
                        tbl.cell(i, j).text = _CTRL.sub("", c)
                        if i == 0 and b.header and not b.header.external:
                            for run in tbl.cell(i, j).paragraphs[0].runs:
                                run.bold = True
                doc.add_paragraph()
            elif kind == "vector":
                pix = page.get_pixmap(dpi=300, clip=b)
                add_picture(pix.tobytes("png"), b.width)
            elif kind == "image":
                data = b["image"]
                if b.get("ext") not in ("png", "jpeg", "jpg", "gif", "bmp", "tiff"):
                    data = page.get_pixmap(dpi=300, clip=b["bbox"]).tobytes("png")
                add_picture(data, b["bbox"][2] - b["bbox"][0])
            else:
                lines = [l for l in b["lines"] if "".join(s["text"] for s in l["spans"]).strip()]
                if not lines:
                    continue
                size = max(s["size"] for l in lines for s in l["spans"])
                level = 1 if size >= body * 1.6 else 2 if size >= body * 1.3 else 3 if size >= body * 1.12 else 0
                par = doc.add_heading(level=level) if level else doc.add_paragraph()
                x0s = [l["bbox"][0] for l in lines]; x1s = [l["bbox"][2] for l in lines]
                bx = fitz.Rect(b["bbox"])
                if len(lines) == 1 and abs((bx.x0 - 0) - (r0.width - bx.x1)) < 12 and bx.width < r0.width * 0.7:
                    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                elif len(lines) > 2 and max(x1s[:-1]) - min(x1s[:-1]) < 2:
                    par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                for li, l in enumerate(lines):
                    spans = l["spans"]
                    for si, s in enumerate(spans):
                        text = _CTRL.sub("", s["text"])
                        last = li < len(lines) - 1 and si == len(spans) - 1
                        if last and text.endswith("-") and len(text) > 1 and text[-2].isalpha():
                            text = text[:-1]                       # re-join hyphenated word
                        elif last and not text.endswith(" "):
                            text += " "
                        if not text:
                            continue
                        run = par.add_run(text)
                        run.bold = bool(s["flags"] & 16) or None
                        run.italic = bool(s["flags"] & 2) or None
                        run.font.size = Pt(round(s["size"] * 2) / 2)
                        run.font.name = _font_family(s["font"])
                        col = s.get("color", 0)
                        run.font.color.rgb = RGBColor((col >> 16) & 255, (col >> 8) & 255, col & 255)
    pdf.close()
    dst = out_path(p, "docx")
    doc.save(dst)
    return dst


def _pdf_to_pptx(p, opts, ctx):
    import io
    from pptx import Presentation
    from pptx.util import Emu

    doc = _fitz().open(p)
    try:
        prs = Presentation()
        r = doc[0].rect
        prs.slide_width = Emu(int(r.width * 12700))
        prs.slide_height = Emu(int(r.height * 12700))
        blank = prs.slide_layouts[6]
        for page in doc:
            slide = prs.slides.add_slide(blank)
            img = io.BytesIO(page.get_pixmap(dpi=200).tobytes("png"))
            slide.shapes.add_picture(img, 0, 0, prs.slide_width, prs.slide_height)
    finally:
        doc.close()
    dst = out_path(p, "pptx")
    prs.save(dst)
    return dst


def _pdf_to_xlsx(p, opts, ctx):
    from openpyxl import Workbook

    doc = _fitz().open(p)
    wb = Workbook()
    wb.remove(wb.active)
    try:
        for i, page in enumerate(doc, 1):
            for j, table in enumerate(page.find_tables().tables, 1):
                ws = wb.create_sheet(f"p{i}-t{j}"[:31])
                for row in table.extract():
                    ws.append([("" if c is None else c) for c in row])
    finally:
        doc.close()
    if not wb.worksheets:
        raise OpError("No tables were found in this PDF")
    dst = out_path(p, "xlsx")
    wb.save(dst)
    return dst


PDF = frozenset({"pdf"})
register("pdf.docx", "to DOCX", "convert", PDF, _pdf_to_docx, badge="DOCX")
register("pdf.jpg", "to JPG", "convert", PDF, _pdf_images("jpg"), badge="JPG")
register("pdf.png", "to PNG", "convert", PDF, _pdf_images("png"), badge="PNG")
register("pdf.txt", "to TXT", "convert", PDF, _pdf_to_txt, badge="TXT")
register("pdf.md", "to Markdown", "convert", PDF, _pdf_to_md, badge="MD")
register("pdf.html", "to HTML", "convert", PDF, _pdf_to_html, badge="HTML")
register("pdf.pptx", "to PPTX", "convert", PDF, _pdf_to_pptx, badge="PPTX")
register("pdf.xlsx", "Tables → XLSX", "convert", PDF, _pdf_to_xlsx, badge="XLSX")


def _pdf_merge(paths, opts, ctx):
    fitz = _fitz()
    out = fitz.open()
    for p in paths:
        with fitz.open(p) as d:
            out.insert_pdf(d)
    dst = out_path(paths[0], "pdf", "-merged")
    out.save(dst, garbage=3, deflate=True)
    out.close()
    return dst


def _pdf_split(p, opts, ctx):
    fitz = _fitz()
    folder = out_dir(p, "-split")
    with fitz.open(p) as d:
        for i in range(d.page_count):
            one = fitz.open()
            one.insert_pdf(d, from_page=i, to_page=i)
            one.save(folder / f"{p.stem}-p{i + 1:03d}.pdf")
            one.close()
    return folder


def _pdf_compress(p, opts, ctx):
    fitz = _fitz()
    dst = out_path(p, "pdf", "-compressed")
    with fitz.open(p) as d:
        if hasattr(d, "rewrite_images"):
            try:
                d.rewrite_images(dpi_threshold=225, dpi_target=150, quality=80, lossy=True,
                                 lossless=False, bitonal=False)
            except Exception:
                pass
        try:
            d.subset_fonts()
        except Exception:
            pass
        d.save(dst, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)
    return dst


def _pdf_rotate(p, opts, ctx):
    fitz = _fitz()
    dst = out_path(p, "pdf", "-rotated")
    with fitz.open(p) as d:
        for page in d:
            page.set_rotation((page.rotation + 90) % 360)
        d.save(dst, garbage=3, deflate=True)
    return dst


def _pdf_extract_images(p, opts, ctx):
    fitz = _fitz()
    folder = out_dir(p, "-images")
    n = 0
    with fitz.open(p) as d:
        seen = set()
        for page in d:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                info = d.extract_image(xref)
                n += 1
                (folder / f"{p.stem}-img{n:03d}.{info['ext']}").write_bytes(info["image"])
    if not n:
        folder.rmdir()
        raise OpError("This PDF has no embedded images")
    return folder


register("pdf.merge", "Merge", "tools", PDF, _pdf_merge, batch=True, min_files=2)
register("pdf.split", "Split pages", "tools", PDF, _pdf_split)
register("pdf.compress", "Compress", "tools", PDF, _pdf_compress)
register("pdf.rotate", "Rotate 90°", "tools", PDF, _pdf_rotate)
register("pdf.images", "Extract images", "tools", PDF, _pdf_extract_images)


# ------------------------------------------------------------- word documents

def _docx_html(p: Path) -> str:
    import mammoth
    with open(p, "rb") as f:
        return mammoth.convert_to_html(f).value


def _docx_to(kind):
    def run(p: Path, opts, ctx):
        import mammoth
        dst = out_path(p, kind)
        if kind == "html":
            dst.write_text(wrap_html(_docx_html(p), p.stem), encoding="utf-8")
        elif kind == "md":
            with open(p, "rb") as f:
                dst.write_text(mammoth.convert_to_markdown(f).value, encoding="utf-8")
        else:
            with open(p, "rb") as f:
                dst.write_text(mammoth.extract_raw_text(f).value, encoding="utf-8")
        return dst
    return run


DOCX = frozenset({"docx"})
register("docx.html", "to HTML", "convert", DOCX, _docx_to("html"), badge="HTML")
register("docx.md", "to Markdown", "convert", DOCX, _docx_to("md"), badge="MD")
register("docx.txt", "to TXT", "convert", DOCX, _docx_to("txt"), badge="TXT")
register("docx.pdf.basic", "to PDF", "convert", DOCX,
         lambda p, o, c: html_to_pdf(*docx_html_with_sizes(p)[:1], out_path(p, "pdf"), p.parent,
                                     img_sizes=docx_html_with_sizes(p)[1]),
         badge="PDF", main_thread=True, available=lambda: not can_office("word"))
register("office.word.pdf", "to PDF", "convert", WORD_IN,
         lambda p, o, c: office_convert(p, "pdf", "word"), badge="PDF", available=lambda: can_office("word"))
register("office.word.docx", "to DOCX", "convert", WORD_IN - {"docx"},
         lambda p, o, c: office_convert(p, "docx", "word"), badge="DOCX", available=lambda: can_office("word"))
register("office.sheet.pdf", "to PDF", "convert", SHEET_OFFICE_IN,
         lambda p, o, c: office_convert(p, "pdf", "sheet"), badge="PDF", available=lambda: can_office("sheet"))
register("office.slide.pdf", "to PDF", "convert", SLIDE_IN,
         lambda p, o, c: office_convert(p, "pdf", "slide"), badge="PDF", available=lambda: can_office("slide"))


# ------------------------------------------------------- markdown / txt / html

MD, TXT, HTML = frozenset({"md"}), frozenset({"txt"}), frozenset({"html"})


def _write(p, ext, content):
    dst = out_path(p, ext)
    dst.write_text(content, encoding="utf-8")
    return dst


register("md.html", "to HTML", "convert", MD,
         lambda p, o, c: _write(p, "html", wrap_html(md_to_html(read_text(p)), p.stem)), badge="HTML")
register("md.pdf", "to PDF", "convert", MD,
         lambda p, o, c: html_to_pdf(md_to_html(read_text(p)), out_path(p, "pdf"), p.parent),
         badge="PDF", main_thread=True)
register("md.docx", "to DOCX", "convert", MD,
         lambda p, o, c: md_to_docx(read_text(p), out_path(p, "docx")), badge="DOCX")
register("md.txt", "to TXT", "convert", MD,
         lambda p, o, c: _write(p, "txt", html_to_text(md_to_html(read_text(p)))), badge="TXT")

register("txt.pdf", "to PDF", "convert", TXT,
         lambda p, o, c: html_to_pdf(text_to_html(read_text(p)), out_path(p, "pdf")),
         badge="PDF", main_thread=True)
register("txt.docx", "to DOCX", "convert", TXT,
         lambda p, o, c: md_to_docx(read_text(p), out_path(p, "docx")), badge="DOCX")
register("txt.md", "to Markdown", "convert", TXT,
         lambda p, o, c: _write(p, "md", read_text(p)), badge="MD")
register("txt.html", "to HTML", "convert", TXT,
         lambda p, o, c: _write(p, "html", wrap_html(text_to_html(read_text(p)), p.stem)), badge="HTML")

register("html.pdf", "to PDF", "convert", HTML,
         lambda p, o, c: html_to_pdf(read_text(p), out_path(p, "pdf"), p.parent), badge="PDF", main_thread=True)
register("html.txt", "to TXT", "convert", HTML,
         lambda p, o, c: _write(p, "txt", html_to_text(read_text(p))), badge="TXT")
register("html.docx", "to DOCX", "convert", HTML,
         lambda p, o, c: html_to_docx(read_text(p), out_path(p, "docx"), p.parent), badge="DOCX")


# --------------------------------------------------------------- spreadsheets

SHEETS = frozenset("xlsx xls csv tsv ods".split())


def _read_ods(p: Path) -> list[tuple[str, list[list]]]:
    from odf import teletype
    from odf.opendocument import load
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    out = []
    for t in load(str(p)).spreadsheet.getElementsByType(Table):
        rows = []
        for tr in t.getElementsByType(TableRow):
            row = []
            for tc in tr.getElementsByType(TableCell):
                rep = min(int(tc.getAttribute("numbercolumnsrepeated") or 1), 512)
                vtype, val = tc.getAttribute("valuetype"), tc.getAttribute("value")
                text = "\n".join(teletype.extractText(x) for x in tc.getElementsByType(P))
                v = (float(val) if vtype in ("float", "percentage", "currency") and val else text)
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                row += [v] * rep
            while row and row[-1] in ("", None):
                row.pop()
            rows += [row] * min(int(tr.getAttribute("numberrowsrepeated") or 1), 512)
        while rows and not rows[-1]:
            rows.pop()
        out.append((t.getAttribute("name") or "Sheet", rows))
    return out


def _write_ods(sheets, dst: Path) -> Path:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    doc = OpenDocumentSpreadsheet()
    for name, rows in sheets:
        t = Table(name=str(name)[:31] or "Sheet")
        for row in rows:
            tr = TableRow()
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tc = TableCell(valuetype="float", value=v)
                else:
                    tc = TableCell(valuetype="string")
                tc.addElement(P(text="" if v is None else str(v)))
                tr.addElement(tc)
            t.addElement(tr)
        doc.spreadsheet.addElement(t)
    doc.save(str(dst))
    return dst


def read_sheets(p: Path) -> list[tuple[str, list[list]]]:
    e = ext_of(p)
    if e == "ods":
        return _read_ods(p)
    if e == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        try:
            return [(ws.title, [["" if v is None else v for v in row] for row in ws.iter_rows(values_only=True)])
                    for ws in wb.worksheets]
        finally:
            wb.close()
    if e == "xls":
        import xlrd
        book = xlrd.open_workbook(p)
        return [(s.name, [s.row_values(r) for r in range(s.nrows)]) for s in book.sheets()]
    text = read_text(p)
    delim = "\t" if e == "tsv" else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    return [(p.stem, list(csv.reader(text.splitlines(), delimiter=delim)))]


def _sheet_to(ext):
    def run(p: Path, opts, ctx):
        sheets = read_sheets(p)
        if ext == "ods":
            return _write_ods(sheets, out_path(p, "ods"))
        if ext == "xlsx":
            from openpyxl import Workbook
            wb = Workbook()
            wb.remove(wb.active)
            for name, rows in sheets:
                ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "_", name)[:31] or "Sheet")
                for row in rows:
                    ws.append(row)
            dst = out_path(p, "xlsx")
            wb.save(dst)
            return dst
        if ext in ("csv", "tsv"):
            outs = []
            for name, rows in sheets:
                suffix = f"-{name}" if len(sheets) > 1 else ""
                dst = out_path(p, ext, suffix)
                with open(dst, "w", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f, delimiter="\t" if ext == "tsv" else ",").writerows(rows)
                outs.append(dst)
            return outs
        if ext == "json":
            data = {}
            for name, rows in sheets:
                if rows:
                    head = [str(h) for h in rows[0]]
                    data[name] = [dict(zip(head, r)) for r in rows[1:]]
            return _write(p, "json", json.dumps(data if len(data) != 1 else next(iter(data.values())),
                                                indent=2, ensure_ascii=False, default=str))
        body = ""
        for name, rows in sheets:
            body += f"<h2>{htmlmod.escape(str(name))}</h2><table>"
            for i, row in enumerate(rows):
                tag = "th" if i == 0 else "td"
                body += "<tr>" + "".join(f"<{tag}>{htmlmod.escape(str(c))}</{tag}>" for c in row) + "</tr>"
            body += "</table>"
        if ext == "html":
            return _write(p, "html", wrap_html(body, p.stem))
        return html_to_pdf(body, out_path(p, "pdf"), landscape=True)
    return run


for _ext in ("xlsx", "ods", "csv", "tsv", "json", "html"):
    register(f"sheet.{_ext}", f"to {_ext.upper()}", "convert", SHEETS, _sheet_to(_ext),
             badge=_ext.upper(), skip_same=_ext)
register("sheet.pdf", "to PDF", "convert", SHEETS, _sheet_to("pdf"), badge="PDF", main_thread=True,
         available=lambda: not can_office("sheet"))
register("sheet.pdf.csv", "to PDF", "convert", frozenset({"csv", "tsv"}), _sheet_to("pdf"), badge="PDF",
         main_thread=True, available=lambda: can_office("sheet"))


# ------------------------------------------------------------------ subtitles

SUBS = frozenset({"srt", "vtt"})
_TS = re.compile(r"(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})")


def _cues(p: Path):
    cues = []
    for block in re.split(r"\n\s*\n", read_text(p).replace("\r\n", "\n")):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->")[:2]
                cues.append((_TS.search(a)[0], _TS.search(b)[0], "\n".join(lines[i + 1:])))
                break
    return cues


def _ts(t, sep):
    m = _TS.match(t)
    h = (m[1] or "0:")[:-1]
    return f"{int(h):02d}:{int(m[2]):02d}:{m[3]}{sep}{m[4]}"


def _sub_to(ext):
    def run(p: Path, opts, ctx):
        cues = _cues(p)
        if ext == "vtt":
            body = "WEBVTT\n\n" + "\n".join(f"{_ts(a, '.')} --> {_ts(b, '.')}\n{t}\n" for a, b, t in cues)
        elif ext == "srt":
            body = "\n".join(f"{i}\n{_ts(a, ',')} --> {_ts(b, ',')}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))
        else:
            body = "\n".join(re.sub(r"<[^>]+>", "", t) for _, _, t in cues) + "\n"
        return _write(p, ext, body)
    return run


for _ext in ("srt", "vtt", "txt"):
    register(f"sub.{_ext}", f"to {_ext.upper()}", "convert", SUBS, _sub_to(_ext), badge=_ext.upper(),
             skip_same=_ext)
