"""Operation registry and helpers shared by all converters/tools/actions."""
from __future__ import annotations

import ctypes
import math
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Callable

MODES = ("convert", "tools", "actions")
CREATE_NO_WINDOW = 0x08000000

ALIASES = {"jpeg": "jpg", "tif": "tiff", "heif": "heic", "aif": "aiff", "htm": "html",
           "markdown": "md", "tgz": "tgz", "text": "txt", "yml": "yaml"}


class OpError(Exception):
    """A user-facing failure message."""


@dataclass
class Op:
    id: str
    label: str
    mode: str
    exts: frozenset | None          # None = anything, including folders
    run: Callable                   # each: run(path, opts, ctx); batch: run(paths, opts, ctx)
    badge: str = ""                 # big text on the wheel slice (e.g. "PNG")
    batch: bool = False
    min_files: int = 1
    prompt: Callable | None = None  # prompt(paths) -> dict | None, runs on the UI thread
    main_thread: bool = False       # needs Qt GUI objects (clipboard, windows, PDF painter)
    available: Callable[[], bool] | None = None
    skip_same: str | None = None    # hide when every input already has this extension
    exclude: frozenset | None = None  # hide when every input is one of these
    max_files: int | None = None      # editors work on one file at a time


REGISTRY: list[Op] = []


def register(*args, **kw) -> Op:
    op = Op(*args, **kw)
    REGISTRY.append(op)
    return op


def ext_of(p: Path) -> str:
    if p.is_dir():
        return "<dir>"
    name = p.name.lower()
    if name.endswith(".tar.gz"):
        return "tgz"
    e = p.suffix.lower().lstrip(".")
    return ALIASES.get(e, e)


def ops_for(paths: list[Path], mode: str) -> list[Op]:
    if not paths:
        return []
    exts = {ext_of(p) for p in paths}
    out = []
    for op in REGISTRY:
        if op.mode != mode or len(paths) < op.min_files or (op.max_files and len(paths) > op.max_files):
            continue
        if op.exts is not None and not exts <= op.exts:
            continue
        if op.skip_same and exts == {op.skip_same}:
            continue
        if op.exclude and exts <= op.exclude:
            continue
        if op.available and not op.available():
            continue
        out.append(op)
    return out


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = (path.name[:-7], ".tar.gz") if path.name.lower().endswith(".tar.gz") else (path.stem, path.suffix)
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def stem_of(p: Path) -> str:
    return p.name[:-7] if p.name.lower().endswith(".tar.gz") else p.stem


def out_path(src: Path, ext: str, suffix: str = "") -> Path:
    return unique(src.with_name(f"{stem_of(src)}{suffix}.{ext}"))


def out_dir(src: Path, suffix: str = "") -> Path:
    d = unique(src.with_name(f"{stem_of(src)}{suffix}"))
    d.mkdir(parents=True)
    return d


def run_proc(cmd: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
        raise OpError("\n".join(err[-4:]) or f"{Path(str(cmd[0])).name} failed")
    return r


def parse_time(text: str) -> float | None:
    """'1:23.5' / '83.5' / '' -> seconds (None for blank)."""
    text = text.strip()
    if not text:
        return None
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return secs


def fmt_time(secs: float) -> str:
    h, rem = divmod(max(secs, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}" if h else f"{int(m)}:{s:06.3f}"


# ---------------------------------------------------------------- UI prompts

def force_foreground(hwnd: int) -> None:
    """Bring one of our windows to the front even if another app owns focus."""
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != me:
        user32.AttachThreadInput(me, fg_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(me, fg_thread, False)
    else:
        user32.SetForegroundWindow(hwnd)


def _front(widget) -> None:
    widget.show()
    widget.raise_()
    widget.activateWindow()
    force_foreground(int(widget.winId()))


def ask_fields(title: str, fields: list[tuple[str, str]], note: str = "") -> list[str] | None:
    from ..dialogs import fields_dialog
    return fields_dialog(title, fields, note)


def ask_int(title: str, label: str, default: int, lo: int, hi: int) -> int | None:
    vals = ask_fields(title, [(label, str(default))], f"Enter a value from {lo} to {hi}.")
    if vals is None:
        return None
    try:
        return min(hi, max(lo, int(float(vals[0]))))
    except ValueError:
        raise OpError(f"'{vals[0]}' is not a number")


def ask_folder(title: str) -> Path | None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFileDialog

    from ..brand import NAME
    dlg = QFileDialog(None, f"{NAME} — {title}")
    dlg.setFileMode(QFileDialog.Directory)
    dlg.setOption(QFileDialog.ShowDirsOnly, True)
    dlg.setOption(QFileDialog.DontUseNativeDialog, True)
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    _front(dlg)
    if dlg.exec() and dlg.selectedFiles():
        return Path(dlg.selectedFiles()[0])
    return None


def chunks(n: int, parts: int) -> list[tuple[float, float]]:
    step = n / parts
    return [(i * step, step) for i in range(parts)]


@cache
def tar_exe() -> str:
    sys_tar = Path(r"C:\Windows\System32\tar.exe")
    return str(sys_tar) if sys_tar.exists() else "tar"


def ceil_div(a: int, b: int) -> int:
    return -(-a // b) if b else 0
