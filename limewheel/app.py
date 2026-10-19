"""Tray application: wires hooks -> wheel -> operations."""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QPointF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import (QAction, QPen, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QMessageBox, QSlider, QSystemTrayIcon, QWidget)

from . import settings as settings_mod
from .brand import B, ID as BRAND_ID, NAME
from .floatwin import FloatWindow
from .hooks import Hooks, parse_hotkey
from .ops import Op, OpError
from .ui import STYLESHEET, Panel, Toasts
from .wheel import MODES, Wheel

APP_NAME = NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def log(*a):
    if os.environ.get("LIMEWHEEL_DEBUG"):
        print("[debug]", *a, flush=True)


def custom_icon_path() -> Path | None:
    """A user-supplied icon.png (settings folder, or next to the exe) overrides the built-in mark."""
    for p in (settings_mod.DIR / "icon.png", Path(sys.executable).parent / "icon.png"):
        if p.exists() and (p.parent != Path(sys.executable).parent or getattr(sys, "frozen", False)):
            return p
    return None


def lime_pixmap(size: int) -> QPixmap:
    """Limewheel mark: the app's own wheel — six glossy lime petals around a hollow hub with a pointer."""
    from .wheel import petal_path
    S = 512
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    def glossy(path, light, mid, deep, edge_w=5):
        br = path.boundingRect()
        g = QLinearGradient(br.topLeft(), br.bottomRight())
        g.setColorAt(0, QColor(light))
        g.setColorAt(0.5, QColor(mid))
        g.setColorAt(1, QColor(deep))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(path)
        sheen = QLinearGradient(br.topLeft(), QPointF(br.left(), br.top() + br.height() * 0.55))
        sheen.setColorAt(0, QColor(255, 255, 255, 120))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawPath(path)
        edge = QLinearGradient(br.topLeft(), br.bottomRight())
        edge.setColorAt(0, QColor(255, 255, 255, 230))
        edge.setColorAt(1, QColor(255, 255, 255, 40))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(edge, edge_w))
        p.drawPath(path)

    c = QPointF(S / 2, S / 2)
    disc = QRadialGradient(c, 236)
    disc.setColorAt(0.7, QColor(205, 240, 150))
    disc.setColorAt(1.0, QColor(150, 205, 80))
    p.setPen(Qt.NoPen)
    p.setBrush(disc)
    p.drawEllipse(c, 236, 236)
    for k in range(6):
        a0 = k * 60 - 30
        glossy(petal_path(c, 92, 220, a0, a0 + 60, 16, ro=34, ri=16), "#C8F26A", "#86C93A", "#4E9A12")
    hub = QPainterPath()
    hub.addEllipse(c, 70, 70)
    glossy(hub, "#FFFFFF", "#EEF9DA", "#C9E7A2")
    arrow = QPainterPath()
    ax, ay = c.x() - 18, c.y() - 30
    pts = [(0, 0), (0, 70), (17, 54), (30, 82), (43, 76), (30, 49), (54, 49)]
    arrow.moveTo(QPointF(ax, ay))
    for dx, dy in pts[1:]:
        arrow.lineTo(QPointF(ax + dx, ay + dy))
    arrow.closeSubpath()
    p.setBrush(QColor("#1D3A08"))
    p.setPen(QPen(QColor("#FFFFFF"), 5))
    p.drawPath(arrow)
    p.end()
    return pm if size == S else pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def make_icon(size: int = 64) -> QIcon:
    """Built-in mark for the active brand, unless the user supplied their own icon.png."""
    custom = custom_icon_path()
    if custom:
        icon = QIcon(str(custom))
        if not icon.isNull():
            return icon
    return QIcon(lime_pixmap(max(size, 256)))


def explorer_selection() -> list[Path]:
    """Files selected in the foreground Explorer window."""
    try:
        import win32com.client
        import win32gui
    except ImportError:
        return []
    fg = win32gui.GetForegroundWindow()
    shell = win32com.client.Dispatch("Shell.Application")
    found: list[list[Path]] = []
    for w in shell.Windows():
        try:
            if int(w.HWND) != fg:
                continue
            items = [Path(i.Path) for i in w.Document.SelectedItems()]
            if items:
                found.append(items)
        except Exception:
            continue
    return found[0] if found else []


