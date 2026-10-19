"""Look & feel: colour tokens, gradient panels, styled controls and progress toasts."""
from __future__ import annotations

import ctypes
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDialog, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from . import icons
from .brand import B, NAME

# ------------------------------------------------------------------ tokens
INK = B["ink"]              # primary text (> 7:1 on every panel stop)
INK_SOFT = B["ink_soft"]    # secondary text (> 4.5:1)
ACCENT = B["accent"]        # selection, primary buttons, slider fill
ACCENT_DARK = B["accent_dark"]
ACCENT_TEXT = "#FFFFFF"
HAIRLINE = B["hairline"]
FIELD = "rgba(255, 255, 255, 0.55)"
CHIP = "rgba(255, 255, 255, 0.42)"
SECTION = B["section"]      # section headings

PANEL_STOPS = B["panel_stops"]
T, TD, AR = B["tone"], B["tone_dark"], B["accent_rgb"]
SHADOW = B["shadow"]
POPUP = B["popup"]


def rgba(c, a):
    return f"rgba({c[0]},{c[1]},{c[2]},{a})"
RADIUS = 18
FONT = "Segoe UI Variable Text"


def ui_font(size: float, weight=QFont.Normal) -> QFont:
    f = QFont(FONT)
    if not f.exactMatch():
        f = QFont("Segoe UI")
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def reduced_motion() -> bool:
    """Honour Windows' 'Animation effects' accessibility switch."""
    val = ctypes.c_bool(True)
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(val), 0)  # SPI_GETCLIENTAREAANIMATION
    except Exception:
        return False
    return not val.value


def panel_gradient(rect: QRectF) -> QLinearGradient:
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    for pos, col in PANEL_STOPS:
        g.setColorAt(pos, QColor(col))
    return g


def _check_png() -> str:
    """Write the ✓ used by checked boxes (QSS images need a file path)."""
    import tempfile
    p = Path(tempfile.gettempdir()) / f"{NAME.lower()}-check.svg"
    if not p.exists():
        p.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" '
                     'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17.5 19 7"/></svg>')
    return p.as_posix()


CHECK_PNG = _check_png()
STYLESHEET = f"""
QWidget {{ color: {INK}; font-family: '{FONT}', 'Segoe UI'; font-size: 10pt; }}
QLabel[role="title"] {{ font-size: 13pt; font-weight: 600; }}
QLabel[role="hint"] {{ color: {INK_SOFT}; }}
QLabel[role="section"] {{ color: {SECTION}; font-weight: 600; }}
QLabel[role="mono"] {{ font-family: 'Cascadia Mono', Consolas, monospace; color: {INK_SOFT}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {FIELD}; border: 2px solid transparent; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: white;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {POPUP}; border-radius: 8px; selection-background-color: {ACCENT};
    selection-color: white; padding: 4px; }}
QPushButton {{ background: {CHIP}; border: 2px solid transparent; border-radius: 8px; padding: 6px 16px; }}
QPushButton:hover {{ background: rgba(255,255,255,0.62); }}
QPushButton:focus {{ border-color: {rgba(AR, 0.65)}; }}
QPushButton:disabled {{ color: rgba(36,22,13,0.38); }}
QPushButton[role="primary"] {{ background: {ACCENT}; color: {ACCENT_TEXT}; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: {ACCENT_DARK}; }}
QPushButton[role="primary"]:focus {{ border-color: {B['focus_dark']}; }}
QPushButton[role="close"] {{ background: rgba(255,255,255,0.32); border-radius: 15px; padding: 0; }}
QPushButton[role="close"]:hover {{ background: rgba(255,255,255,0.6); }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; background: rgba(255,255,255,0.55);
    border: 1.5px solid {B['unchecked']}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("{CHECK_PNG}"); }}
QCheckBox:focus {{ text-decoration: underline; }}
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {rgba(T, 0.25)}; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {ACCENT}; }}
QSlider::handle:horizontal {{ width: 22px; height: 22px; margin: -9px 0; border-radius: 11px; background: white;
    border: 1px solid {rgba(TD, 0.18)}; }}
QSlider::handle:horizontal:focus {{ border: 2px solid {ACCENT}; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 6px; margin: 2px 0; border-radius: 10px; background: rgba(255,255,255,0.38); }}
QListWidget::item:hover {{ background: rgba(255,255,255,0.6); }}
QListWidget::item:selected {{ background: {rgba(AR, 0.88)}; color: white; }}
QScrollBar:vertical {{ width: 8px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {rgba(TD, 0.25)}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QToolTip {{ background: {POPUP}; color: {INK}; border: 1px solid {HAIRLINE}; border-radius: 6px; padding: 4px; }}
"""


