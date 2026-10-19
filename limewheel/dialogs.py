"""Tool dialogs in the panel style: Trim (waveform), Resize, Split, generic fields."""
from __future__ import annotations

import array
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QSlider, QWidget

from .brand import B
from .ui import ACCENT, INK, Panel

BUCKETS = 180


def fmt(secs: float) -> str:
    m, s = divmod(max(secs, 0.0), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


# ------------------------------------------------------------ waveform data

class _PeakSignals(QObject):
    ready = Signal(list)


class _Peaks(QRunnable):
    def __init__(self, path: Path):
        super().__init__()
        self.path, self.signals = path, _PeakSignals()

    def run(self):
        peaks: list[float] = []
        try:
            from .ops.base import CREATE_NO_WINDOW
            from .ops.media import ffmpeg
            r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(self.path), "-vn",
                                "-ac", "1", "-ar", "2000", "-f", "s16le", "-"],
                               capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            a = array.array("h")
            a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
            if len(a):
                step = max(1, len(a) // BUCKETS)
                raw = [max(abs(v) for v in a[i:i + step]) for i in range(0, step * BUCKETS, step) if a[i:i + step]]
                top = max(raw) or 1
                peaks = [v / top for v in raw]
        except Exception:
            peaks = []
        try:
            self.signals.ready.emit(peaks)
        except RuntimeError:
            pass


class RangeWave(QWidget):
    """Waveform with draggable start/end handles (keyboard: ←/→ move, Tab switches handle)."""
    changed = Signal(float, float)

    HANDLE = 14

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(118)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Trim range")
        self.peaks: list[float] = []
        self.start, self.end = 0.0, 1.0
        self._drag: str | None = None
        self.active = "start"

    def set_peaks(self, peaks: list[float]):
        self.peaks = peaks
        self.update()

    def _x(self, v: float) -> float:
        return self.HANDLE + v * (self.width() - 2 * self.HANDLE)

    def _v(self, x: float) -> float:
        return min(1.0, max(0.0, (x - self.HANDLE) / max(1, self.width() - 2 * self.HANDLE)))

    def mousePressEvent(self, e):
        x = e.position().x()
        self._drag = "start" if abs(x - self._x(self.start)) <= abs(x - self._x(self.end)) else "end"
        self.active = self._drag
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag:
            v = self._v(e.position().x())
            if self._drag == "start":
                self.start = min(v, self.end - 0.01)
            else:
                self.end = max(v, self.start + 0.01)
            self.changed.emit(self.start, self.end)
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        step = 0.05 if e.modifiers() & Qt.ShiftModifier else 0.01
        if e.key() in (Qt.Key_Left, Qt.Key_Right):
            d = -step if e.key() == Qt.Key_Left else step
            if self.active == "start":
                self.start = min(max(0.0, self.start + d), self.end - 0.01)
            else:
                self.end = max(min(1.0, self.end + d), self.start + 0.01)
            self.changed.emit(self.start, self.end)
            self.update()
        elif e.key() == Qt.Key_Tab and self.active == "start":
            self.active = "end"
            self.update()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = QPainterPath()
        bg.addRoundedRect(r, 12, 12)
        p.fillPath(bg, QColor(255, 255, 255, 60))
        xs, xe = self._x(self.start), self._x(self.end)
        sel = QRectF(xs, r.top(), xe - xs, r.height())
        p.fillRect(sel, QColor(*B['select_soft'], 120))
        # bars
        mid = r.center().y()
        n = len(self.peaks) or BUCKETS
        inner_w = self.width() - 2 * self.HANDLE
        bw = inner_w / n
        for i in range(n):
            v = self.peaks[i] if self.peaks else 0.12
            h = max(2.0, v * (r.height() - 26) / 2)
            x = self.HANDLE + i * bw + bw / 2
            on = xs <= x <= xe
            p.setPen(QPen(QColor(ACCENT) if on else QColor(*B['wave_off'], 110), max(1.2, bw * 0.55),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
        # selection frame + handles
        p.setPen(QPen(QColor(ACCENT), 3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(xs, r.top() + 1.5, xe - xs, r.height() - 3), 6, 6)
        for which, x in (("start", xs), ("end", xe)):
            hr = QRectF(x - self.HANDLE if which == "start" else x, r.top(), self.HANDLE, r.height())
            path = QPainterPath()
            path.addRoundedRect(hr, 6, 6)
            p.fillPath(path, QColor(ACCENT))
            p.setPen(QPen(QColor("#FFFFFF"), 2.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(hr.center().x(), mid - 14), QPointF(hr.center().x(), mid + 14))
            if self.hasFocus() and which == self.active:
                p.fillPath(path, QColor(INK))      # focused handle turns dark: visible keyboard focus
                p.setPen(QPen(QColor("#FFFFFF"), 2.4, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(hr.center().x(), mid - 14), QPointF(hr.center().x(), mid + 14))
        p.end()


def _row(*widgets, stretch_at: int | None = 1) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for i, x in enumerate(widgets):
        if i == stretch_at:
            h.addStretch()
        h.addWidget(x)
    return w


def _label(text: str, role: str = "") -> QLabel:
    l = QLabel(text)
    if role:
        l.setProperty("role", role)
    return l


# ---------------------------------------------------------------- dialogs

def trim_dialog(path: Path, duration: float, kind: str) -> dict | None:
    dlg = Panel(f"Trim {kind}", width=520)
    sel = _label("", "hint")
    dlg.body_layout.addWidget(_row(_label("Drag the ends to trim", "hint"), sel))
    wave = RangeWave()
    dlg.body_layout.addWidget(wave)
    start_l, end_l = _label("", "mono"), _label("", "mono")
    dlg.body_layout.addWidget(_row(start_l, end_l))
    tip = _label("Tip: click the waveform, then use ← → (Shift for bigger steps) and Tab to switch ends.", "hint")
    tip.setWordWrap(True)
    dlg.body_layout.addWidget(tip)

    def refresh(s, e):
        sel.setText(f"{fmt((e - s) * duration)} selected")
        start_l.setText(f"Start  {fmt(s * duration)}")
        end_l.setText(f"End  {fmt(e * duration)}")

    wave.changed.connect(refresh)
    refresh(0.0, 1.0)
    job = _Peaks(path)
    job.setAutoDelete(False)
    job.signals.ready.connect(wave.set_peaks)
    dlg._job = job
    QThreadPool.globalInstance().start(job)
    wave.setFocus()
    if not dlg.run():
        return None
    start = wave.start * duration
    end = None if wave.end >= 0.999 else wave.end * duration
    return {"start": start, "end": end}


def slider_dialog(title: str, label: str, lo: int, hi: int, value: int, describe) -> int | None:
    dlg = Panel(title, width=440)
    value_l = _label("", "hint")
    dlg.body_layout.addWidget(_row(_label(label), value_l))
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setAccessibleName(label)
    dlg.body_layout.addWidget(s)
    note = _label("", "hint")
    note.setWordWrap(True)
    dlg.body_layout.addWidget(note)

    def upd(v):
        a, b = describe(v)
        value_l.setText(a)
        note.setText(b)

    s.valueChanged.connect(upd)
    upd(value)
    s.setFocus()
    return s.value() if dlg.run() else None


def fields_dialog(title: str, fields: list[tuple[str, str]], note: str = "") -> list[str] | None:
    dlg = Panel(title, width=400)
    if note:
        n = _label(note, "hint")
        n.setWordWrap(True)
        dlg.body_layout.addWidget(n)
    form = QFormLayout()
    form.setSpacing(10)
    edits = []
    for label, default in fields:
        e = QLineEdit(str(default))
        form.addRow(_label(label), e)
        edits.append(e)
    holder = QWidget()
    holder.setLayout(form)
    dlg.body_layout.addWidget(holder)
    if edits:
        edits[0].selectAll()
        edits[0].setFocus()
    if not dlg.run():
        return None
    return [e.text().strip() for e in edits]


class Segmented(QWidget):
    """Row of pill buttons, one selected (like the Mono | Stereo control in the reference)."""
    changed = Signal(int)

    def __init__(self, options: list[str], index: int = 0):
        super().__init__()
        from PySide6.QtWidgets import QButtonGroup, QPushButton
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, text in enumerate(options):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"QPushButton:checked {{ background: {ACCENT}; color: white; font-weight: 600; }}")
            self.group.addButton(b, i)
            h.addWidget(b)
        self.group.button(index).setChecked(True)
        self.group.idClicked.connect(self.changed.emit)

    def index(self) -> int:
        return self.group.checkedId()


SPEEDS = [0.25, 0.5, 0.75, 1.25, 1.5, 2.0, 4.0]


def speed_dialog(duration: float, has_video: bool) -> dict | None:
    dlg = Panel("Change Speed", width=470)
    dlg.body_layout.addWidget(_label("Playback speed"))
    seg = Segmented([f"{s:g}×" for s in SPEEDS], SPEEDS.index(2.0))
    dlg.body_layout.addWidget(seg)
    note = _label("", "hint")
    dlg.body_layout.addWidget(note)
    pitch = QCheckBox("Keep the voice pitch natural")
    pitch.setChecked(True)
    dlg.body_layout.addWidget(pitch)
    mute = QCheckBox("Remove the sound")
    if has_video:
        dlg.body_layout.addWidget(mute)

    def upd(i):
        s = SPEEDS[i]
        note.setText(f"New length ≈ {fmt(duration / s)}" if duration else "")

    seg.changed.connect(upd)
    upd(seg.index())
    if not dlg.run():
        return None
    return {"speed": SPEEDS[seg.index()], "pitch": pitch.isChecked(), "mute": mute.isChecked()}


CHANNEL_MODES = ["Stereo", "Mono", "Swap L/R", "Left only", "Right only"]


def channels_dialog(channels: int) -> dict | None:
    dlg = Panel("Convert Audio Channels", width=470)
    dlg.body_layout.addWidget(_label(f"Source: {'mono' if channels == 1 else f'{channels} channels'}", "hint"))
    seg = Segmented(CHANNEL_MODES, 1 if channels >= 2 else 0)
    dlg.body_layout.addWidget(seg)
    sliders = {}
    for side in ("Left", "Right"):
        val = _label("100%", "hint")
        val.setMinimumWidth(44)
        s = QSlider(Qt.Horizontal)
        s.setRange(0, 200)
        s.setValue(100)
        s.setAccessibleName(f"{side} volume")
        s.valueChanged.connect(lambda v, l=val: l.setText(f"{v}%"))
        name_l = _label(f"{side} volume")
        name_l.setMinimumWidth(96)
        row = _row(name_l, s, val, stretch_at=None)
        row.layout().setStretch(1, 1)
        dlg.body_layout.addWidget(row)
        sliders[side] = s
    if not dlg.run():
        return None
    return {"mode": CHANNEL_MODES[seg.index()], "left": sliders["Left"].value() / 100,
            "right": sliders["Right"].value() / 100}


BG_COLORS = [("White", "#FFFFFF"), ("Black", "#111111"), ("Light grey", "#ECECEC"),
             ("Accent", None), ("Transparent", "transparent"), ("Custom…", "custom")]


def background_dialog() -> dict | None:
    from PySide6.QtWidgets import QColorDialog
    dlg = Panel("Add Background", width=470)
    dlg.body_layout.addWidget(_label("Background colour"))
    seg = Segmented([n for n, _ in BG_COLORS], 0)
    dlg.body_layout.addWidget(seg)
    chosen = {"custom": "#FFD54F"}
    swatch = _label("", "hint")
    dlg.body_layout.addWidget(swatch)

    def pick(i):
        name, val = BG_COLORS[i]
        if val == "custom":
            c = QColorDialog.getColor(QColor(chosen["custom"]), dlg, "Background colour")
            if c.isValid():
                chosen["custom"] = c.name()
            swatch.setText(f"Custom colour {chosen['custom']}")
        else:
            swatch.setText("Keeps transparency (PNG)" if val == "transparent" else "")

    seg.changed.connect(pick)
    pad_val = _label("8%", "hint")
    pad = QSlider(Qt.Horizontal)
    pad.setRange(0, 40)
    pad.setValue(8)
    pad.setAccessibleName("Padding")
    pad.valueChanged.connect(lambda v: pad_val.setText(f"{v}%"))
    row = _row(_label("Padding"), pad, pad_val, stretch_at=None)
    row.layout().setStretch(1, 1)
    dlg.body_layout.addWidget(row)
    rounded = QCheckBox("Round the image corners")
    shadow = QCheckBox("Soft shadow under the image")
    shadow.setChecked(True)
    square = QCheckBox("Make the canvas square")
    for w in (rounded, shadow, square):
        dlg.body_layout.addWidget(w)
    if not dlg.run():
        return None
    name, val = BG_COLORS[seg.index()]
    color = ACCENT if val is None else chosen["custom"] if val == "custom" else val
    return {"color": color, "pad": pad.value() / 100, "rounded": rounded.isChecked(),
            "shadow": shadow.isChecked(), "square": square.isChecked()}


class BleepWave(QWidget):
    """Waveform where dragging adds bleep ranges; click a range then Delete to remove it."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(120)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Bleep ranges")
        self.peaks: list[float] = []
        self.ranges: list[list[float]] = []
        self.selected = -1
        self._new: list[float] | None = None

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.update()

    def _v(self, x):
        return min(1.0, max(0.0, x / max(1, self.width())))

    def mousePressEvent(self, e):
        v = self._v(e.position().x())
        for i, (a, b) in enumerate(self.ranges):
            if a <= v <= b:
                self.selected = i
                self.update()
                return
        self.selected = -1
        self._new = [v, v]

    def mouseMoveEvent(self, e):
        if self._new:
            self._new[1] = self._v(e.position().x())
            self.update()

    def mouseReleaseEvent(self, e):
        if self._new:
            a, b = sorted(self._new)
            if b - a > 0.003:
                self.ranges.append([a, b])
                self.ranges.sort()
                self.changed.emit()
            self._new = None
            self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and 0 <= self.selected < len(self.ranges):
            self.ranges.pop(self.selected)
            self.selected = -1
            self.changed.emit()
            self.update()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = QPainterPath()
        bg.addRoundedRect(r, 12, 12)
        p.fillPath(bg, QColor(255, 255, 255, 60))
        mid = r.center().y()
        n = len(self.peaks) or BUCKETS
        bw = self.width() / n
        spans = self.ranges + ([sorted(self._new)] if self._new else [])
        for i, (a, b) in enumerate(spans):
            x0, x1 = a * self.width(), b * self.width()
            p.fillRect(QRectF(x0, r.top(), max(2, x1 - x0), r.height()),
                       QColor(INK) if i == self.selected else QColor(ACCENT))
        for i in range(n):
            v = self.peaks[i] if self.peaks else 0.12
            h = max(2.0, v * (r.height() - 20) / 2)
            x = i * bw + bw / 2
            inside = any(a * self.width() <= x <= b * self.width() for a, b in spans)
            p.setPen(QPen(QColor("#FFFFFF") if inside else QColor(*B['wave_off'], 160), max(1.2, bw * 0.55),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
        p.end()


def bleep_dialog(path: Path, duration: float, make_preview, play, stop) -> dict | None:
    dlg = Panel("Bleep Audio", apply_text="Save Bleeped Copy", width=600)
    count = _label("", "hint")
    dlg.body_layout.addWidget(_row(_label("Bleeps"), count))
    wave = BleepWave()
    dlg.body_layout.addWidget(wave)
    times = _label("", "mono")
    times.setWordWrap(True)
    dlg.body_layout.addWidget(times)
    tip = _label("Drag across the waveform to add a bleep. Click one and press Delete to remove it.", "hint")
    tip.setWordWrap(True)
    dlg.body_layout.addWidget(tip)
    sound = Segmented(["Beep tone", "Silence"], 0)
    dlg.body_layout.addWidget(_row(_label("Replace with"), sound, stretch_at=None))
    from PySide6.QtWidgets import QPushButton
    from . import icons
    dpr = dlg.devicePixelRatioF()
    orig = QPushButton("Original")
    withb = QPushButton("With bleeps")
    stop_b = QPushButton("Stop")
    for b, ic in ((orig, "play"), (withb, "play"), (stop_b, "stop")):
        b.setIcon(icons.pixmap(ic, INK, 12, dpr))
    dlg.body_layout.addWidget(_row(_label("Preview"), orig, withb, stop_b, stretch_at=1))

    def secs():
        return [(a * duration, b * duration) for a, b in wave.ranges]

    def upd():
        n = len(wave.ranges)
        count.setText(str(n))
        times.setText("  ".join(f"{fmt(a)}–{fmt(b)}" for a, b in secs()) or "No bleeps yet.")
        dlg.apply_btn.setEnabled(n > 0)
        withb.setEnabled(n > 0)

    wave.changed.connect(upd)
    upd()
    orig.clicked.connect(lambda: play(make_preview([], "tone")))
    withb.clicked.connect(lambda: play(make_preview(secs(), "tone" if sound.index() == 0 else "silence")))
    stop_b.clicked.connect(stop)
    job = _Peaks(path)
    job.setAutoDelete(False)
    job.signals.ready.connect(wave.set_peaks)
    dlg._job = job
    QThreadPool.globalInstance().start(job)
    ok = dlg.run()
    stop()
    if not ok or not wave.ranges:
        return None
    return {"ranges": secs(), "sound": "tone" if sound.index() == 0 else "silence"}


__all__ = ["bleep_dialog", "trim_dialog", "slider_dialog", "fields_dialog", "speed_dialog", "channels_dialog",
           "background_dialog", "Segmented", "Panel", "QCheckBox"]
