"""Classic (non-Store) registration of the Limewheel Explorer context menu.

Registers the IExplorerCommand handler for the current user only (HKCU), so no
administrator rights are required. This is the fallback used by the plain
Win32/PyInstaller install; the MSIX package declares the same handler
declaratively in its manifest instead.

Public API:
    register(dll: Path) -> None
    unregister() -> None

register() expects the path to LimewheelShell.dll. Limewheel.exe is assumed to
sit in the same folder (that is the contract with the app and with the DLL,
which locates the exe next to itself).
"""
from __future__ import annotations

import ctypes
import winreg
from pathlib import Path

# CLSID of the handler implemented in limewheel_shell.cpp.
CLSID = "{5C3E8F2A-7B41-4D9C-9A6E-1F0B2C7D8E91}"

# Registry locations (all under HKCU\Software\Classes).
_CLSID_KEY = r"Software\Classes\CLSID\%s" % CLSID
_INPROC_KEY = _CLSID_KEY + r"\InprocServer32"
_VERB_STAR = r"Software\Classes\*\shell\Limewheel"
_VERB_DIR = r"Software\Classes\Directory\shell\Limewheel"

# SHChangeNotify constants.
_SHCNE_ASSOCCHANGED = 0x08000000
_SHCNF_IDLIST = 0x0000


def _notify_assoc_changed() -> None:
    """Tell the shell that file associations changed."""
    try:
        ctypes.windll.shell32.SHChangeNotify(
            _SHCNE_ASSOCCHANGED, _SHCNF_IDLIST, None, None
        )
    except Exception:
        # Notification is best-effort; a logoff/logon picks it up anyway.
        pass


def _set(key_path: str, name: str, value: str) -> None:
    """Create key_path under HKCU and set a string value on it."""
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _write_verb(key_path: str, exe: Path) -> None:
    """Write the ExplorerCommandHandler verb values under key_path."""
    icon = '%s,0' % str(exe)
    _set(key_path, "ExplorerCommandHandler", CLSID)
    _set(key_path, "MUIVerb", "Limewheel")
    _set(key_path, "Icon", icon)
    _set(key_path, "MultiSelectModel", "Player")


def register(dll: Path) -> None:
    """Register the context-menu handler for the current user."""
    dll = Path(dll).resolve()
    exe = dll.with_name("Limewheel.exe")

    # 1. The COM server (CLSID -> InprocServer32).
    #    Default value of the CLSID key: a friendly name (optional but tidy).
    _set(_CLSID_KEY, "", "Limewheel context menu")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _INPROC_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(dll))
        winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Apartment")

    # 2. The verbs on files (*) and folders (Directory).
    _write_verb(_VERB_STAR, exe)
    _write_verb(_VERB_DIR, exe)

    _notify_assoc_changed()


def _delete_tree(root, key_path: str) -> None:
    """Recursively delete key_path (and its subkeys) under root; ignore absent."""
    try:
        with winreg.OpenKey(root, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(root, key_path + "\\" + sub)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteKey(root, key_path)
    except FileNotFoundError:
        pass


def unregister() -> None:
    """Remove exactly the keys that register() created."""
    root = winreg.HKEY_CURRENT_USER
    _delete_tree(root, _VERB_STAR)
    _delete_tree(root, _VERB_DIR)
    _delete_tree(root, _CLSID_KEY)
    _notify_assoc_changed()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Register/unregister the "
                                     "Limewheel Explorer context menu (HKCU).")
    parser.add_argument("action", choices=["register", "unregister"])
    parser.add_argument("--dll", type=Path,
                        help="Path to LimewheelShell.dll (required for register)")
    args = parser.parse_args()

    if args.action == "register":
        if not args.dll:
            parser.error("--dll is required for register")
        register(args.dll)
        print("Registered Limewheel context menu for current user.")
    else:
        unregister()
        print("Unregistered Limewheel context menu for current user.")