def close_button(parent: QWidget) -> QPushButton:
    b = QPushButton(parent)
    b.setProperty("role", "close")
    b.setFixedSize(30, 30)
    b.setFocusPolicy(Qt.NoFocus)          # Esc closes; keeps first focus on the content
    b.setIcon(icons.pixmap("close", INK, 14, parent.devicePixelRatioF(), 2.2))
    b.setIconSize(QSize(14, 14))
    b.setToolTip("Close (Esc)")
    b.setAccessibleName("Close")
    b.setCursor(Qt.PointingHandCursor)
    return b


def primary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("role", "primary")
    b.setCursor(Qt.PointingHandCursor)
    b.setDefault(True)
    return b


class _Surface(QWidget):
    """Paints the rounded gradient card; children go in self.body."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, RADIUS, RADIUS)
        p.fillPath(path, panel_gradient(r))
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        p.drawPath(path)
        p.end()


class Panel(QDialog):
    """Frameless dialog: round ✕, centred title, divider, body, footer with Apply."""

    def __init__(self, title: str, apply_text: str = "Apply", width: int = 420):
        super().__init__(None, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(f"{NAME} — {title}")
        self.setStyleSheet(STYLESHEET)
        self._drag: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 24)      # room for the shadow
        self.surface = _Surface(self)
        shadow = QGraphicsDropShadowEffect(self.surface)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(*SHADOW, 90))
        self.surface.setGraphicsEffect(shadow)
        outer.addWidget(self.surface)

        col = QVBoxLayout(self.surface)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(58)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 0, 14, 0)
        self.close_btn = close_button(self)
        self.close_btn.clicked.connect(self.reject)
        self.title = QLabel(title)
        self.title.setProperty("role", "title")
        self.title.setAlignment(Qt.AlignCenter)
        spacer = QWidget()
        spacer.setFixedSize(30, 30)
        hl.addWidget(self.close_btn)
        hl.addWidget(self.title, 1)
        hl.addWidget(spacer)
        col.addWidget(head)
        col.addWidget(self._rule())

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(24, 18, 24, 18)
        self.body_layout.setSpacing(12)
        col.addWidget(self.body, 1)

        col.addWidget(self._rule())
        foot = QWidget()
        self.foot_layout = QHBoxLayout(foot)
        self.foot_layout.setContentsMargins(24, 12, 20, 14)
        self.foot_layout.addStretch()
        self.apply_btn = primary(apply_text)
        self.apply_btn.clicked.connect(self.accept)
        self.foot_layout.addWidget(self.apply_btn)
        col.addWidget(foot)
        self.surface.setMinimumWidth(width)

    @staticmethod
    def _rule() -> QWidget:
        w = QWidget()
        w.setFixedHeight(1)
        w.setStyleSheet(f"background: {HAIRLINE};")
        return w

    # Drag the panel by its header.
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 76:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def run(self) -> bool:
        try:
            from .ops.base import force_foreground
        except ImportError:                 # the installer ships ui.py without the converters
            def force_foreground(hwnd):
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.adjustSize()
        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2)
        self.show()
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))
        return self.exec() == QDialog.Accepted


# ------------------------------------------------------------------ toasts

class _Bar(QWidget):
    """Thin progress bar: determinate when value() >= 0, else an animated sweep."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(6)
        self.value = -1.0
        self.phase = 0.0
        self._t = QTimer(self)
        self._t.timeout.connect(self._tick)
        if not reduced_motion():
            self._t.start(16)

    def _tick(self):
        self.phase = (self.phase + 0.012) % 1.0
        self.update()

    def stop(self, value=1.0):
        self._t.stop()
        self.value = value
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*T, 60))
        p.drawRoundedRect(r, 3, 3)
        p.setBrush(QColor(ACCENT))
        if self.value >= 0:
            p.drawRoundedRect(QRectF(0, 0, r.width() * self.value, r.height()), 3, 3)
        else:
            w = r.width() * 0.32
            x = (r.width() + w) * self.phase - w
            p.setClipRect(r)
            p.drawRoundedRect(QRectF(x, 0, w, r.height()), 3, 3)
        p.end()


