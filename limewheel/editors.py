"""Interactive editors in the panel style: Edit Photo, Crop (image/video frame), Redact, Annotate."""
from __future__ import annotations

import math

from PIL import Image, ImageEnhance, ImageFilter
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF, QShortcut)
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QPushButton,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from . import icons
from .dialogs import Segmented, _label, _row
from .ui import ACCENT, INK, SECTION, Panel

PREVIEW_MAX = 1100


# ------------------------------------------------------------ PIL <-> Qt

def pil_to_qimage(im: Image.Image) -> QImage:
    im = im.convert("RGBA")
    return QImage(im.tobytes("raw", "RGBA"), im.width, im.height, im.width * 4, QImage.Format_RGBA8888).copy()


def qimage_to_pil(q: QImage) -> Image.Image:
    q = q.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombytes("RGBA", (q.width(), q.height()), bytes(q.constBits())[: q.width() * q.height() * 4])


def preview_copy(im: Image.Image) -> tuple[Image.Image, float]:
    k = min(1.0, PREVIEW_MAX / max(im.size))
    if k < 1:
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.Resampling.LANCZOS)
    return im, k


class ImageView(QWidget):
    """Dark rounded stage that shows an image fitted inside; maps widget <-> image coordinates."""

    def __init__(self, min_h: int = 420):
        super().__init__()
        self.setMinimumSize(560, min_h)
        self.pix: QPixmap | None = None
        self.img_w = self.img_h = 1

    def set_image(self, q: QImage, full_size: tuple[int, int] | None = None):
        self.pix = QPixmap.fromImage(q)
        self.img_w, self.img_h = full_size or (q.width(), q.height())
        self.update()

    def fit_rect(self) -> QRectF:
        r = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        if not self.pix:
            return r
        k = min(r.width() / self.img_w, r.height() / self.img_h)
        w, h = self.img_w * k, self.img_h * k
        return QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h)

    def to_img(self, p: QPointF) -> QPointF:
        f = self.fit_rect()
        return QPointF((p.x() - f.left()) / f.width() * self.img_w, (p.y() - f.top()) / f.height() * self.img_h)

    def to_view(self, p: QPointF) -> QPointF:
        f = self.fit_rect()
        return QPointF(f.left() + p.x() / self.img_w * f.width(), f.top() + p.y() / self.img_h * f.height())

    def scale(self) -> float:
        return self.fit_rect().width() / self.img_w

    def paint_base(self, p: QPainter):
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        bg = QPainterPath()
        bg.addRoundedRect(QRectF(self.rect()), 14, 14)
        p.fillPath(bg, QColor(22, 16, 10))
        if self.pix:
            p.drawPixmap(self.fit_rect(), self.pix, QRectF(self.pix.rect()))

    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_base(p)
        p.end()


# ------------------------------------------------------------ Edit Photo

LIGHT = [("Exposure", -200, 200, "ev"), ("Brightness", -100, 100, ""), ("Contrast", -100, 100, ""),
         ("Highlights", -100, 100, ""), ("Shadows", -100, 100, ""), ("Whites", -100, 100, ""),
         ("Blacks", -100, 100, ""), ("Gamma", -100, 100, "")]
COLOR = [("Temperature", -100, 100, ""), ("Tint", -100, 100, ""), ("Saturation", -100, 100, ""),
         ("Vibrance", -100, 100, ""), ("Sharpness", 0, 100, "")]


