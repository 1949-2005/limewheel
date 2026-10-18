"""'Float' action: a small always-on-top shelf you can drag files back out of."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileInfo, QMimeData, QPoint, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFileIconProvider, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget)

from .ui import SHADOW, STYLESHEET, _Surface, close_button, ui_font

THUMB = 44


def _thumb(p: Path) -> QIcon:
    r = QImageReader(str(p))
    if r.canRead():
        s = r.size()
        if s.isValid() and s.width() and s.height():
            k = THUMB * 2 / max(s.width(), s.height())
            r.setScaledSize(QSize(max(1, int(s.width() * k)), max(1, int(s.height() * k))))
        img = r.read()
        if not img.isNull():
            return QIcon(QPixmap.fromImage(img))
    return QFileIconProvider().icon(QFileInfo(str(p)))


class _List(QListWidget):
    def mimeData(self, items):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(i.data(Qt.UserRole)) for i in items])
        return md

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction


class FloatWindow(QWidget):
    def __init__(self, paths: list[Path], on_close):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(STYLESHEET)
        self.setAccessibleName("Floating files")
        self._on_close = on_close
        self._drag: QPoint | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 18)
        surface = _Surface(self)
        sh = QGraphicsDropShadowEffect(surface)
        sh.setBlurRadius(28)
        sh.setOffset(0, 8)
        sh.setColor(QColor(*SHADOW, 80))
        surface.setGraphicsEffect(sh)
        outer.addWidget(surface)
        lay = QVBoxLayout(surface)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        close = close_button(self)
        close.clicked.connect(self.close)
        title = QLabel(f"{len(paths)} floating item{'s' if len(paths) != 1 else ''}")
        title.setFont(ui_font(10.5, QFont.DemiBold))
        head.addWidget(close)
        head.addWidget(title, 1)
        lay.addLayout(head)
        hint = QLabel("Drag items out to drop them anywhere · Ctrl+A selects all")
        hint.setProperty("role", "hint")
        lay.addWidget(hint)
        self.list = _List()
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragOnly)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for p in paths:
            it = QListWidgetItem(_thumb(p), p.name)
            it.setData(Qt.UserRole, str(p))
            it.setToolTip(str(p))
            self.list.addItem(it)
        lay.addWidget(self.list)
        self.resize(330, min(132 + (THUMB + 18) * len(paths), 460))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def closeEvent(self, e):
        self._on_close(self)
        super().closeEvent(e)