class Toast(QWidget):
    closed = Signal(object)
    reveal = Signal(object)

    def __init__(self, title: str, detail: str):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(STYLESHEET)
        self.output: Path | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 18)
        self.surface = _Surface(self)
        sh = QGraphicsDropShadowEffect(self.surface)
        sh.setBlurRadius(28)
        sh.setOffset(0, 8)
        sh.setColor(QColor(*SHADOW, 80))
        self.surface.setGraphicsEffect(sh)
        outer.addWidget(self.surface)
        lay = QVBoxLayout(self.surface)
        lay.setContentsMargins(14, 12, 18, 16)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.close_btn = close_button(self)
        self.close_btn.clicked.connect(self.dismiss)
        self.title = QLabel(title)
        self.title.setFont(ui_font(11.5, QFont.DemiBold))
        top.addWidget(self.close_btn)
        top.addWidget(self.title, 1)
        lay.addLayout(top)
        self.detail = QLabel(detail)
        self.detail.setProperty("role", "hint")
        self.detail.setContentsMargins(4, 0, 0, 0)
        lay.addWidget(self.detail)
        self.bar = _Bar()
        lay.addWidget(self.bar)
        self.action = QPushButton("Show in folder")
        self.action.setCursor(Qt.PointingHandCursor)
        self.action.clicked.connect(lambda: (self.reveal.emit(self.output), self.dismiss()))
        self.action.hide()
        lay.addWidget(self.action, 0, Qt.AlignRight)
        self.setFixedWidth(380)
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.dismiss)

    def finish(self, title: str, detail: str, output: Path | None = None, error: bool = False):
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setWordWrap(True)
        self.bar.stop(1.0)
        self.bar.setVisible(not error)
        self.output = output
        self.action.setVisible(bool(output))
        if error:
            self.title.setStyleSheet(f"color: {SECTION};")
        self.adjustSize()
        self._life.start(9000 if error else 4500)
        self.closed.emit(None)          # ask the manager to re-stack (height changed)

    def dismiss(self):
        self.hide()
        self.closed.emit(self)
        self.deleteLater()


class Toasts:
    """Stacks toasts in the bottom-right corner of the primary screen."""

    def __init__(self):
        self.items: list[Toast] = []

    def show(self, title: str, detail: str, reveal=None) -> Toast:
        t = Toast(title, detail)
        t.closed.connect(self._closed)
        if reveal:
            t.reveal.connect(reveal)
        self.items.append(t)
        t.adjustSize()
        t.show()
        self._stack()
        return t

    def _closed(self, toast):
        if toast in self.items:
            self.items.remove(toast)
        self._stack()

    def _stack(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        y = g.bottom() - 8
        for t in reversed(self.items):
            t.adjustSize()
            y -= t.height()
            t.move(g.right() - t.width() - 8, y)
            y += 10