def _smooth(e0, e1, x):
    t = min(1.0, max(0.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


def adjust(im: Image.Image, v: dict) -> Image.Image:
    """Apply all slider values (ints) with per-channel lookup tables + Pillow enhancers."""
    alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") else None
    rgb = im.convert("RGB")
    ev = v.get("Exposure", 0) / 100
    k = {n: v.get(n, 0) / 100 for n, *_ in LIGHT + COLOR}
    bp = -0.15 * k["Blacks"]
    wp = 1 - 0.15 * k["Whites"]
    gamma = 2 ** (-k["Gamma"])
    tone = []
    for i in range(256):
        x = i / 255 * (2 ** ev)
        x = (x - bp) / max(1e-3, (wp - bp))
        x = x + k["Brightness"] * 0.5 * x * (1 - x) * 2 if 0 <= x <= 1 else x + k["Brightness"] * 0.1
        x = 0.5 + (x - 0.5) * (1 + k["Contrast"])
        x += k["Highlights"] * 0.25 * _smooth(0.45, 1.0, x)
        x += k["Shadows"] * 0.25 * (1 - _smooth(0.0, 0.55, x))
        x = max(0.0, x) ** gamma
        tone.append(min(1.0, max(0.0, x)))
    t, ti = k["Temperature"], k["Tint"]
    mult = (1 + 0.14 * t + 0.04 * ti, 1 - 0.10 * ti, 1 - 0.14 * t + 0.04 * ti)
    lut = []
    for m in mult:
        lut += [int(round(min(1.0, max(0.0, x * m)) * 255)) for x in tone]
    out = rgb.point(lut)
    sat = 1 + k["Saturation"] + 0.5 * k["Vibrance"]
    if abs(sat - 1) > 1e-3:
        out = ImageEnhance.Color(out).enhance(max(0.0, sat))
    if k["Sharpness"] > 0:
        out = ImageEnhance.Sharpness(out).enhance(1 + 2.5 * k["Sharpness"])
    if alpha is not None:
        out = out.convert("RGBA")
        out.putalpha(alpha)
    return out


def _fmt_val(name, v):
    return f"{v / 100:+.2f} EV" if name == "Exposure" else f"{v:+d}" if v else "+0"


def photo_editor(im: Image.Image, title="Edit Photo") -> dict | None:
    dlg = Panel(title, width=820)
    small, _ = preview_copy(im)
    view = ImageView(380)
    view.set_image(pil_to_qimage(small))
    dlg.body_layout.addWidget(view, 1)
    values: dict[str, int] = {}
    sliders: dict[str, QSlider] = {}
    grid_holder = QWidget()
    grid = QGridLayout(grid_holder)
    grid.setContentsMargins(0, 4, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(4)
    timer = QTimer(dlg)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: view.set_image(pil_to_qimage(adjust(small, values))))
    row = 0
    for section, items in (("Light", LIGHT), ("Color", COLOR)):
        head = _label(section, "section")
        grid.addWidget(head, row, 0, 1, 4)
        row += 1
        for name, lo, hi, _unit in items:
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(0)
            s.setAccessibleName(name)
            val = _label("+0")
            val.setMinimumWidth(70)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            reset = QPushButton()
            reset.setIcon(icons.pixmap("rotate", INK, 14, dlg.devicePixelRatioF()))
            reset.setFixedSize(28, 28)
            reset.setToolTip(f"Reset {name}")
            reset.setAccessibleName(f"Reset {name}")
            reset.setStyleSheet("QPushButton { background: transparent; } QPushButton:hover { background: rgba(255,255,255,0.5); }")

            def changed(v, n=name, l=val):
                values[n] = v
                l.setText(_fmt_val(n, v))
                timer.start(40)

            s.valueChanged.connect(changed)
            reset.clicked.connect(lambda _=False, sl=s: sl.setValue(0))
            grid.addWidget(_label(name), row, 0)
            grid.addWidget(s, row, 1)
            grid.addWidget(val, row, 2)
            grid.addWidget(reset, row, 3)
            grid.setColumnStretch(1, 1)
            sliders[name] = s
            row += 1
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid_holder)
    scroll.setMinimumHeight(250)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
    dlg.body_layout.addWidget(scroll)
    reset_all = QPushButton("Reset all")
    reset_all.clicked.connect(lambda: [s.setValue(0) for s in sliders.values()])
    dlg.foot_layout.insertWidget(0, reset_all)
    if not dlg.run():
        return None
    if not any(values.values()):
        return None
    return {"values": dict(values)}


# ------------------------------------------------------------ Crop

ASPECTS = [("Freeform", None), ("Original", "orig"), ("Square", 1.0), ("4:3", 4 / 3), ("3:2", 3 / 2),
           ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:5", 4 / 5)]


class CropView(ImageView):
    changed = Signal()
    HANDLE = 9

    def __init__(self):
        super().__init__(400)
        self.box = QRectF()           # in image coordinates
        self.aspect: float | None = None
        self._drag = None
        self._start = None
        self.setMouseTracking(True)

    def reset(self):
        self.box = QRectF(0, 0, self.img_w, self.img_h)
        self.apply_aspect()

    def apply_aspect(self):
        if self.aspect:
            c = self.box.center()
            w, h = self.box.width(), self.box.height()
            if w / h > self.aspect:
                w = h * self.aspect
            else:
                h = w / self.aspect
            self.box = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
            self._clamp()
        self.changed.emit()
        self.update()

    def _clamp(self):
        b = self.box
        w, h = min(b.width(), self.img_w), min(b.height(), self.img_h)
        x = min(max(0, b.left()), self.img_w - w)
        y = min(max(0, b.top()), self.img_h - h)
        self.box = QRectF(x, y, w, h)

    def _view_box(self) -> QRectF:
        return QRectF(self.to_view(self.box.topLeft()), self.to_view(self.box.bottomRight()))

    def _hit(self, pos: QPointF):
        vb = self._view_box()
        hs = self.HANDLE * 1.6
        for name, pt in (("tl", vb.topLeft()), ("tr", vb.topRight()), ("bl", vb.bottomLeft()), ("br", vb.bottomRight())):
            if abs(pos.x() - pt.x()) < hs and abs(pos.y() - pt.y()) < hs:
                return name
        if vb.contains(pos):
            return "move"
        return None

    def mousePressEvent(self, e):
        self._drag = self._hit(e.position())
        self._start = (self.to_img(e.position()), QRectF(self.box))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            h = self._hit(pos)
            self.setCursor(Qt.SizeAllCursor if h == "move" else Qt.SizeFDiagCursor if h in ("tl", "br")
                           else Qt.SizeBDiagCursor if h else Qt.ArrowCursor)
            return
        p0, b0 = self._start
        p = self.to_img(pos)
        dx, dy = p.x() - p0.x(), p.y() - p0.y()
        b = QRectF(b0)
        if self._drag == "move":
            b.translate(dx, dy)
            self.box = b
            self._clamp()
        else:
            fixed = {"tl": b0.bottomRight(), "tr": b0.bottomLeft(), "bl": b0.topRight(), "br": b0.topLeft()}[self._drag]
            px = min(max(p.x(), 0), self.img_w)
            py = min(max(p.y(), 0), self.img_h)
            w, h = abs(px - fixed.x()), abs(py - fixed.y())
            if self.aspect:
                if w / max(h, 1e-6) > self.aspect:
                    w = h * self.aspect
                else:
                    h = w / self.aspect
            w, h = max(w, 8), max(h, 8)
            x = fixed.x() - w if px < fixed.x() else fixed.x()
            y = fixed.y() - h if py < fixed.y() else fixed.y()
            self.box = QRectF(x, y, w, h)
            self._clamp()
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_base(p)
        if self.pix and not self.box.isEmpty():
            f = self.fit_rect()
            vb = self._view_box()
            shade = QPainterPath()
            shade.addRect(f)
            hole = QPainterPath()
            hole.addRect(vb)
            p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
            p.setPen(QPen(QColor(255, 255, 255, 120), 1))
            for i in (1, 2):
                x = vb.left() + vb.width() * i / 3
                y = vb.top() + vb.height() * i / 3
                p.drawLine(QPointF(x, vb.top()), QPointF(x, vb.bottom()))
                p.drawLine(QPointF(vb.left(), y), QPointF(vb.right(), y))
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawRect(vb)
            p.setBrush(QColor("#FFFFFF"))
            p.setPen(QPen(QColor(ACCENT), 2))
            for pt in (vb.topLeft(), vb.topRight(), vb.bottomLeft(), vb.bottomRight()):
                p.drawRoundedRect(QRectF(pt.x() - self.HANDLE, pt.y() - self.HANDLE, 2 * self.HANDLE, 2 * self.HANDLE), 4, 4)
        p.end()


def crop_editor(preview: QImage, full_size: tuple[int, int], title="Crop Image", note: str = "") -> dict | None:
    dlg = Panel(title, width=760)
    view = CropView()
    view.set_image(preview, full_size)
    dlg.body_layout.addWidget(view, 1)
    combo = QComboBox()
    combo.addItems([n for n, _ in ASPECTS])
    combo.setAccessibleName("Aspect ratio")
    size_l = _label("", "hint")
    reset = QPushButton("Reset")
    dlg.body_layout.addWidget(_row(_label("Aspect ratio"), combo, reset, size_l, stretch_at=3))
    if note:
        dlg.body_layout.addWidget(_label(note, "hint"))

    def on_aspect(i):
        a = ASPECTS[i][1]
        view.aspect = full_size[0] / full_size[1] if a == "orig" else a
        view.apply_aspect()

    def upd():
        b = view.box
        size_l.setText(f"{int(round(b.width()))} × {int(round(b.height()))} px")

    combo.currentIndexChanged.connect(on_aspect)
    reset.clicked.connect(lambda: (combo.setCurrentIndex(0), view.reset()))
    view.changed.connect(upd)
    view.reset()
    if not dlg.run():
        return None
    b = view.box
    x, y = int(round(b.left())), int(round(b.top()))
    w, h = int(round(b.width())), int(round(b.height()))
    if (x, y, w, h) == (0, 0, full_size[0], full_size[1]):
        return None
    return {"box": (x, y, w, h)}


# ------------------------------------------------------------ Redact / Annotate

SWATCHES = ["#111111", "#FFFFFF", "#F04A0E", "#FFD23F", "#3FA34D", "#2D7FF9", "#B04AE8"]


class MarkupView(ImageView):
    changed = Signal()

    def __init__(self):
        super().__init__(440)
        self.shapes: list[dict] = []     # image-space shapes
        self.tool = "box"
        self.color = SWATCHES[0]
        self.width = 6
        self.current: dict | None = None
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        p = self.to_img(e.position())
        if self.tool == "text":
            text, ok = QInputDialog.getText(self, "Text", "Text to add:")
            if ok and text.strip():
                self.shapes.append({"tool": "text", "pts": [p], "color": self.color, "width": self.width,
                                    "text": text.strip()})
                self.changed.emit()
                self.update()
            return
        self.current = {"tool": self.tool, "pts": [p, p], "color": self.color, "width": self.width}

    def mouseMoveEvent(self, e):
        if self.current:
            p = self.to_img(e.position())
            if self.current["tool"] in ("pen", "highlight"):
                self.current["pts"].append(p)
            else:
                self.current["pts"][1] = p
            self.update()

    def mouseReleaseEvent(self, e):
        if self.current:
            a, b = self.current["pts"][0], self.current["pts"][-1]
            if self.current["tool"] in ("pen", "highlight") or abs(a.x() - b.x()) + abs(a.y() - b.y()) > 3:
                self.shapes.append(self.current)
                self.changed.emit()
            self.current = None
            self.update()

    def undo(self):
        if self.shapes:
            self.shapes.pop()
            self.changed.emit()
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_base(p)
        p.setClipRect(self.fit_rect())
        k = self.scale()
        p.translate(self.fit_rect().topLeft())
        p.scale(k, k)
        for s in self.shapes + ([self.current] if self.current else []):
            draw_shape(p, s, preview=True)
        p.end()


def _rect(s) -> QRectF:
    a, b = s["pts"][0], s["pts"][-1]
    return QRectF(a, b).normalized()


def draw_shape(p: QPainter, s: dict, preview: bool = False):
    """Draw one annotation shape in image coordinates (redactions only preview here)."""
    t, col, w = s["tool"], QColor(s["color"]), s["width"]
    p.setRenderHint(QPainter.Antialiasing)
    if t in ("box", "blur", "pixelate"):
        r = _rect(s)
        if t == "box":
            p.fillRect(r, QColor(s["color"]) if s["color"] != "#FFFFFF" else QColor("#FFFFFF"))
        elif preview:
            p.fillRect(r, QColor(120, 120, 120, 170))
            p.setPen(QPen(QColor("#FFFFFF"), 2, Qt.DashLine))
            p.drawRect(r)
            p.setPen(QColor("#FFFFFF"))
            f = QFont("Segoe UI")
            f.setPixelSize(max(10, int(min(r.height() * 0.3, 28))))
            p.setFont(f)
            p.drawText(r, Qt.AlignCenter, "Blur" if t == "blur" else "Pixelate")
        return
    pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    if t == "highlight":
        c = QColor(col)
        c.setAlpha(110)
        pen = QPen(c, w * 4, Qt.SolidLine, Qt.FlatCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if t in ("pen", "highlight"):
        p.drawPolyline(QPolygonF(s["pts"]))
    elif t == "rect":
        p.drawRoundedRect(_rect(s), w, w)
    elif t == "ellipse":
        p.drawEllipse(_rect(s))
    elif t == "arrow":
        a, b = s["pts"][0], s["pts"][-1]
        p.drawLine(a, b)
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        L = w * 4.5
        head = QPolygonF([b, QPointF(b.x() - L * math.cos(ang - 0.45), b.y() - L * math.sin(ang - 0.45)),
                          QPointF(b.x() - L * math.cos(ang + 0.45), b.y() - L * math.sin(ang + 0.45))])
        p.setBrush(col)
        p.drawPolygon(head)
    elif t == "text":
        f = QFont("Segoe UI")
        f.setBold(True)
        f.setPixelSize(int(w * 6))
        p.setFont(f)
        p.setPen(col)
        p.drawText(s["pts"][0], s["text"])


def render_markup(im: Image.Image, shapes: list[dict]) -> Image.Image:
    """Full-resolution result: pixel redactions with PIL, then vector shapes with Qt."""
    out = im.convert("RGBA")
    for s in shapes:
        if s["tool"] in ("blur", "pixelate"):
            r = _rect(s)
            box = (max(0, int(r.left())), max(0, int(r.top())), min(out.width, int(r.right())),
                   min(out.height, int(r.bottom())))
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                continue
            region = out.crop(box)
            if s["tool"] == "blur":
                region = region.filter(ImageFilter.GaussianBlur(max(8, min(region.size) // 6)))
            else:
                n = max(6, min(region.size) // 12)
                small = region.resize((max(1, region.width // n), max(1, region.height // n)), Image.Resampling.BILINEAR)
                region = small.resize(region.size, Image.Resampling.NEAREST)
            out.paste(region, box[:2])
    vec = [s for s in shapes if s["tool"] not in ("blur", "pixelate")]
    if vec:
        q = pil_to_qimage(out)
        p = QPainter(q)
        for s in vec:
            draw_shape(p, s)
        p.end()
        out = qimage_to_pil(q)
    return out


REDACT_TOOLS = [("Black box", "box"), ("Blur", "blur"), ("Pixelate", "pixelate")]
ANNOTATE_TOOLS = [("Pen", "pen"), ("Arrow", "arrow"), ("Box", "rect"), ("Ellipse", "ellipse"),
                  ("Highlight", "highlight"), ("Text", "text")]


def markup_editor(im: Image.Image, mode: str) -> dict | None:
    redact = mode == "redact"
    dlg = Panel("Redact" if redact else "Annotate", width=820)
    small, k = preview_copy(im)
    view = MarkupView()
    view.set_image(pil_to_qimage(small), im.size)
    tools = REDACT_TOOLS if redact else ANNOTATE_TOOLS
    view.tool = tools[0][1]
    seg = Segmented([n for n, _ in tools], 0)
    seg.changed.connect(lambda i: setattr(view, "tool", tools[i][1]))
    dlg.body_layout.addWidget(seg)
    if not redact:
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(_label("Colour"))
        btns = []
        for c in SWATCHES:
            b = QPushButton()
            b.setFixedSize(26, 26)
            b.setCheckable(True)
            b.setAccessibleName(f"Colour {c}")
            b.setStyleSheet(f"QPushButton {{ background: {c}; border-radius: 13px; border: 2px solid rgba(0,0,0,0.25); }}"
                            f"QPushButton:checked {{ border: 3px solid {INK}; }}")
            b.clicked.connect(lambda _=False, cc=c, bb=b: ([x.setChecked(x is bb) for x in btns], setattr(view, "color", cc)))
            btns.append(b)
            sl.addWidget(b)
        btns[2].setChecked(True)
        view.color = SWATCHES[2]
        sl.addSpacing(14)
        sl.addWidget(_label("Size"))
        size = QSlider(Qt.Horizontal)
        size.setRange(2, 30)
        base = max(2, int(max(im.size) / 300))
        size.setValue(min(30, base + 2))
        view.width = size.value()
        size.valueChanged.connect(lambda v: setattr(view, "width", v))
        sl.addWidget(size, 1)
        dlg.body_layout.addWidget(sw)
    dlg.body_layout.addWidget(view, 1)
    hint = _label("Drag on the image to " + ("cover what you want to hide." if redact else "draw.") +
                  "  Ctrl+Z undoes the last mark.", "hint")
    dlg.body_layout.addWidget(hint)
    undo = QPushButton("Undo")
    undo.clicked.connect(view.undo)
    dlg.foot_layout.insertWidget(0, undo)
    QShortcut(QKeySequence.Undo, dlg, activated=view.undo)
    if not dlg.run() or not view.shapes:
        return None
    return {"shapes": view.shapes}
