"""File actions: work on any selection, including folders."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import ask_folder, register, unique


def _ask_dest(title):
    def prompt(paths):
        d = ask_folder(title)
        return None if d is None else {"dest": d}
    return prompt


def _copy(paths, opts, ctx):
    outs = []
    for p in paths:
        dst = unique(opts["dest"] / p.name)
        if p.is_dir():
            shutil.copytree(p, dst)
        else:
            shutil.copy2(p, dst)
        outs.append(dst)
    return outs


def _move(paths, opts, ctx):
    return [Path(shutil.move(str(p), str(unique(opts["dest"] / p.name)))) for p in paths]


def _copy_path(paths, opts, ctx):
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText("\n".join(f'"{p}"' if " " in str(p) else str(p) for p in paths))
    return []


def _terminal(paths, opts, ctx):
    p = paths[0]
    folder = p if p.is_dir() else p.parent
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-d", str(folder)])
    else:
        subprocess.Popen(["powershell.exe", "-NoExit"], cwd=folder, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return []


def _reveal(paths, opts, ctx):
    subprocess.Popen(f'explorer /select,"{paths[0]}"')
    return []


def _share(paths, opts, ctx):
    """Open Windows' own share sheet (Nearby Share, Mail, Phone Link...) — the AirDrop equivalent."""
    import pythoncom
    import win32gui
    from win32comext.shell import shell, shellcon
    from .base import OpError
    pythoncom.CoInitialize()
    parent = paths[0].parent
    if any(p.parent != parent for p in paths):
        raise OpError("Share works on files from one folder at a time")
    desk = shell.SHGetDesktopFolder()
    folder = desk.BindToObject(desk.ParseDisplayName(0, None, str(parent))[1], None, shell.IID_IShellFolder)
    children = [folder.ParseDisplayName(0, None, p.name)[1] for p in paths]
    hwnd = win32gui.GetForegroundWindow()
    cm = folder.GetUIObjectOf(hwnd, children, shell.IID_IContextMenu, 0)[1]
    menu = win32gui.CreatePopupMenu()
    try:
        cm.QueryContextMenu(menu, 0, 1, 0x7FFF, shellcon.CMF_NORMAL)
        cm.InvokeCommand((0, hwnd, "Windows.ModernShare", None, None, 1, 0, None))
    except Exception as e:
        raise OpError(f"Windows couldn't open the share sheet ({e})")
    finally:
        win32gui.DestroyMenu(menu)
    return []


def _float(paths, opts, ctx):
    ctx.app.float_files(paths)
    return []


register("act.share", "Share", "actions", None, _share, batch=True, main_thread=True,
         exclude=frozenset({"<dir>"}))
register("act.float", "Float", "actions", None, _float, batch=True, main_thread=True)
register("act.copy", "Copy to…", "actions", None, _copy, batch=True, prompt=_ask_dest("Copy to"))
register("act.move", "Move to…", "actions", None, _move, batch=True, prompt=_ask_dest("Move to"))
register("act.path", "Copy Path", "actions", None, _copy_path, batch=True, main_thread=True)
register("act.terminal", "Terminal", "actions", None, _terminal, batch=True, main_thread=True)
register("act.reveal", "Show in Explorer", "actions", None, _reveal, batch=True, main_thread=True)
