"""End-to-end: start a real OLE file drag, tap Shift, drop on the top slice of the wheel.

Requires Limewheel running with "explorer_only": false. Usage: python tests/e2e_drag.py FILE
"""
import ctypes
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QLabel

user32 = ctypes.windll.user32
target = Path(sys.argv[1]).resolve()


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        class MI(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("extra", ctypes.c_size_t)]

        class KI(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("extra", ctypes.c_size_t)]
        _fields_ = [("mi", MI), ("ki", KI), ("pad", ctypes.c_byte * 32)]
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def mouse(x, y, flags):
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    i = INPUT(type=0)
    i.u.mi.dx, i.u.mi.dy = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
    i.u.mi.dwFlags = flags | 0x8000 | 0x0001   # ABSOLUTE | MOVE
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def key(vk, up=False):
    i = INPUT(type=1)
    i.u.ki.wVk = vk
    i.u.ki.dwFlags = 2 if up else 0
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


class Source(QLabel):
    def mousePressEvent(self, e):
        print("press at", e.globalPosition().toPoint(), flush=True)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            print("drag start", flush=True)
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(str(target))])
            d = QDrag(self)
            d.setMimeData(md)
            result = d.exec(Qt.CopyAction | Qt.MoveAction)
            print("drag result:", result, flush=True)
            QTimer.singleShot(500, app.quit)


app = QApplication([])
w = Source(f"drag source: {target.name}")
w.setStyleSheet("background:#333;color:white;font-size:16px;padding:20px")
w.resize(300, 120)
w.move(300, 300)
w.show()
w.activateWindow()


def script():
    time.sleep(1.0)
    user32.SetProcessDPIAware()
    scale = user32.GetDpiForSystem() / 96
    cx, cy = int(450 * scale), int(360 * scale)
    mouse(cx, cy, 0)
    time.sleep(0.2)
    mouse(cx, cy, 0x0002)                      # left down
    for i in range(1, 8):
        time.sleep(0.05)
        mouse(cx + i * 4, cy, 0)               # start the drag
    time.sleep(0.4)
    key(0x10)                                  # tap Shift -> Convert wheel
    key(0x10, up=True)
    time.sleep(0.6)
    sx, sy = cx + 28, cy                       # wheel is centered here
    for i in range(1, 13):                     # move up onto the top inner slice
        time.sleep(0.04)
        mouse(sx, sy - int(i * 10 * scale), 0)
    time.sleep(0.5)
    mouse(sx, sy - int(120 * scale), 0x0004)   # left up -> drop
    print("dropped", flush=True)


threading.Thread(target=script, daemon=True).start()
app.exec()
