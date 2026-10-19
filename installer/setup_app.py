"""<Name>Setup.exe — installs the app  for the current user (no admin rights needed).

    <Name>Setup.exe                      interactive installer
    <Name>Setup.exe --silent [--dir D] [--no-desktop] [--startup] [--no-launch]
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QWidget)

def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / name


sys.path.insert(0, str(resource("")))
from limewheel.brand import NAME as APP  # noqa: E402

VERSION = "1.3.0"
PUBLISHER = f"{APP} for Windows"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP}"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP
CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- install steps

def _running() -> bool:
    r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {APP}.exe", "/NH"], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    return f"{APP}.exe".lower() in r.stdout.lower()


def stop_running():
    """Close a running copy and wait until Windows has actually released its files."""
    import time
    subprocess.run(["taskkill", "/IM", f"{APP}.exe", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    for _ in range(40):
        if not _running():
            break
        time.sleep(0.25)
    time.sleep(0.5)


def extract(dest: Path, progress=lambda f: None):
    with zipfile.ZipFile(resource("payload.zip")) as z:
        items = z.infolist()
        total = sum(i.file_size for i in items) or 1
        done = 0
        dest.mkdir(parents=True, exist_ok=True)
        import time
        for i in items:
            for attempt in range(20):            # a file can stay locked briefly after the old app exits
                try:
                    z.extract(i, dest)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.5)
            done += i.file_size
            progress(done / total)


def shortcut(lnk: Path, target: Path):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        sh = win32com.client.Dispatch("WScript.Shell")
        s = sh.CreateShortcut(str(lnk))
        s.TargetPath = str(target)
        s.WorkingDirectory = str(target.parent)
        s.IconLocation = f"{target},0"
        s.Description = "Drag a file, tap a key, drop to convert"
        s.Save()
    finally:
        pythoncom.CoUninitialize()


def start_menu() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop() -> Path:
    import ctypes
    from ctypes import wintypes
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
    return Path(buf.value)


UNINSTALL_PS1 = r"""# Removes __APP__ (installed per-user by its setup). Settings in %APPDATA% are kept.
$ErrorActionPreference = 'SilentlyContinue'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Stop-Process -Name '__APP__' -Force
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '__APP__'
Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\__APP__' -Recurse
Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Programs')) '__APP__.lnk')
Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '__APP__.lnk')
# Explorer right-click menu (only present when the menu add-on was installed)
if (Test-Path -LiteralPath (Join-Path $dir 'LimewheelShell.dll')) {
  Remove-Item -LiteralPath 'HKCU:\Software\Classes\*\shell\Limewheel' -Recurse
  Remove-Item -LiteralPath 'HKCU:\Software\Classes\Directory\shell\Limewheel' -Recurse
  Remove-Item -LiteralPath 'HKCU:\Software\Classes\CLSID\__CLSID__' -Recurse
}
Start-Sleep -Milliseconds 500
Set-Location $env:TEMP
Remove-Item -LiteralPath $dir -Recurse -Force
if (Test-Path -LiteralPath $dir) {
  # Explorer may still hold the menu add-on: finish the clean-up at the next sign-in
  Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name '__APP__Cleanup' -Value ('cmd.exe /c rmdir /s /q "' + $dir + '"')
}
Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show('__APP__ was removed.', '__APP__') | Out-Null
"""


SHELL_DLL = "LimewheelShell.dll"            # Explorer right-click menu add-on (Limewheel builds only)


def register(dest: Path, desktop_icon: bool, startup: bool):
    import winreg
    exe = dest / f"{APP}.exe"
    from shellreg import CLSID
    (dest / "uninstall.ps1").write_text(UNINSTALL_PS1.replace("__APP__", APP).replace("__CLSID__", CLSID),
                                        encoding="utf-8-sig")
    if (dest / SHELL_DLL).exists():
        import shellreg
        shellreg.register(dest / SHELL_DLL)
    shortcut(start_menu() / f"{APP}.lnk", exe)
    if desktop_icon:
        shortcut(desktop() / f"{APP}.lnk", exe)
    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in [("DisplayName", APP), ("DisplayVersion", VERSION), ("Publisher", PUBLISHER),
                          ("DisplayIcon", f"{exe},0"), ("InstallLocation", str(dest)),
                          ("UninstallString", f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle '
                                              f'Hidden -File "{dest / "uninstall.ps1"}"')]:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if startup:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


def remove_previous(dest: Path):
    """Updating: clear the old version's program files so nothing stale is left behind.
    Only touches a folder that is clearly ours (contains our exe or our _internal bundle)."""
    import shutil
    import time
    if not dest.exists() or not ((dest / f"{APP}.exe").exists() or (dest / "_internal").is_dir()):
        return
    for _ in range(20):                       # the old exe may need a moment to exit
        try:
            shutil.rmtree(dest / "_internal", ignore_errors=False) if (dest / "_internal").exists() else None
            break
        except OSError:
            time.sleep(0.5)
    for name in (f"{APP}.exe", "icon.png", "uninstall.ps1"):
        try:
            (dest / name).unlink(missing_ok=True)
        except OSError:
            pass
    # Explorer keeps the right-click add-on loaded, so it can't be deleted or overwritten while
    # Explorer runs. Windows does allow renaming a loaded DLL: move it aside, delete it next time.
    for old in dest.glob(f"{SHELL_DLL}.*.old"):
        try:
            old.unlink()
        except OSError:
            pass
    dll = dest / SHELL_DLL
    if dll.exists():
        try:
            dll.unlink()
        except OSError:
            try:
                dll.rename(dest / f"{SHELL_DLL}.{int(time.time())}.old")
            except OSError:
                pass


def install(dest: Path, desktop_icon: bool, startup: bool, progress=lambda f: None):
    stop_running()
    remove_previous(dest)
    extract(dest, progress)
    register(dest, desktop_icon, startup)


def launch(dest: Path):
    subprocess.Popen([str(dest / f"{APP}.exe")], cwd=dest)


# ------------------------------------------------------------------ UI

class _Sig(QObject):
    progress = Signal(float)
    done = Signal(str)          # "" on success, else the error


class _Job(QRunnable):
    def __init__(self, dest, desk, startup):
        super().__init__()
        self.args, self.sig = (dest, desk, startup), _Sig()

    def run(self):
        try:
            install(*self.args, progress=self.sig.progress.emit)
            self.sig.done.emit("")
        except Exception as e:
            self.sig.done.emit(f"{type(e).__name__}: {e}")


def run_ui(app: QApplication) -> int:
    from limewheel.ui import Panel, _Bar

    dlg = Panel(f"Install {APP}", apply_text="Install", width=470)
    icon_file = resource("icon.png")
    if icon_file.exists():
        dlg.setWindowIcon(QIcon(str(icon_file)))
    b = dlg.body_layout
    intro = QLabel("Drag a file, tap Shift, Ctrl or Alt, and drop it on an option to convert, edit or move it. "
                   "Everything runs on this PC.")
    intro.setWordWrap(True)
    b.addWidget(intro)

    sec = QLabel("Install location")
    sec.setProperty("role", "section")
    b.addWidget(sec)
    row = QWidget()
    rl = QHBoxLayout(row)
    rl.setContentsMargins(0, 0, 0, 0)
    path = QLineEdit(str(DEFAULT_DIR))
    path.setAccessibleName("Install location")
    browse = QPushButton("Change…")

    def pick():
        d = QFileDialog.getExistingDirectory(dlg, f"Install {APP} to", path.text())
        if d:
            path.setText(str(Path(d) / APP) if Path(d).name != APP else d)

    browse.clicked.connect(pick)
    rl.addWidget(path, 1)
    rl.addWidget(browse)
    b.addWidget(row)
    hint = QLabel("No administrator rights needed. Keep the path short.")
    hint.setProperty("role", "hint")
    b.addWidget(hint)

    sec2 = QLabel("Options")
    sec2.setProperty("role", "section")
    b.addWidget(sec2)
    desk = QCheckBox("Create a desktop shortcut")
    desk.setChecked(True)
    startup = QCheckBox(f"Start {APP} when I sign in")
    startup.setChecked(True)
    launch_cb = QCheckBox(f"Open {APP} when setup finishes")
    launch_cb.setChecked(True)
    for w in (desk, startup, launch_cb):
        b.addWidget(w)

    status = QLabel("")
    status.setProperty("role", "hint")
    status.setWordWrap(True)
    bar = _Bar()
    bar.stop(0.0)
    bar.hide()
    b.addWidget(bar)
    b.addWidget(status)

    state = {"done": False}

    def start():
        if state["done"]:
            if launch_cb.isChecked():
                launch(Path(path.text()))
            dlg.accept()
            return
        dest = Path(path.text().strip() or DEFAULT_DIR)
        if len(str(dest)) > 120:
            status.setText("That path is too long for Windows to load the app from. Choose a shorter folder.")
            return
        for w in (path, browse, desk, startup, dlg.apply_btn):
            w.setEnabled(False)
        bar.show()
        status.setText("Installing…")
        job = _Job(dest, desk.isChecked(), startup.isChecked())
        job.setAutoDelete(False)
        state["job"] = job
        job.sig.progress.connect(lambda f: (setattr(bar, "value", f), bar.update()))
        job.sig.done.connect(finished)
        QThreadPool.globalInstance().start(job)

    def finished(err: str):
        dlg.apply_btn.setEnabled(True)
        if err:
            status.setText(f"Setup couldn't finish: {err}")
            for w in (path, browse, desk, startup):
                w.setEnabled(True)
            return
        state["done"] = True
        bar.stop(1.0)
        dlg.title.setText(f"{APP} is installed")
        status.setText("Find it in the Start menu or the system tray. Uninstall any time from "
                       "Settings → Apps → Installed apps.")
        dlg.apply_btn.setText("Finish")
        dlg.adjustSize()

    dlg.apply_btn.clicked.disconnect()
    dlg.apply_btn.clicked.connect(start)
    dlg.run()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--silent" in args:
        dest = Path(args[args.index("--dir") + 1]) if "--dir" in args else DEFAULT_DIR
        try:
            install(dest, "--no-desktop" not in args, "--startup" in args)
        except BaseException:                    # silent: report via exit code + a log, never a popup
            import traceback
            (Path(os.environ["TEMP"]) / f"{APP}-setup-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            return 1
        if "--no-launch" not in args:
            launch(dest)
        return 0
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(f"{APP} Setup")
    return run_ui(app)


if __name__ == "__main__":
    sys.exit(main())
