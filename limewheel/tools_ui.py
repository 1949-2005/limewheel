"""Panels for the 1.2 tools: Compress to size, Metadata, Reorganize PDF, Collage, QR results."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPixmap, QTransform
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSlider, QTextEdit,
                               QWidget)

from .dialogs import Segmented, _label, _row
from .ui import ACCENT, Panel


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# ------------------------------------------------------------ Compress to size

def target_size_dialog(current: int, kind: str, count: int = 1) -> dict | None:
    dlg = Panel("Compress to Size", apply_text="Compress", width=460)
    dlg.body_layout.addWidget(_label(f"Current size: {human(current)}" + (f"  (first of {count} files)" if count > 1 else ""),
                                     "hint"))
    presets = [("25%", current * 0.25), ("50%", current * 0.5), ("1 MB", 1 << 20), ("5 MB", 5 << 20),
               ("10 MB", 10 << 20), ("25 MB", 25 << 20)]
    presets = [(n, v) for n, v in presets if v < current] or presets[:2]
    seg = Segmented([n for n, _ in presets], 0)
    dlg.body_layout.addWidget(seg)
    spin = QDoubleSpinBox()
    spin.setDecimals(1)
    spin.setRange(1, 1_000_000)
    unit = QComboBox()
    unit.addItems(["KB", "MB"])
    spin.setAccessibleName("Target size")

    def set_bytes(b):
        if b >= 1 << 20:
            unit.setCurrentText("MB")
            spin.setValue(round(b / (1 << 20), 1))
        else:
            unit.setCurrentText("KB")
            spin.setValue(round(b / 1024, 1))

    seg.changed.connect(lambda i: set_bytes(presets[i][1]))
    set_bytes(presets[0][1])
    dlg.body_layout.addWidget(_row(_label("Target"), spin, unit, stretch_at=None))
    note = {"image": "Images keep their format; quality, then size, is reduced until the file fits.",
            "video": "Video is re-encoded to MP4 at the bitrate that fits (two passes for accuracy).",
            "audio": "Audio is re-encoded at the bitrate that fits.",
            "pdf": "Pictures inside the PDF are recompressed step by step until it fits; text stays sharp."}[kind]
    n = _label(note, "hint")
    n.setWordWrap(True)
    dlg.body_layout.addWidget(n)
    spin.setFocus()
    if not dlg.run():
        return None
    mult = (1 << 20) if unit.currentText() == "MB" else 1024
    return {"bytes": int(spin.value() * mult)}


# ------------------------------------------------------------ Metadata

def metadata_dialog(title: str, info: list[tuple[str, str]], fields: list[tuple[str, str, str]],
                    extra_checks: list[tuple[str, str, bool]] = ()) -> dict | None:
    """info: read-only (label, value). fields: (key, label, value) editable. extra_checks: (key, label, default)."""
    dlg = Panel(title, apply_text="Save Copy", width=560)
    if info:
        dlg.body_layout.addWidget(_label("Details", "section"))
        box = QTextEdit()
        box.setReadOnly(True)
        box.setMinimumHeight(150)
        box.setStyleSheet("QTextEdit { background: rgba(255,255,255,0.45); border-radius: 8px; padding: 6px; }")
        box.setPlainText("\n".join(f"{k}: {v}" for k, v in info))
        box.setAccessibleName("Metadata details")
        dlg.body_layout.addWidget(box)
    edits = {}
    if fields:
        dlg.body_layout.addWidget(_label("Edit", "section"))
        form = QFormLayout()
        form.setSpacing(8)
        for key, label, value in fields:
            e = QLineEdit(value or "")
            e.setAccessibleName(label)
            form.addRow(_label(label), e)
            edits[key] = e
        holder = QWidget()
        holder.setLayout(form)
        dlg.body_layout.addWidget(holder)
    checks = {}
    for key, label, default in extra_checks:
        c = QCheckBox(label)
        c.setChecked(default)
        checks[key] = c
        dlg.body_layout.addWidget(c)
    strip = QCheckBox("Remove all metadata instead")
    dlg.body_layout.addWidget(strip)
    strip.toggled.connect(lambda on: [e.setEnabled(not on) for e in edits.values()])
    if not dlg.run():
        return None
    return {"fields": {k: e.text().strip() for k, e in edits.items()},
            "checks": {k: c.isChecked() for k, c in checks.items()}, "strip": strip.isChecked()}


# ------------------------------------------------------------ Reorganize PDF

def organize_dialog(thumbs: list[QPixmap]) -> dict | None:
    dlg = Panel("Reorganize PDF", apply_text="Save Copy", width=860)
    lst = QListWidget()
    lst.setViewMode(QListWidget.IconMode)
    lst.setIconSize(QSize(120, 160))
    lst.setGridSize(QSize(150, 200))
    lst.setResizeMode(QListWidget.Adjust)
    lst.setMovement(QListWidget.Snap)
    lst.setDragDropMode(QAbstractItemView.InternalMove)
    lst.setDefaultDropAction(Qt.MoveAction)
    lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
    lst.setMinimumHeight(430)
    lst.setAccessibleName("Pages")
    lst.setStyleSheet(f"QListWidget::item {{ background: rgba(255,255,255,0.35); border-radius: 10px; margin: 4px; }}"
                      f"QListWidget::item:selected {{ background: {ACCENT}; color: white; }}")

    def refresh(item):
        rot = item.data(Qt.UserRole + 1)
        pm = item.data(Qt.UserRole + 2).transformed(QTransform().rotate(rot), Qt.SmoothTransformation)
        item.setIcon(QIcon(pm))

    def fill():
        lst.clear()
        for i, pm in enumerate(thumbs):
            it = QListWidgetItem(f"Page {i + 1}")
            it.setData(Qt.UserRole, i)
            it.setData(Qt.UserRole + 1, 0)
            it.setData(Qt.UserRole + 2, pm)
            it.setTextAlignment(Qt.AlignHCenter)
            refresh(it)
            lst.addItem(it)

    fill()
    hint = _label("Drag pages to reorder. Select pages (Ctrl/Shift-click) to rotate or delete them.", "hint")
    dlg.body_layout.addWidget(hint)
    dlg.body_layout.addWidget(lst, 1)

    def rotate(delta):
        for it in lst.selectedItems():
            it.setData(Qt.UserRole + 1, (it.data(Qt.UserRole + 1) + delta) % 360)
            refresh(it)

    def delete():
        for it in lst.selectedItems():
            lst.takeItem(lst.row(it))

    def reverse():
        items = [lst.takeItem(0) for _ in range(lst.count())]
        for it in reversed(items):
            lst.addItem(it)

    buttons = []
    for text, fn in (("Rotate left", lambda: rotate(-90)), ("Rotate right", lambda: rotate(90)),
                     ("Delete", delete), ("Reverse order", reverse), ("Reset", fill)):
        b = QPushButton(text)
        b.clicked.connect(fn)
        buttons.append(b)
    dlg.body_layout.addWidget(_row(*buttons, stretch_at=len(buttons)))
    from PySide6.QtGui import QKeySequence, QShortcut
    QShortcut(QKeySequence.Delete, lst, activated=delete)
    if not dlg.run():
        return None
    order = [[lst.item(r).data(Qt.UserRole), lst.item(r).data(Qt.UserRole + 1)] for r in range(lst.count())]
    if not order:
        return None
    return {"order": order}


# ------------------------------------------------------------ Collage

LAYOUTS = ["Grid", "Row", "Column"]
BACKS = [("White", "#FFFFFF"), ("Black", "#111111"), ("Accent", None), ("Transparent", "transparent")]
WIDTHS = [1080, 2048, 4096]


def collage_dialog(images, compose) -> dict | None:
    """images: list of PIL images (already loaded); compose(images, opts, width) -> PIL image."""
    from .editors import ImageView, pil_to_qimage
    dlg = Panel("Collage", apply_text="Create Collage", width=820)
    view = ImageView(360)
    dlg.body_layout.addWidget(view, 1)
    layout = Segmented(LAYOUTS, 0)
    back = Segmented([n for n, _ in BACKS], 0)
    gap = QSlider(Qt.Horizontal)
    gap.setRange(0, 80)
    gap.setValue(16)
    gap.setAccessibleName("Spacing")
    rounded = QCheckBox("Rounded corners")
    size = QComboBox()
    size.addItems([f"{w} px wide" for w in WIDTHS])
    size.setCurrentIndex(1)
    g = QGridLayout()
    g.addWidget(_label("Layout"), 0, 0)
    g.addWidget(layout, 0, 1)
    g.addWidget(_label("Background"), 1, 0)
    g.addWidget(back, 1, 1)
    g.addWidget(_label("Spacing"), 2, 0)
    g.addWidget(gap, 2, 1)
    g.addWidget(_label("Size"), 3, 0)
    g.addWidget(_row(size, rounded, stretch_at=None), 3, 1)
    holder = QWidget()
    holder.setLayout(g)
    dlg.body_layout.addWidget(holder)
    small = []
    for im in images:
        s = im.copy()
        s.thumbnail((500, 500))
        small.append(s)

    def opts():
        bname, bval = BACKS[back.index()]
        return {"layout": LAYOUTS[layout.index()], "gap": gap.value(), "rounded": rounded.isChecked(),
                "background": ACCENT if bval is None else bval, "width": WIDTHS[size.currentIndex()]}

    timer = QTimer(dlg)
    timer.setSingleShot(True)

    def preview():
        o = opts()
        k = 640 / o["width"]
        p = dict(o, gap=max(0, round(o["gap"] * k)))
        view.set_image(pil_to_qimage(compose(small, p, 640)))

    timer.timeout.connect(preview)
    for sig in (layout.changed, back.changed, gap.valueChanged, rounded.toggled, size.currentIndexChanged):
        sig.connect(lambda *_: timer.start(60))
    preview()
    if not dlg.run():
        return None
    return opts()


# ------------------------------------------------------------ QR results

def codes_dialog(results: list[tuple[str, str]], source: str):
    dlg = Panel("QR & Barcodes", apply_text="Done", width=520)
    dlg.body_layout.addWidget(_label(f"{len(results)} found in {source}", "hint"))
    for fmt, text in results:
        dlg.body_layout.addWidget(_label(fmt, "section"))
        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        box.setMaximumHeight(90)
        box.setAccessibleName(f"{fmt} content")
        box.setStyleSheet("QTextEdit { background: rgba(255,255,255,0.5); border-radius: 8px; padding: 6px; }")
        dlg.body_layout.addWidget(box)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda _=False, t=text, b=copy: (QGuiApplication.clipboard().setText(t), b.setText("Copied")))
        row = [copy]
        if text.lower().startswith(("http://", "https://")):
            op = QPushButton("Open link")
            op.clicked.connect(lambda _=False, t=text: QDesktopServices.openUrl(QUrl(t)))
            row.append(op)
        dlg.body_layout.addWidget(_row(*row, stretch_at=len(row)))
    dlg.run()