class _JobSignals(QObject):
    done = Signal(object, list)    # op, outputs
    failed = Signal(object, str)


class Job(QRunnable):
    def __init__(self, fn, op):
        super().__init__()
        self.fn, self.op, self.signals = fn, op, _JobSignals()

    def run(self):
        try:
            self.signals.done.emit(self.op, self.fn())
        except OpError as e:
            self.signals.failed.emit(self.op, str(e))
        except Exception as e:
            traceback.print_exc()
            self.signals.failed.emit(self.op, f"{type(e).__name__}: {e}")


class Ctx:
    def __init__(self, app: "LimewheelApp"):
        self.app = app
        self.settings = app.settings


def execute(op: Op, paths: list[Path], opts: dict, ctx: Ctx) -> list[Path]:
    results = [op.run(paths, opts, ctx)] if op.batch else [op.run(p, opts, ctx) for p in paths]
    out: list[Path] = []
    for r in results:
        if r is None:
            continue
        out.extend(r if isinstance(r, list) else [r])
    return out


class SettingsDialog(Panel):
    def __init__(self, data: dict, startup: bool):
        super().__init__("Settings", apply_text="Save", width=460)
        self.setWindowIcon(make_icon())
        self.apply_btn.clicked.disconnect()
        self.apply_btn.clicked.connect(self._check)

        def section(text):
            l = QLabel(text)
            l.setProperty("role", "section")
            return l

        def form():
            f = QFormLayout()
            f.setSpacing(10)
            f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            holder = QWidget()
            holder.setLayout(f)
            return f, holder

        b = self.body_layout
        b.addWidget(section("Wheel keys"))
        f, h = form()
        self.combos = {}
        for mode, title in MODES:
            cb = QComboBox()
            cb.addItems(["Shift", "Ctrl", "Alt"])
            cb.setCurrentText(data["keys"].get(mode, "shift").capitalize())
            cb.setAccessibleName(f"{title} wheel key")
            f.addRow(QLabel(title), cb)
            self.combos[mode] = cb
        b.addWidget(h)
        self.key_error = QLabel("Each wheel needs a different key.")
        self.key_error.setProperty("role", "section")
        self.key_error.hide()
        b.addWidget(self.key_error)

        b.addWidget(section("Without dragging"))
        f, h = form()
        self.hotkey = QLineEdit(data.get("hotkey", ""))
        self.hotkey.setPlaceholderText("ctrl+alt+t")
        f.addRow(QLabel("Selection hotkey"), self.hotkey)
        b.addWidget(h)
        self.hotkey_error = QLabel("Use a form like ctrl+alt+t (modifiers + one key).")
        self.hotkey_error.setProperty("role", "section")
        self.hotkey_error.hide()
        b.addWidget(self.hotkey_error)

        b.addWidget(section("General"))
        self.explorer = QCheckBox("Only for drags that start in Explorer or on the Desktop")
        self.explorer.setChecked(data.get("explorer_only", True))
        b.addWidget(self.explorer)
        self.hold = QCheckBox("Touch or pen: hold, then drag to open the Convert wheel")
        self.hold.setChecked(data.get("hold_to_convert", True))
        b.addWidget(self.hold)
        self.hold_mouse = QCheckBox("Mouse too: hold still for half a second, then drag")
        self.hold_mouse.setChecked(data.get("hold_with_mouse", False))
        b.addWidget(self.hold_mouse)
        self.reveal = QCheckBox("Open the folder when a job finishes")
        self.reveal.setChecked(data.get("reveal_when_done", False))
        b.addWidget(self.reveal)
        self.startup = QCheckBox(f"Start {NAME} when I sign in")
        self.startup.setChecked(startup)
        b.addWidget(self.startup)
        q_row = QWidget()
        ql = QHBoxLayout(q_row)
        ql.setContentsMargins(0, 0, 0, 0)
        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(10, 100)
        self.quality.setValue(int(data.get("image_quality", 85)))
        self.quality.setAccessibleName("Image quality")
        q_val = QLabel(f"{self.quality.value()}")
        q_val.setMinimumWidth(32)
        q_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.quality.valueChanged.connect(lambda v: q_val.setText(str(v)))
        ql.addWidget(QLabel("Image quality"))
        ql.addWidget(self.quality, 1)
        ql.addWidget(q_val)
        b.addWidget(q_row)
        hint = QLabel("Drag a file, tap a key, then drop it on an option.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        b.addWidget(hint)

    def _check(self):
        keys = [c.currentText() for c in self.combos.values()]
        bad_keys = len(set(keys)) != len(keys)
        bad_hotkey = bool(self.hotkey.text().strip()) and not parse_hotkey(self.hotkey.text())
        self.key_error.setVisible(bad_keys)
        self.hotkey_error.setVisible(bad_hotkey)
        if bad_hotkey:
            self.hotkey.setFocus()
        if not (bad_keys or bad_hotkey):
            self.accept()
        self.adjustSize()

    def values(self) -> dict:
        return {"keys": {m: c.currentText().lower() for m, c in self.combos.items()},
                "hotkey": self.hotkey.text().strip(), "explorer_only": self.explorer.isChecked(),
                "hold_to_convert": self.hold.isChecked(), "hold_with_mouse": self.hold_mouse.isChecked(),
                "reveal_when_done": self.reveal.isChecked(),
                "image_quality": self.quality.value()}


class LimewheelApp(QObject):
    def __init__(self, qapp: QApplication, oneshot: list[Path] | None = None):
        super().__init__()
        self.qapp = qapp
        self.settings = settings_mod.load()
        self.ctx = Ctx(self)
        self.pool = QThreadPool.globalInstance()
        self.floats: list[FloatWindow] = []
        self.active_jobs = 0
        self._jobs: set[Job] = set()
        self.oneshot = oneshot is not None
        self._last_output: Path | None = None

        self.wheel = Wheel(self.settings)
        self.wheel.run_requested.connect(self.run_op)
        self.toasts = Toasts()

        self.tray = QSystemTrayIcon(make_icon(), qapp)
        self.tray.setToolTip(f"{NAME} — drag a file and tap Shift / Ctrl / Alt")
        self.tray.messageClicked.connect(self._reveal_last)
        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{ background: {B['menu_bg']}; border: 1px solid {B['hairline']}; border-radius: 10px; padding: 6px; }}
            QMenu::item {{ color: {B['ink']}; padding: 7px 22px; border-radius: 6px; font-size: 10pt; }}
            QMenu::item:selected {{ background: {B['accent']}; color: white; }}
            QMenu::separator {{ height: 1px; background: {B['hairline']}; margin: 5px 8px; }}""")
        sel = QAction("Open wheel for Explorer selection", menu)
        sel.triggered.connect(self.on_hotkey)
        menu.addAction(sel)
        menu.addSeparator()
        st = QAction("Settings…", menu)
        st.triggered.connect(self.open_settings)
        menu.addAction(st)
        quit_ = QAction(f"Quit {NAME}", menu)
        quit_.triggered.connect(self.quit)
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.show()

        self.hooks = None
        if self.oneshot:
            self.wheel.visibility_changed.connect(self._oneshot_maybe_quit)
            self.wheel.open_keyboard(oneshot, "convert")
        else:
            self.hooks = Hooks(self.settings)
            self.hooks.signals.modifier.connect(self.on_modifier)
            self.hooks.signals.mouse_up.connect(self.on_mouse_up)
            self.hooks.signals.escape.connect(self.wheel.close_wheel)
            self.hooks.signals.hotkey.connect(self.on_hotkey)
            self.wheel.visibility_changed.connect(lambda v: setattr(self.hooks, "wheel_visible", v))
            self.hooks.start()
            keys = self.settings["keys"]
            hk = self.settings.get("hotkey", "").upper() if self.hooks.hotkey_ok else None
            msg = (f"Drag a file and tap {keys['convert'].capitalize()}, {keys['tools'].capitalize()} or "
                   f"{keys['actions'].capitalize()}." + (f" Or select files and press {hk}." if hk else ""))
            t = self.toasts.show(f"{NAME} is ready", msg)
            t.finish(f"{NAME} is ready", msg)

    # --------------------------------------------------------- triggers
    def mode_for_key(self, key: str) -> str | None:
        for mode, k in self.settings["keys"].items():
            if k == key:
                return mode
        return None

    def on_modifier(self, key: str, dragging: bool):
        log("modifier", key, "dragging=", dragging, "visible=", self.wheel.isVisible())
        mode = self.mode_for_key(key)
        if not mode:
            return
        if self.wheel.isVisible():
            self.wheel.set_mode(mode)
        elif dragging:
            self.wheel.open_drag(mode)

    def on_mouse_up(self):
        log("mouse up")
        if self.wheel.isVisible() and not self.wheel.keyboard:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(450, self.wheel.hide_if_idle)

    def on_hotkey(self):
        paths = explorer_selection()
        if not paths:
            t = self.toasts.show("Nothing selected", "")
            t.finish("Nothing selected", "Select files in an Explorer window, then press the hotkey again.")
            return
        self.wheel.open_keyboard(paths, "convert")

    # ------------------------------------------------------- operations
    GERUND = {"Compress": "Compressing", "Trim": "Trimming", "Split": "Splitting", "Merge": "Merging",
              "Resize": "Resizing", "Rotate 90°": "Rotating", "Flip": "Flipping", "Mute": "Muting",
              "Grayscale": "Converting to grayscale", "Strip metadata": "Removing metadata",
              "Crop square": "Cropping", "Snapshot": "Taking a snapshot", "Normalize": "Normalizing",
              "Extract images": "Extracting images", "Split pages": "Splitting pages", "Copy to": "Copying",
              "Move to": "Moving", "Extract": "Extracting", "Merge to PDF": "Merging to PDF",
              "Make GIF": "Making a GIF", "Resize 720p": "Resizing to 720p"}

    @staticmethod
    def _subject(paths: list[Path]) -> str:
        return paths[0].name if len(paths) == 1 else f"{len(paths)} files"

    def _titles(self, op: Op) -> tuple[str, str]:
        label = op.label.rstrip("…")
        if op.mode == "convert" and op.badge and label != "Extract":
            target = label[3:] if label.startswith("to ") else op.badge
            return f"Converting to {target}", f"Converted to {target}"
        return f"{self.GERUND.get(label, label)}…", f"{label} — done"

    def run_op(self, op: Op, paths: list[Path]):
        log("run", op.id, paths)
        working, _ = self._titles(op)
        try:
            opts = {}
            if op.prompt:
                opts = op.prompt(paths)
                if opts is None:
                    return self._oneshot_done()
            if op.main_thread:
                toast = self.toasts.show(working, self._subject(paths), self._reveal) if op.mode != "actions" else None
                self.qapp.processEvents()
                self._done(op, execute(op, paths, opts, self.ctx), toast)
                return
        except OpError as e:
            return self._failed(op, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._failed(op, f"{type(e).__name__}: {e}")
        toast = self.toasts.show(working, self._subject(paths), self._reveal)
        job = Job(lambda: execute(op, paths, opts, self.ctx), op)
        job.setAutoDelete(False)
        self._jobs.add(job)

        def finished(*_):
            self._jobs.discard(job)
            self.active_jobs = max(0, self.active_jobs - 1)
            self.tray.setToolTip(NAME + (f" — working ({self.active_jobs})…" if self.active_jobs else ""))

        job.signals.done.connect(lambda o, outs: (finished(), self._done(o, outs, toast)))
        job.signals.failed.connect(lambda o, msg: (finished(), self._failed(o, msg, toast)))
        self.active_jobs += 1
        self.tray.setToolTip(f"{NAME} — working ({self.active_jobs})…")
        self.pool.start(job)

    def _done(self, op: Op, outputs: list, toast=None):
        _, done = self._titles(op)
        if outputs:
            self._last_output = Path(outputs[0])
            names = ", ".join(Path(o).name for o in outputs[:3]) + (" …" if len(outputs) > 3 else "")
            if toast is None:
                toast = self.toasts.show(done, names, self._reveal)
            toast.finish(done, names, Path(outputs[0]))
            if self.settings.get("reveal_when_done"):
                self._reveal(outputs[0])
        elif op.id == "act.path":
            t = self.toasts.show("Path copied", "Paste it anywhere with Ctrl+V.")
            t.finish("Path copied", "Paste it anywhere with Ctrl+V.")
        elif toast is not None:
            toast.dismiss()
        print(f"[done] {op.id}: {[str(o) for o in outputs]}", flush=True)
        self._oneshot_done()

    def _failed(self, op: Op, msg: str, toast=None):
        title = f"Couldn't {op.label.rstrip('…').lower()}" if op.mode != "convert" else \
            f"Couldn't convert to {op.badge or op.label}"
        if toast is None:
            toast = self.toasts.show(title, "")
        toast.finish(title, msg[:300], error=True)
        print(f"[failed] {op.id}: {msg}", flush=True)
        self._oneshot_done()

    def _reveal(self, path):
        if path and Path(path).exists():
            subprocess.Popen(f'explorer /select,"{path}"')

    def _reveal_last(self):
        self._reveal(self._last_output)

    def float_files(self, paths: list[Path]):
        w = FloatWindow(paths, lambda win: self.floats.remove(win) if win in self.floats else None)
        from PySide6.QtGui import QCursor
        pos = QCursor.pos()
        w.move(pos.x() - 130, pos.y() - 40)
        w.show()
        self.floats.append(w)

    # --------------------------------------------------------- one-shot
    def _oneshot_maybe_quit(self, visible: bool):
        if not visible and not self.wheel.dropped:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200, lambda: self.quit() if not self.wheel.dropped else None)

    def _oneshot_done(self):
        if self.oneshot and not self.active_jobs and not self.floats:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(3500, self.quit)

    # --------------------------------------------------------- settings
    def _startup_enabled(self) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False

    def _set_startup(self, on: bool):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    pyw = Path(sys.executable).with_name("pythonw.exe")
                    main = Path(__file__).resolve().parent.parent / "main.py"
                    cmd = f'"{pyw if pyw.exists() else sys.executable}" "{main}"'
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self._startup_enabled())
        if not dlg.run():
            return
        old_hotkey = self.settings.get("hotkey")
        self.settings.update(dlg.values())
        settings_mod.save(self.settings)
        if dlg.startup.isChecked() != self._startup_enabled():
            self._set_startup(dlg.startup.isChecked())
        if self.hooks and self.settings.get("hotkey") != old_hotkey:
            self.hooks.restart()
        self.wheel.update()

    def quit(self):
        if self.hooks:
            self.hooks.stop()
        self.tray.hide()
        self.qapp.quit()


def main(argv: list[str]) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(argv[:1])
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(make_icon())

    if len(argv) >= 5 and argv[1] == "--run-op":
        # Test hook for packaged builds: --run-op <op id> <file> <result.json> [options.json]
        # Never shows a window: every failure is written to result.json.
        import json
        try:
            from .ops import REGISTRY
            op = next(o for o in REGISTRY if o.id == argv[2])
            opts = json.loads(Path(argv[5]).read_text(encoding="utf-8-sig")) if len(argv) > 5 else {}
            ctx = Ctx.__new__(Ctx)
            ctx.app, ctx.settings = None, settings_mod.load()
            outs = execute(op, [Path(argv[3]).resolve()], opts, ctx)
            res = {"ok": True, "outputs": [str(o) for o in outs]}
        except BaseException as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        Path(argv[4]).write_text(json.dumps(res), encoding="utf-8")
        return 0

    files = [Path(a).resolve() for a in argv[1:] if not a.startswith("--")]
    if files:
        app = LimewheelApp(qapp, oneshot=files)
        return qapp.exec()

    settings_mod.DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(settings_mod.DIR / "limewheel.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, f"{NAME} is already running (see the tray).")
        return 0
    app = LimewheelApp(qapp)  # noqa: F841
    return qapp.exec()
