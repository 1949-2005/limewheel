"""Global low-level mouse/keyboard hooks + hotkey, on a dedicated thread.

Detects "a mouse drag is in progress" and emits a signal when a modifier key
is tapped during it, which is how the wheel is summoned mid-drag.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading

from PySide6.QtCore import QObject, Signal

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
WM_HOTKEY, WM_QUIT = 0x312, 0x12
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

MODIFIER_VKS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift",
                0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
                0x12: "alt", 0xA4: "alt", 0xA5: "alt"}
EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass", "Progman", "WorkerW"}
DRAG_THRESHOLD = 8

KEY_NAMES = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D, "home": 0x24, "end": 0x23,
             **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part == "win":
            mods |= MOD_WIN
        elif part in KEY_NAMES:
            vk = KEY_NAMES[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            return None
    return (mods | MOD_NOREPEAT, vk) if vk else None


def _class_at(pt: wt.POINT) -> str:
    hwnd = user32.GetAncestor(user32.WindowFromPoint(pt), 2)  # GA_ROOT
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


class HookSignals(QObject):
    modifier = Signal(str, bool)   # key name, a drag is in progress
    escape = Signal()
    mouse_up = Signal()
    hotkey = Signal()


class Hooks:
    def __init__(self, settings: dict):
        self.signals = HookSignals()
        self.settings = settings
        self.wheel_visible = False
        self._down = False
        self._dragging = False
        self._origin = (0, 0)
        self._from_explorer = False
        self._held: set[int] = set()
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.hotkey_ok = False
        # Keep references so ctypes callbacks aren't garbage-collected.
        self._kb_proc = HOOKPROC(self._on_key)
        self._ms_proc = HOOKPROC(self._on_mouse)

    # ---------------------------------------------------------- lifecycle
    def start(self):
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="limewheel-hooks")
        self._thread.start()
        ready.wait(2)

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
            self._tid = 0

    def restart(self):
        self.stop()
        self.start()

    def _run(self, ready: threading.Event):
        self._tid = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        hk = parse_hotkey(self.settings.get("hotkey", ""))
        self.hotkey_ok = bool(hk and user32.RegisterHotKey(None, 1, hk[0], hk[1]))
        self.installed = bool(kb and ms)
        if os.environ.get("LIMEWHEEL_DEBUG"):
            print(f"[debug] hooks kb={bool(kb)} mouse={bool(ms)} hotkey={self.hotkey_ok} "
                  f"err={ctypes.get_last_error()}", flush=True)
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.signals.hotkey.emit()
        if self.hotkey_ok:
            user32.UnregisterHotKey(None, 1)
        user32.UnhookWindowsHookEx(kb)
        user32.UnhookWindowsHookEx(ms)

    # -------------------------------------------------------------- hooks
    def _on_mouse(self, code, wparam, lparam):
        if code >= 0:
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if wparam == WM_LBUTTONDOWN:
                self._down, self._dragging = True, False
                self._origin = (info.pt.x, info.pt.y)
                try:
                    self._from_explorer = _class_at(info.pt) in EXPLORER_CLASSES
                except Exception:
                    self._from_explorer = True
            elif wparam == WM_MOUSEMOVE and self._down and not self._dragging:
                if abs(info.pt.x - self._origin[0]) + abs(info.pt.y - self._origin[1]) > DRAG_THRESHOLD:
                    self._dragging = True
            elif wparam == WM_LBUTTONUP:
                was = self._dragging
                self._down = self._dragging = False
                if was:
                    self.signals.mouse_up.emit()
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_key(self, code, wparam, lparam):
        if code >= 0:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if vk not in self._held:
                    self._held.add(vk)
                    name = MODIFIER_VKS.get(vk)
                    if name:
                        allowed = self._from_explorer or not self.settings.get("explorer_only", True)
                        drag = self._dragging and allowed
                        if drag or self.wheel_visible:
                            self.signals.modifier.emit(name, drag)
                    elif vk == 0x1B and self.wheel_visible:
                        self.signals.escape.emit()
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                self._held.discard(vk)
        return user32.CallNextHookEx(None, code, wparam, lparam)
