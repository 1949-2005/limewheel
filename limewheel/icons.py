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
    "sliders": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "eye-off": '<path d="M10.7 5.1A9.8 9.8 0 0 1 12 5c5 0 8.5 4.6 9.6 6.4a1.2 1.2 0 0 1 0 1.2 17 17 0 0 1-2.4 3.1M6.6 6.6A16.6 16.6 0 0 0 2.4 11.4a1.2 1.2 0 0 0 0 1.2C3.5 14.4 7 19 12 19a9.6 9.6 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2M3 3l18 18"/>',
    "pen": '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/><path d="M14 6l3 3"/>',
    # file categories (centre of the wheel for files without a picture preview)
    "cat-video": '<rect x="2.5" y="5" width="14" height="14" rx="2.5"/><path d="m16.5 10 5-3v10l-5-3"/>',
    "cat-audio": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "cat-pdf": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 17v-4h1.5a1.3 1.3 0 0 1 0 2.6H8M13 13v4h1a2 2 0 0 0 0-4z"/>',
    "cat-doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h6M8 9h2"/>',
    "cat-slides": '<rect x="2.5" y="3.5" width="19" height="13" rx="2"/><path d="M12 16.5V21M8 21h8M7 12l3-3 2 2 4-4"/>',
    "cat-sheet": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 9v12M15 9v12"/>',
    "cat-book": '<path d="M4 19.5V5a2.5 2.5 0 0 1 2.5-2.5H20v17H6.5A2.5 2.5 0 0 0 4 22"/><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>',
    "cat-subs": '<rect x="2.5" y="4.5" width="19" height="15" rx="2.5"/><path d="M7 15h4M13 15h4M7 11h2M11 11h6"/>',
    "cat-archive": '<rect x="2.5" y="3" width="19" height="5" rx="1.5"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8M10 12h4"/>',
    "cat-folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
    "frame": '<rect x="2.5" y="2.5" width="19" height="19" rx="3"/><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="m7 14.5 3-3 2.5 2.5 1.5-1.5 3 3"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    "gauge": '<path d="M12 14l4-4"/><path d="M3.5 18a9.5 9.5 0 1 1 17 0z"/>',
    "headphones": '<path d="M3 18v-6a9 9 0 0 1 18 0v6"/><rect x="2.5" y="14" width="5" height="7" rx="2"/><rect x="16.5" y="14" width="5" height="7" rx="2"/>',
    "bleep": '<path d="M2 12h2M6 9v6M10 6v12M14 9v6"/><path d="m17 9 5 6M22 9l-5 6"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="C"/>',
    "tag": '<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1.2" fill="C"/>',
    "grid": '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
    "qr": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM18 18h3v3h-3zM14 20v1M20 14h1"/>',
    "reorder": '<rect x="3" y="4" width="7" height="9" rx="1.5"/><rect x="14" y="11" width="7" height="9" rx="1.5"/><path d="M14 6h4a2 2 0 0 1 2 2v1M10 18H6a2 2 0 0 1-2-2v-1"/><path d="m18 7 2 2 2-2M6 17l-2-2-2 2"/>',
    "play": '<path d="M7 4.5v15l12.5-7.5z" fill="C"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2" fill="C"/>',
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
    "img.edit": "sliders", "img.crop": "crop", "img.redact": "eye-off", "img.annotate": "pen", "img.background": "frame",
    "vid.crop": "crop", "vid.speed": "gauge", "aud.speed": "gauge", "aud.channels": "headphones",
    "vid.channels": "headphones", "aud.bleep": "bleep", "vid.bleep": "bleep", "act.share": "share",
    "img.tosize": "target", "vid.tosize": "target", "aud.tosize": "target", "pdf.tosize": "target",
    "img.meta": "tag", "vid.meta": "tag", "aud.meta": "tag", "pdf.meta": "tag", "pdf.organize": "reorder",
    "img.collage": "grid", "img.qr": "qr", "pdf.qr": "qr",
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


_CATEGORY = {
    "cat-video": "mp4 mov mkv webm avi wmv m4v flv mpg mpeg 3gp ts",
    "cat-audio": "mp3 m4a wav flac ogg opus aiff aif wma aac",
    "cat-pdf": "pdf",
    "cat-doc": "docx doc odt rtf txt md markdown html htm",
    "cat-slides": "pptx ppt odp key",
    "cat-sheet": "xlsx xls ods csv tsv numbers",
    "cat-book": "epub mobi",
    "cat-subs": "srt vtt ass ssa",
    "cat-archive": "zip rar 7z tar gz tgz bz2 xz",
}
_EXT_CATEGORY = {e: name for name, exts in _CATEGORY.items() for e in exts.split()}


def for_file(ext: str, is_dir: bool = False) -> str:
    """Category glyph for a file type (images get a real thumbnail instead)."""
    return "cat-folder" if is_dir else _EXT_CATEGORY.get(ext, "file")
