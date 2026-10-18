"""Limewheel — drag a file, tap a key, drop to convert.

    python main.py              run in the tray
    python main.py FILE...      open the wheel for these files once (also used by "Send to")
"""
import sys

from limewheel.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
