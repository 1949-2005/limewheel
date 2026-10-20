"""Tray application: wires hooks -> wheel -> operations."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QPointF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import (QAction, QPen, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QSlider, QSystemTrayIcon, QWidget)

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
    """Limewheel mark: the wheel's six glass petals (same tint as the app's wheel) on a transparent background."""
    from .wheel import petal_path
    S = 512
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(S / 2, S / 2)
    for k in range(6):
        a0 = k * 60 - 30
        path = petal_path(c, 92, 236, a0, a0 + 60, 18, ro=36, ri=16)
        br = path.boundingRect()
        g = QLinearGradient(br.topLeft(), br.bottomRight())
        g.setColorAt(0, QColor(B["panel_stops"][0][1]))
        g.setColorAt(1, QColor(B["panel_stops"][2][1]))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(path)
        sheen = QLinearGradient(br.topLeft(), QPointF(br.left(), br.top() + br.height() * 0.5))
        sheen.setColorAt(0, QColor(255, 255, 255, 140))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(B["hot"]), 6))
        p.drawPath(path)
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


# ------------------------------------------------------------- hand-off from Explorer
# `<exe> --mode tools --list <file>` (the right-click menu) or `<exe> <paths…>` while the app is
# already running: the new process sends the files to the running one with WM_COPYDATA and exits.
WM_COPYDATA = 0x004A
IPC_MAGIC = 0x4C57                                  # "LW"


class _COPYDATA(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", ctypes.c_uint32), ("lpData", ctypes.c_void_p)]


def packaged() -> bool:
    """True when running from the Microsoft Store (MSIX) package."""
    n = ctypes.c_uint32(0)
    try:
        return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None) != 15700  # NO_PACKAGE
    except (AttributeError, OSError):
        return False


def _ipc_title() -> str:
    return f"{NAME} IPC {os.environ.get('USERNAME', '')}"


def parse_open_args(argv: list[str]) -> tuple[str, list[Path]]:
    """--mode <convert|tools|actions>, --list <utf-8 file, one path per line>, plus plain paths."""
    mode, paths, it = "convert", [], iter(argv)
    for a in it:
        if a == "--mode":
            mode = next(it, "convert")
        elif a == "--list":
            f = Path(next(it, ""))
            try:
                paths += [Path(line.strip()) for line in f.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
                f.unlink()
            except OSError:
                pass
        elif not a.startswith("--"):
            paths.append(Path(a))
    if mode not in ("convert", "tools", "actions"):
        mode = "convert"
    return mode, [p.resolve() for p in paths if p.exists()]


def forward_to_running(mode: str, paths: list[Path]) -> bool:
    import json
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, _ipc_title())
    if not hwnd:
        return False
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    user32.AllowSetForegroundWindow(pid.value)              # so the wheel can take the keyboard focus
    data = json.dumps({"mode": mode, "paths": [str(p) for p in paths]}).encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    cds = _COPYDATA(IPC_MAGIC, len(data), ctypes.cast(buf, ctypes.c_void_p))
    res = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), WM_COPYDATA, None, ctypes.byref(cds),
                                    0x0002, 3000, ctypes.byref(res))           # SMTO_ABORTIFHUNG
    return bool(ok and res.value == 1)


class _Ipc(QWidget):
    """Hidden window that receives files from new launches. Several launches within 300 ms
    (classic menus start one process per file) are merged into one wheel."""

    def __init__(self, on_open):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint)
        self.setWindowTitle(_ipc_title())
        self.on_open, self._mode, self._paths = on_open, "convert", []
        self.winId()                                        # create the native window now (never shown)
        from PySide6.QtCore import QTimer
        self._flush = QTimer(self, singleShot=True, interval=300)
        self._flush.timeout.connect(self._fire)

    def nativeEvent(self, kind, message):
        import json
        from ctypes import wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_COPYDATA:
            cds = _COPYDATA.from_address(msg.lParam)
            if cds.dwData == IPC_MAGIC and cds.cbData:
                try:
                    d = json.loads(ctypes.string_at(cds.lpData, cds.cbData).decode("utf-8"))
                    self._mode = d.get("mode", "convert")
                    self._paths += [Path(p) for p in d.get("paths", []) if Path(p) not in self._paths]
                    self._flush.start()
                    return True, 1
                except Exception:
                    traceback.print_exc()
        return False, 0

    def _fire(self):
        paths, self._paths = self._paths, []
        if paths:
            self.on_open(self._mode, paths)


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
        self.sounds = QCheckBox("Play a soft tick when a petal lights up")
        self.sounds.setChecked(data.get("sounds", True))
        b.addWidget(self.sounds)
        self.startup = QCheckBox(f"Start {NAME} when I sign in")
        self.startup.setChecked(startup)
        if packaged():                      # Store apps start via their StartupTask, switched in Windows
            s_row = QWidget()
            sl = QHBoxLayout(s_row)
            sl.setContentsMargins(0, 0, 0, 0)
            s_hint = QLabel("Start at sign-in is switched in Windows Settings")
            s_hint.setProperty("role", "hint")
            s_btn = QPushButton("Startup apps…")
            s_btn.clicked.connect(lambda: os.startfile("ms-settings:startupapps"))
            sl.addWidget(s_hint, 1)
            sl.addWidget(s_btn)
            b.addWidget(s_row)
        else:
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
        b.addWidget(section("Automation"))
        self.watch_rules = [dict(r) for r in data.get("watch_folders", [])]
        w_row = QWidget()
        wl = QHBoxLayout(w_row)
        wl.setContentsMargins(0, 0, 0, 0)
        self.watch_info = QLabel()
        self.watch_info.setProperty("role", "hint")
        watch_btn = QPushButton("Watch folders…")
        watch_btn.clicked.connect(self._edit_watch)
        wl.addWidget(self.watch_info, 1)
        wl.addWidget(watch_btn)
        b.addWidget(w_row)
        self._watch_text()
        hint = QLabel("Drag a file, tap a key, then drop it on an option.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        b.addWidget(hint)

    def _watch_text(self):
        n = sum(1 for r in self.watch_rules if r.get("enabled", True))
        self.watch_info.setText("No watch folders" if not self.watch_rules else
                                f"{n} watch folder{'s' if n != 1 else ''} on")

    def _edit_watch(self):
        from .tools_ui import watch_dialog
        rules = watch_dialog(self.watch_rules)
        if rules is not None:
            self.watch_rules = rules
            self._watch_text()

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
                "reveal_when_done": self.reveal.isChecked(), "sounds": self.sounds.isChecked(),
                "image_quality": self.quality.value(), "watch_folders": self.watch_rules}


class LimewheelApp(QObject):
    def __init__(self, qapp: QApplication, oneshot: list[Path] | None = None, oneshot_mode: str = "convert"):
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
        wf = QAction("Watch folders…", menu)
        wf.triggered.connect(self.open_watch)
        menu.addAction(wf)
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
        self.watcher = None
        self._watch_pool = QThreadPool(self)
        self._watch_pool.setMaxThreadCount(2)             # don't flood the PC when 50 files land at once
        self._watch_stats = {"queued": 0, "done": 0, "failed": 0, "toast": None, "last": None, "error": ""}
        if self.oneshot:
            self.wheel.visibility_changed.connect(self._oneshot_maybe_quit)
            self.wheel.open_keyboard(oneshot, oneshot_mode)
        else:
            self.hooks = Hooks(self.settings)
            self.hooks.signals.modifier.connect(self.on_modifier)
            self.hooks.signals.mouse_up.connect(self.on_mouse_up)
            self.hooks.signals.escape.connect(self.wheel.close_wheel)
            self.hooks.signals.hotkey.connect(self.on_hotkey)
            self.wheel.visibility_changed.connect(lambda v: setattr(self.hooks, "wheel_visible", v))
            self.hooks.start()
            self.ipc = _Ipc(self.open_paths)
            from .watch import Watcher
            self.watcher = Watcher(self)
            self.watcher.ready.connect(self._watch_run)
            self.watcher.set_rules(self.settings.get("watch_folders", []))
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

    def open_paths(self, mode: str, paths: list[Path]):
        """Files handed over by the Explorer menu (or a second launch)."""
        log("open", mode, paths)
        if self.wheel.isVisible():
            self.wheel.hide()
        self.wheel.open_keyboard(paths, mode)

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

    # ---------------------------------------------------- watch folders
    def _watch_run(self, rule: dict, path: Path):
        from .watch import action_label, run_rule
        s = self._watch_stats
        s["queued"] += 1
        if s["toast"] is None:
            s["toast"] = self.toasts.show("Watch folder", f"{action_label(rule['action'])}: {path.name}",
                                          self._reveal)
        job = Job(lambda: run_rule(rule, path, self.ctx), None)
        job.setAutoDelete(False)
        self._jobs.add(job)

        def finished(outs=None, err=None):
            self._jobs.discard(job)
            if err is None:
                s["done"] += 1
                if outs:
                    s["last"] = Path(outs[0])
            else:
                s["failed"] += 1
                s["error"] = f"{path.name}: {err}"
                print(f"[watch failed] {path}: {err}", flush=True)
            if s["done"] + s["failed"] >= s["queued"]:
                n, bad = s["done"], s["failed"]
                title = f"Watch folder: {n} file{'s' if n != 1 else ''} done" if n else "Watch folder"
                detail = (f"{bad} couldn't be converted — {s['error']}" if bad else
                          (s["last"].name if s["last"] else ""))
                s["toast"].finish(title, detail[:300], s["last"], error=bool(bad and not n))
                self._last_output = s["last"] or self._last_output
                self._watch_stats = {"queued": 0, "done": 0, "failed": 0, "toast": None, "last": None, "error": ""}

        job.signals.done.connect(lambda _op, outs: finished(outs))
        job.signals.failed.connect(lambda _op, msg: finished(err=msg))
        self._watch_pool.start(job)

    def open_watch(self):
        from .tools_ui import watch_dialog
        rules = watch_dialog(self.settings.get("watch_folders", []))
        if rules is None:
            return
        self.settings["watch_folders"] = rules
        settings_mod.save(self.settings)
        if self.watcher:
            self.watcher.set_rules(rules)

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
        if not packaged() and dlg.startup.isChecked() != self._startup_enabled():
            self._set_startup(dlg.startup.isChecked())
        if self.hooks and self.settings.get("hotkey") != old_hotkey:
            self.hooks.restart()
        if self.watcher:
            self.watcher.set_rules(self.settings.get("watch_folders", []))
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

    if any(not a.startswith("--") for a in argv[1:]) or "--list" in argv:
        mode, files = parse_open_args(argv[1:])
        if not files:
            return 0
        if forward_to_running(mode, files):
            return 0
        app = LimewheelApp(qapp, oneshot=files, oneshot_mode=mode)
        return qapp.exec()

    settings_mod.DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(settings_mod.DIR / "limewheel.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, f"{NAME} is already running (see the tray).")
        return 0
    app = LimewheelApp(qapp)  # noqa: F841
    return qapp.exec()
