"""The radial wheel overlay: frosted petals that appear under the cursor."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PySide6.QtCore import (QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import icons, sounds
from .ops import ops_for
from .ops.base import ext_of, force_foreground
from .brand import B, NAME
from .ui import ACCENT, INK, reduced_motion, ui_font

MODES = [("convert", "Convert"), ("tools", "Tools"), ("actions", "Actions")]
KEY_LABEL = {"shift": "Shift", "ctrl": "Ctrl", "alt": "Alt"}

# Proportions follow the product site's wheel (inner 48.6 / outer 119.5 / disc 126 on 272 px), x1.5.
CENTER_R = 67          # hole in the middle (thumbnail lives here)
RING_W = 106
RING_GAP = 6           # gap between rings and between petals (px)
RIM = 10               # frosted disc edge around the outermost ring
RING_CAP = [8, 14, 20]
SIZE = 2 * (CENTER_R + 3 * (RING_W + RING_GAP) + RIM) + 100
LABEL_FONT = "Segoe UI Variable Display"   # what the site's rounded font stack falls back to on Windows
HOT = QColor(B["hot"])
HOT_EDGE = QColor(*B["hot_edge"])
PETAL_EDGE = QColor(93, 102, 102, 56)
LABEL = QColor("#000000")
CAPTION = QColor(108, 121, 125)

PETAL_TOP = QColor(*B["petal_top"])
PETAL_BOTTOM = QColor(*B["petal_bottom"])
HOT_TOP = QColor(B["hot_top"])
HOT_BOTTOM = QColor(B["hot_bottom"])
RIM_FILL = QColor(*B["rim"])
PILL = QColor(*B["pill"])
# Glass petals: the brand's petal tint, but see-through so the frosted backdrop shows.
GLASS_TOP = QColor(*B["petal_top"][:3], 118)
GLASS_BOTTOM = QColor(*B["petal_bottom"][:3], 96)
IMAGE_EXTS = {"jpg", "png", "webp", "bmp", "gif", "tiff", "ico", "svg", "avif", "heic"}
VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "wmv", "m4v", "flv", "mpg", "mpeg", "3gp", "ts"}


class _ThumbSignals(QObject):
    ready = Signal(int, bytes)


class _VideoThumb(QRunnable):
    """Grab one frame with ffmpeg off the UI thread (keeps the drag smooth)."""

    def __init__(self, path: Path, token: int):
        super().__init__()
        self.path, self.token, self.signals = path, token, _ThumbSignals()

    def run(self):
        try:
            from .ops.media import ffmpeg
            from .ops.base import CREATE_NO_WINDOW
            r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", "0.5", "-i", str(self.path),
                                "-frames:v", "1", "-vf", "scale=180:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
            data = r.stdout if r.returncode == 0 else b""
        except Exception:
            data = b""
        try:
            self.signals.ready.emit(self.token, data)
        except RuntimeError:
            pass


def frosted_backdrop(centre, size: int):
    """Screenshot the whole screen under `centre` and blur it: the 'frosted glass' the wheel
    sits on (Windows has no smooth, round blur-behind for layered windows, so we make our own).
    Returns (pixmap, luma around centre, screen top-left) or None. The whole screen is kept so
    the wheel can move away from an edge without a new capture."""
    from PySide6.QtGui import QGuiApplication
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageStat
        from .editors import pil_to_qimage, qimage_to_pil
    except Exception:
        return None
    screen = QGuiApplication.screenAt(centre) or QGuiApplication.primaryScreen()
    geo = screen.geometry()
    shot = screen.grabWindow(0, 0, 0, geo.width(), geo.height())
    if shot.isNull():
        return None
    im = qimage_to_pil(shot.toImage()).convert("RGB")
    w, h = im.size
    small = im.resize((max(1, w // 5), max(1, h // 5)), Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(4))
    small = ImageEnhance.Color(small).enhance(1.6)            # vibrancy, like macOS materials
    small = ImageEnhance.Brightness(small).enhance(1.1)
    pm = QPixmap.fromImage(pil_to_qimage(small.resize((w, h), Image.Resampling.BICUBIC)))
    pm.setDevicePixelRatio(shot.devicePixelRatio())
    k = small.width / max(1, geo.width())
    cx, cy, r = (centre.x() - geo.x()) * k, (centre.y() - geo.y()) * k, size * k / 2
    area = small.crop((max(0, int(cx - r)), max(0, int(cy - r)), max(1, int(cx + r)), max(1, int(cy + r))))
    luma = ImageStat.Stat(area.convert("L")).mean[0] / 255          # 0 dark .. 1 light
    return pm, luma, geo.topLeft()


def _pt(c: QPointF, r: float, deg: float) -> QPointF:
    a = math.radians(deg)
    return QPointF(c.x() + r * math.sin(a), c.y() - r * math.cos(a))


def _lerp(a: QPointF, b: QPointF, t: float) -> QPointF:
    return QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)


def petal_path(c: QPointF, r0: float, r1: float, a0: float, a1: float, gap: float,
               ro: float = 20, ri: float = 9) -> QPainterPath:
    """Annular sector with a constant pixel gap and rounded corners (angles clockwise from 12 o'clock)."""
    half = gap / 2
    o0, o1 = a0 + math.degrees(half / r1), a1 - math.degrees(half / r1)
    i0, i1 = a0 + math.degrees(half / r0), a1 - math.degrees(half / r0)
    if i1 - i0 < 1:                       # inner edge collapses to a point
        i0 = i1 = (a0 + a1) / 2
    ro = min(ro, (r1 - r0) / 3, math.radians(o1 - o0) * r1 / 2.4)
    ri = min(ri, (r1 - r0) / 4, max(math.radians(i1 - i0) * r0 / 2.4, 0.01))
    K_os, K_oe = _pt(c, r1, o0), _pt(c, r1, o1)
    K_is, K_ie = _pt(c, r0, i0), _pt(c, r0, i1)
    side_s = math.hypot(K_os.x() - K_is.x(), K_os.y() - K_is.y()) or 1
    side_e = math.hypot(K_oe.x() - K_ie.x(), K_oe.y() - K_ie.y()) or 1

    path = QPainterPath()
    path.moveTo(_lerp(K_os, K_is, ro / side_s))
    oa0, oa1 = o0 + math.degrees(ro / r1), o1 - math.degrees(ro / r1)
    path.quadTo(K_os, _pt(c, r1, oa0))
    steps = max(6, int(oa1 - oa0))
    for k in range(1, steps + 1):
        path.lineTo(_pt(c, r1, oa0 + (oa1 - oa0) * k / steps))
    path.quadTo(K_oe, _lerp(K_oe, K_ie, ro / side_e))
    if i1 > i0:
        path.lineTo(_lerp(K_ie, K_oe, ri / side_e))
        ia1, ia0 = i1 - math.degrees(ri / r0), i0 + math.degrees(ri / r0)
        path.quadTo(K_ie, _pt(c, r0, ia1))
        steps = max(4, int(ia1 - ia0))
        for k in range(1, steps + 1):
            path.lineTo(_pt(c, r0, ia1 - (ia1 - ia0) * k / steps))
        path.quadTo(K_is, _lerp(K_is, K_os, ri / side_s))
    else:
        tip_e = _lerp(K_ie, K_oe, ri * 1.6 / side_e)
        tip_s = _lerp(K_is, K_os, ri * 1.6 / side_s)
        path.lineTo(tip_e)
        path.quadTo(K_ie, tip_s)
    path.closeSubpath()
    return path


def _human_size(paths: list[Path]) -> str:
    if len(paths) > 1:
        return f"{len(paths)} items"
    p = paths[0]
    if p.is_dir():
        return "Folder"
    try:
        n = p.stat().st_size
    except OSError:
        return p.name
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class Wheel(QWidget):
    run_requested = Signal(object, list)   # op, paths
    visibility_changed = Signal(bool)

    def __init__(self, settings: dict):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.settings = settings
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"{NAME} wheel")
        self.resize(SIZE, SIZE)
        self.paths: list[Path] | None = None
        self.mode = "convert"
        self.ops = []
        self.slices = []         # (ring, start_deg_clockwise_from_top, span, op_index)
        self.paths_cache: dict[int, QPainterPath] = {}
        self.rings = 1
        self.hover = -1
        self.keyboard = False
        self.dropped = False
        self.thumb: QPixmap | None = None
        self.glyph: str | None = None      # category icon when there's no picture preview
        self.backdrop: QPixmap | None = None
        self.backdrop_luma = 1.0
        self.backdrop_origin = None          # screen top-left of the backdrop pixmap
        self.anchor = None                   # where the cursor was when the wheel opened
        self._thumb_token = 0
        self._thumb_jobs: set = set()
        self.size_text = ""
        self.motion = not reduced_motion()
        self.glow: dict[int, float] = {}          # per-slice highlight, eased toward 0/1
        self._fade = QTimer(self)
        self._fade.timeout.connect(self._step_glow)
        self.scale = 1.0
        self._pop = QVariantAnimation(self)
        self._pop.setDuration(150)
        self._pop.setStartValue(0.86)
        self._pop.setEndValue(1.0)
        self._pop.valueChanged.connect(self._set_scale)

    # ------------------------------------------------------------ opening
    def key_for(self, mode: str) -> str:
        return KEY_LABEL.get(self.settings["keys"].get(mode, ""), "?")

    def open_drag(self, mode: str):
        self.keyboard, self.dropped = False, False
        self._set_paths(None)
        self.set_mode(mode)
        self._show_at(QCursor.pos())

    def open_keyboard(self, paths: list[Path], mode: str = "convert"):
        self.keyboard, self.dropped = True, False
        self._set_paths(paths)
        self.set_mode(mode)
        self._show_at(QCursor.pos())
        self.activateWindow()
        force_foreground(int(self.winId()))
        self.setFocus()

    def _show_at(self, pos):
        self.anchor = pos
        if not self.isVisible():
            got = frosted_backdrop(pos, SIZE)
            self.backdrop, self.backdrop_luma, self.backdrop_origin = got if got else (None, 1.0, None)
        self._place()
        self.glow.clear()
        self.show()
        self.raise_()
        if self.motion:
            self._pop.start()
        self.visibility_changed.emit(True)

    def _place(self):
        """Centre the wheel on the cursor, but slide it inward when the cursor is near a screen
        edge so every petal, the key chips and the caption stay visible. The cursor always stays
        inside our window, so the drop still lands on us."""
        from PySide6.QtGui import QGuiApplication
        pos = getattr(self, "anchor", None) or QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        a = screen.availableGeometry()
        r = self._outer() + RIM
        top, bottom, side = r + 48, r + 44, r + 6        # chips above, caption below
        cx = min(max(pos.x(), a.left() + side), a.right() - side) if a.width() > 2 * side else pos.x()
        cy = min(max(pos.y(), a.top() + top), a.bottom() - bottom) if a.height() > top + bottom else pos.y()
        lim = SIZE // 2 - 4                                 # never push the cursor out of the window
        cx = min(max(cx, pos.x() - lim), pos.x() + lim)
        cy = min(max(cy, pos.y() - lim), pos.y() + lim)
        self.move(cx - SIZE // 2, cy - SIZE // 2)

    def _set_scale(self, v):
        self.scale = float(v)
        self.update()

    def _set_paths(self, paths):
        self.paths = paths
        self.thumb, self.glyph, self.size_text = None, None, ""
        if not paths:
            return
        self.size_text = _human_size(paths)
        p = paths[0]
        if ext_of(p) in IMAGE_EXTS:
            r = QImageReader(str(p))
            s = r.size()
            if s.isValid() and s.width() and s.height():
                k = 150 / max(s.width(), s.height())
                r.setScaledSize(QSize(max(1, int(s.width() * k)), max(1, int(s.height() * k))))
            img = r.read()
            if not img.isNull():
                self.thumb = QPixmap.fromImage(img)
        if self.thumb is None:
            self.glyph = icons.for_file(ext_of(p), p.is_dir())
            if ext_of(p) in VIDEO_EXTS:
                self._thumb_token += 1
                job = _VideoThumb(p, self._thumb_token)
                job.setAutoDelete(False)
                self._thumb_jobs.add(job)
                job.signals.ready.connect(self._video_thumb)
                job.signals.ready.connect(lambda *_: self._thumb_jobs.discard(job))
                QThreadPool.globalInstance().start(job)

    def _video_thumb(self, token: int, data: bytes):
        if token == self._thumb_token and data:
            pm = QPixmap()
            if pm.loadFromData(data):
                self.thumb, self.glyph = pm, None
                self.update()

    def set_mode(self, mode: str):
        self.mode = mode
        self._rebuild()
        self.update()

    def cycle_mode(self, step: int):
        names = [m for m, _ in MODES]
        self.set_mode(names[(names.index(self.mode) + step) % len(names)])

    def _rebuild(self):
        self.ops = ops_for(self.paths, self.mode) if self.paths else []
        n = len(self.ops)
        self.slices, self.paths_cache = [], {}
        self.rings = 1
        if n:
            rings = 1
            while sum(RING_CAP[:rings]) < n and rings < len(RING_CAP):
                rings += 1
            caps = RING_CAP[:rings]
            counts = [max(1, round(n * c / sum(caps))) for c in caps]
            counts[-1] = n - sum(counts[:-1])
            while counts[-1] < 1:
                counts[counts.index(max(counts))] -= 1
                counts[-1] += 1
            idx = 0
            for ring, cnt in enumerate(counts):
                span = 360 / cnt
                for k in range(cnt):
                    self.slices.append((ring, k * span - span / 2, span, idx))
                    idx += 1
            self.rings = rings
        self.hover = 0 if (self.keyboard and n) else -1
        self.glow.clear()
        if self.isVisible():
            self._place()                     # more rings = bigger wheel; keep it on screen

    def hideEvent(self, e):
        self.visibility_changed.emit(False)
        super().hideEvent(e)

    def close_wheel(self):
        self.hide()

    def hide_if_idle(self):
        if self.isVisible() and not self.keyboard and not self.dropped:
            self.hide()

    # ----------------------------------------------------------- geometry
    def _center(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    @staticmethod
    def _radii(ring: int) -> tuple[float, float]:
        r0 = CENTER_R + RING_GAP + ring * (RING_W + RING_GAP)
        return r0, r0 + RING_W

    def _outer(self) -> float:
        return self._radii(self.rings - 1)[1]

    def _hit(self, pos: QPointF) -> int:
        c = self._center()
        dx, dy = pos.x() - c.x(), pos.y() - c.y()
        d = math.hypot(dx, dy)
        if d < CENTER_R + RING_GAP / 2:
            return -1
        ring = int((d - CENTER_R - RING_GAP / 2) // (RING_W + RING_GAP))
        if ring >= self.rings:
            return -1
        ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360   # clockwise from top
        for r, start, span, idx in self.slices:
            if r == ring and (ang - start) % 360 < span:
                return idx
        return -1

    def _set_hover(self, idx: int):
        if idx != self.hover:
            self.hover = idx
            if idx >= 0 and self.isVisible() and self.settings.get("sounds", True):
                sounds.play("highlight")
            if self.motion:
                if not self._fade.isActive():
                    self._fade.start(16)
            else:
                self.glow = {idx: 1.0} if idx >= 0 else {}
            self.update()

    def _step_glow(self):
        done = True
        for i in set(self.glow) | ({self.hover} if self.hover >= 0 else set()):
            target = 1.0 if i == self.hover else 0.0
            cur = self.glow.get(i, 0.0)
            step = 0.22 if target > cur else 0.14          # enter faster than exit reads as snappy
            nxt = min(target, cur + step) if target > cur else max(target, cur - step)
            self.glow[i] = nxt
            done &= abs(nxt - target) < 1e-3
        self.glow = {k: v for k, v in self.glow.items() if v > 0}
        if done:
            self._fade.stop()
        self.update()

    # ------------------------------------------------------------- events
    def dragEnterEvent(self, e):
        md = e.mimeData()
        paths = [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []
        if not paths:
            e.ignore()
            return
        if paths != self.paths:
            self._set_paths(paths)
            self._rebuild()
        self._set_hover(self._hit(e.position()))
        e.setDropAction(Qt.CopyAction)
        e.accept()
        self.update()

    def dragMoveEvent(self, e):
        self._set_hover(self._hit(e.position()))
        if self.hover >= 0:
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_hover(-1)

    def dropEvent(self, e):
        idx = self._hit(e.position())
        if idx < 0:
            e.ignore()
            return
        e.setDropAction(Qt.CopyAction)
        e.accept()
        self._fire(idx)

    def _fire(self, idx: int):
        self.dropped = True
        op, paths = self.ops[idx], list(self.paths or [])
        self.hide()
        QTimer.singleShot(0, lambda: self.run_requested.emit(op, paths))

    def mouseMoveEvent(self, e):
        if self.keyboard:
            self._set_hover(self._hit(e.position()))

    def mousePressEvent(self, e):
        idx = self._hit(e.position())
        if idx >= 0:
            self._fire(idx)
        else:
            self.hide()

    def keyPressEvent(self, e):
        k, n = e.key(), len(self.ops)
        if k == Qt.Key_Escape:
            self.hide()
        elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self.hover >= 0:
            self._fire(self.hover)
        elif k in (Qt.Key_Right, Qt.Key_Down) and n:
            self._set_hover((self.hover + 1) % n)
        elif k in (Qt.Key_Left, Qt.Key_Up) and n:
            self._set_hover((self.hover - 1) % n)
        elif k == Qt.Key_Tab:
            self.cycle_mode(1)
        elif k == Qt.Key_Backtab:
            self.cycle_mode(-1)

    def focusOutEvent(self, e):
        if self.keyboard:
            QTimer.singleShot(100, lambda: self.hide() if not self.isActiveWindow() else None)

    # -------------------------------------------------------------- paint
    def _glass(self, p: QPainter, build):
        """Paint the blurred backdrop inside a shape (in window coordinates, unscaled)."""
        if self.backdrop is None:
            return
        path = QPainterPath()
        build(path)
        p.save()
        p.resetTransform()
        p.setClipPath(path)
        o = self.backdrop_origin - self.pos() if self.backdrop_origin is not None else None
        p.drawPixmap(o.x() if o else 0, o.y() if o else 0, self.backdrop)
        p.restore()

    @staticmethod
    def _bevel(p: QPainter, build, radius: float, width: float = 2.0, inset: bool = False,
               rect: QRectF | None = None, strength: float = 1.0):
        """Thick-glass edge: a white highlight fading from top-left to bottom-right, and a faint
        dark line just inside on the bottom-right (or the reverse for a recessed shape)."""
        path = QPainterPath()
        build(path)
        r = rect or path.boundingRect()
        light = QLinearGradient(r.topLeft(), r.bottomRight())
        hi, lo = QColor(255, 255, 255, int(235 * strength)), QColor(255, 255, 255, int(45 * strength))
        light.setColorAt(0.0, lo if inset else hi)
        light.setColorAt(0.55, QColor(255, 255, 255, int(90 * strength)))
        light.setColorAt(1.0, hi if inset else lo)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(light, width))
        p.drawPath(path)
        shade = QLinearGradient(r.topLeft(), r.bottomRight())
        shade.setColorAt(0.0, QColor(0, 0, 0, int(28 * strength) if inset else 0))
        shade.setColorAt(0.6, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 0 if inset else int(32 * strength)))
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(shade, width * 2.4))
        p.drawPath(path)
        p.restore()

    @staticmethod
    def _font(px: float, weight=QFont.DemiBold, spacing: float = 0.0, caps: bool = False) -> QFont:
        f = QFont(LABEL_FONT)
        if not f.exactMatch():
            f = ui_font(10, weight)
        f.setPixelSize(max(1, round(px)))
        f.setWeight(weight)
        if spacing:
            f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
        if caps:
            f.setCapitalization(QFont.AllUppercase)
        return f

    def _caption(self) -> str:
        if 0 <= self.hover < len(self.ops):
            op = self.ops[self.hover]
            label = op.label.rstrip("…")
            if self.mode == "convert" and op.badge and not icons.for_op(op):
                return f"Convert to {op.badge}"
            return label
        if self.paths:
            name = self.paths[0].name if len(self.paths) == 1 else ""
            return f"{name}  ·  {self.size_text}" if name else self.size_text
        return f"{dict(MODES)[self.mode]}: drop a file on a petal"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # Nearly invisible fill so the whole square accepts drops.
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        c = self._center()
        if self.scale != 1.0:
            p.translate(c)
            p.scale(self.scale, self.scale)
            p.translate(-c)
        outer = self._outer()
        rim = outer + RIM
        hole = CENTER_R - 4
        dark = 1 - min(1.0, self.backdrop_luma / 0.55)      # 0 on light desktops, 1 on very dark ones

        # Soft drop shadow under the disc (site: 0 5px 8px rgba(38,52,56,.125))
        p.setPen(Qt.NoPen)
        for k in range(6):
            p.setBrush(QColor(38, 52, 56, 7))
            p.drawEllipse(QPointF(c.x(), c.y() + 5), rim + 1 + k * 1.6, rim + 1 + k * 1.6)

        # Frosted disc: blurred desktop + a cool grey tint, a 1px inner white line. The hole is
        # left for the centre bubble.
        disc = QPainterPath()
        disc.addEllipse(c, rim, rim)
        disc.addEllipse(c, hole, hole)
        sdisc = QPainterPath()
        sdisc.addEllipse(c, rim * self.scale, rim * self.scale)
        self._glass(p, lambda path: path.addPath(sdisc))
        p.setBrush(QColor(192, 201, 205, int(64 + 60 * dark)))
        p.drawPath(disc)
        p.setBrush(QColor(236, 240, 241, int(70 + 90 * dark)))
        p.drawPath(disc)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 85), 1.0))
        p.drawEllipse(c, rim - 0.5, rim - 0.5)
        p.setPen(QPen(QColor(93, 102, 102, 40), 1.0))
        p.drawEllipse(c, rim, rim)

        # Centre bubble (white glass)
        p.setPen(QPen(PETAL_EDGE, 1.1))
        p.setBrush(QColor(255, 255, 255, int(205 + 30 * dark)))
        p.drawEllipse(c, hole, hole)

        if not self.slices:
            r0, r1 = self._radii(0)
            ring = QPainterPath()
            ring.addEllipse(c, r1, r1)
            ring.addEllipse(c, r0, r0)
            p.setPen(QPen(PETAL_EDGE, 1.1))
            p.setBrush(QColor(255, 255, 255, 190))
            p.drawPath(ring)
            p.setPen(LABEL)
            p.setFont(self._font(14))
            msg = "Move over the wheel" if self.paths is None else "No options for this file"
            p.drawText(QRectF(c.x() - r1, c.y() - (r0 + r1) / 2 - 12, 2 * r1, 24), Qt.AlignCenter, msg)

        dpr = self.devicePixelRatioF()
        for ring, start, span, idx in self.slices:
            r0, r1 = self._radii(ring)
            path = self.paths_cache.get(idx)
            if path is None:
                path = petal_path(c, r0, r1, start, start + span, RING_GAP, ro=30, ri=10)
                self.paths_cache[idx] = path
            g = self.glow.get(idx, 0.0)
            br = path.boundingRect()
            # Petal: milky white glass (site gradient #fff .8 -> #f3f6f7 .58 -> #f4f6f6 .75), the
            # hovered one fills with the accent.
            lift = 0.25 * dark
            grad = QRadialGradient(br.center(), max(br.width(), br.height()) / 2)
            # (alphas raised a little over the site's: our backdrop is a busy desktop, not a white page)
            for at, col, a in ((0.0, (255, 255, 255), 0.86), (0.68, (245, 247, 248), 0.74), (1.0, (244, 246, 246), 0.82)):
                base = QColor(*col, int(255 * min(1.0, a + lift)))
                grad.setColorAt(at, self._mix(base, HOT, g))
            p.setPen(QPen(self._mix(PETAL_EDGE, HOT_EDGE, g), 1.1))
            p.setBrush(grad)
            p.drawPath(path)

            op = self.ops[idx]
            mid = start + span / 2
            rm = (r0 + r1) / 2
            k = 1.0 if ring == 0 else 0.88
            fg = self._mix(LABEL, QColor("#FFFFFF"), g)
            box_w = max(64.0, min(RING_W - 4, 2 * rm * math.sin(math.radians(min(span, 140) / 2)) - 12))
            icon = icons.for_op(op)
            if op.badge and not icon:
                t = _pt(c, rm, mid)
                size = 20 if len(op.badge) <= 4 else 16
                p.setPen(fg)
                p.setFont(self._font(size * k, QFont.DemiBold, 0.5))
                p.drawText(QRectF(t.x() - box_w / 2, t.y() - 16, box_w, 32), Qt.AlignCenter, op.badge)
            else:
                t = _pt(c, rm, mid)                            # icon stacked above its label
                isz = round(24 * k)
                pm = icons.pixmap(icon or "sparkle", fg.name(), isz, dpr, 1.6)
                p.drawPixmap(QPointF(t.x() - isz / 2, t.y() - 12 * k - isz / 2), pm)
                p.setFont(self._font(12 * k, QFont.DemiBold, 0.3, caps=True))
                p.setPen(fg)
                label = op.label.rstrip("…")
                p.drawText(QRectF(t.x() - box_w / 2, t.y() + 4 * k, box_w, 34),
                           Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, label)

        # Thumbnail in the hole
        if self.thumb is not None:
            tw = 88.0
            pm = self.thumb
            k = tw / max(pm.width() / pm.devicePixelRatioF(), pm.height() / pm.devicePixelRatioF(), 1)
            w = pm.width() / pm.devicePixelRatioF() * k
            h = pm.height() / pm.devicePixelRatioF() * k
            r = QRectF(c.x() - w / 2, c.y() - h / 2 - 6, w, h)
            clip = QPainterPath()
            clip.addRoundedRect(r, 10, 10)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(r, pm, QRectF(pm.rect()))
            p.restore()
            p.setPen(QPen(QColor(0, 0, 0, 30), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(clip)
        elif self.glyph:
            gsz = 46
            p.drawPixmap(QPointF(c.x() - gsz / 2, c.y() - gsz / 2 - 16),
                         icons.pixmap(self.glyph, "#3A4246", gsz, dpr, 1.4))

        # Centre pill: hovered option, else size / mode (site: white .72, 24px radius, soft shadow)
        if 0 <= self.hover < len(self.ops):
            op = self.ops[self.hover]
            is_tool = not (op.badge and not icons.for_op(op))
            pill_text = op.label.rstrip("…") if is_tool else op.badge
        else:
            is_tool = False
            pill_text = self.size_text if self.paths else dict(MODES)[self.mode]
        f = self._font(11.5, QFont.DemiBold, 0.3, caps=True) if is_tool else self._font(16, QFont.Medium, 0.5)
        p.setFont(f)
        tw = min(max(p.fontMetrics().horizontalAdvance(pill_text) + 30, 105), hole * 2 - 6)
        ph = 40
        py = c.y() - ph / 2 + (30 if (self.thumb is not None or self.glyph) else 0)
        pr = QRectF(c.x() - tw / 2, py, tw, ph)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 10))
        p.drawRoundedRect(pr.translated(0, 2), ph / 2, ph / 2)
        p.setBrush(QColor(255, 255, 255, 235))
        p.setPen(QPen(QColor(255, 255, 255, 180), 1))
        p.drawRoundedRect(pr, ph / 2, ph / 2)
        p.setPen(LABEL)
        p.drawText(pr.adjusted(8, 0, -8, 0), Qt.AlignCenter,
                   p.fontMetrics().elidedText(pill_text, Qt.ElideMiddle, int(tw) - 16))

        p.resetTransform()
        # Caption under the wheel ("Convert to PNG"), on a small frosted pill so it reads anywhere
        cap = self._caption()
        p.setFont(ui_font(9.5, QFont.Medium))
        cw = min(p.fontMetrics().horizontalAdvance(cap) + 28, self.width() - 20)
        cy = c.y() + rim * self.scale + 12
        cr = QRectF(c.x() - cw / 2, cy, cw, 26)
        self._glass(p, lambda path: path.addRoundedRect(cr, 13, 13))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(170 + 50 * dark)))
        p.drawRoundedRect(cr, 13, 13)
        p.setPen(CAPTION if dark < 0.5 else QColor(70, 80, 84))
        p.drawText(cr, Qt.AlignCenter, p.fontMetrics().elidedText(cap, Qt.ElideMiddle, int(cw) - 20))

        # Mode chips (which key opens which wheel), styled like the site's key caps
        f = ui_font(8.5, QFont.DemiBold)
        p.setFont(f)
        labels = [(m, f"{self.key_for(m)}  {t}") for m, t in MODES]
        widths = [p.fontMetrics().horizontalAdvance(t) + 24 for _, t in labels]
        total = sum(widths) + 4 * (len(widths) - 1) + 8
        y = c.y() - (rim * self.scale) - 44
        group = QRectF(c.x() - total / 2, y, total, 32)
        self._glass(p, lambda path: path.addRoundedRect(group, 16, 16))
        p.setPen(QPen(QColor(93, 102, 102, 50), 1))
        p.setBrush(QColor(255, 255, 255, int(185 + 45 * dark)))
        p.drawRoundedRect(group, 16, 16)
        x = group.left() + 4
        for (m, t), w in zip(labels, widths):
            r = QRectF(x, y + 4, w, 24)
            active = m == self.mode
            p.setPen(Qt.NoPen)
            p.setBrush(HOT if active else QColor(255, 255, 255, 0))
            p.drawRoundedRect(r, 12, 12)
            p.setPen(QColor("#FFFFFF") if active else LABEL)
            p.drawText(r, Qt.AlignCenter, t)
            x += w + 4
        p.end()

    @staticmethod
    def _mix(a: QColor, b: QColor, t: float) -> QColor:
        return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                      int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))
