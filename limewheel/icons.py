"""Line icons (24px grid, 1.8 stroke) rendered from inline SVG, tinted on demand."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_P = {
    "compress": '<path d="M4 14h6v6M3 21l7-7M20 10h-6V4M21 3l-7 7"/>',
    "expand": '<path d="M15 3h6v6M21 3l-7 7M9 21H3v-6M3 21l7-7"/>',
    "rotate": '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M21 3v5h-5"/>',
    "flip": '<path d="M12 3v18"/><path d="M8.5 7 3 12l5.5 5z"/><path d="M15.5 7 21 12l-5.5 5z"/>',
    "crop": '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/>',
    "contrast": '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="C"/>',
    "tag-off": '<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 '
               '3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1.2" fill="C"/><path d="M3 21 21 3"/>',
    "merge": '<rect x="3" y="3" width="11" height="13" rx="2"/><rect x="10" y="8" width="11" height="13" rx="2"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "film": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/>',
    "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>'
                '<path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
    "split": '<rect x="2.5" y="5" width="8" height="14" rx="2"/><rect x="13.5" y="5" width="8" height="14" rx="2"/>',
    "mute": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z"/>'
              '<circle cx="12" cy="13" r="3.2"/>',
    "wave": '<path d="M2 12h2M6 8v8M10 4v16M14 7v10M18 10v4M22 12h-2"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    "pages": '<path d="M15 2H8a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M4 7v13a2 2 0 0 0 2 2h9"/>',
    "pin": '<path d="M12 17v5"/><path d="M9 3h6l-1 6 4 4v2H6v-2l4-4z"/>',
    "copy": '<rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2"/>',
    "move": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 '
            '2v13a2 2 0 0 0 2 2z"/><path d="m12 10 3 3-3 3M8 13h7"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
            '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "terminal": '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 '
              '0-2 2v13a2 2 0 0 0 2 2z"/><circle cx="12" cy="13" r="2.5"/>',
    "package": '<path d="m21 8-9-5-9 5v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    "table": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    "sparkle": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="M4 12.5 9 17.5 20 6.5"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><circle cx="12" cy="16.5" r="0.6" fill="C"/>',
}

# Operation id -> icon name (tools & actions; conversions show their format badge).
OP_ICONS = {
    "img.compress": "compress", "img.resize": "expand", "img.rotate": "rotate", "img.flip": "flip",
    "img.square": "crop", "img.gray": "contrast", "img.strip": "tag-off", "img.mergepdf": "merge",
    "img.gif": "film",
    "vid.compress": "compress", "vid.trim": "scissors", "vid.split": "split", "vid.merge": "merge",
    "vid.mute": "mute", "vid.720": "monitor", "vid.rotate": "rotate", "vid.frame": "camera",
    "aud.compress": "compress", "aud.trim": "scissors", "aud.split": "split", "aud.merge": "merge",
    "aud.normalize": "wave",
    "pdf.merge": "merge", "pdf.split": "pages", "pdf.compress": "compress", "pdf.rotate": "rotate",
    "pdf.images": "image",
    "act.float": "pin", "act.copy": "copy", "act.move": "move", "act.path": "link",
    "act.terminal": "terminal", "act.reveal": "folder",
    "arc.extract": "package",
}


def _svg(name: str, color: str, stroke: float) -> bytes:
    body = _P[name].replace('"C"', f'"{color}"')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode()


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int, dpr: float = 1.0, stroke: float = 1.8) -> QPixmap:
    px = int(size * dpr)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    r = QSvgRenderer(QByteArray(_svg(name, color, stroke)))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, px, px))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def for_op(op) -> str | None:
    return OP_ICONS.get(op.id)
