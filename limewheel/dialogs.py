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


__all__ = ["trim_dialog", "slider_dialog", "fields_dialog", "Panel", "QCheckBox"]
