"""User settings stored as JSON in %APPDATA%\\LimewheelWin."""
import json
import os
from pathlib import Path

from .brand import B

DIR = Path(os.environ.get("APPDATA", Path.home())) / B["settings_dir"]
FILE = DIR / "settings.json"

DEFAULTS = {
    # Which modifier opens which wheel while dragging a file.
    "keys": {"convert": "shift", "tools": "ctrl", "actions": "alt"},
    # Global hotkey that opens the wheel for the files selected in Explorer.
    "hotkey": "ctrl+alt+t",
    # Only react to drags that start in Explorer / the desktop.
    "explorer_only": True,
    # Quality used for lossy image output (JPG/WebP/AVIF/HEIC).
    "image_quality": 85,
    # Touch screens / pens: press, hold briefly, then drag to open the Convert wheel.
    "hold_to_convert": True,
    # Same gesture with a mouse (off by default).
    "hold_with_mouse": False,
}


def load() -> dict:
    data = json.loads(json.dumps(DEFAULTS))
    try:
        stored = json.loads(FILE.read_text(encoding="utf-8"))
        for k, v in stored.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
