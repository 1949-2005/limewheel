"""Theme tokens (colours, name, settings folder) for Limewheel."""
from __future__ import annotations

import os
from pathlib import Path

BRANDS = {
    "limewheel": {
        "name": "Limewheel",
        "settings_dir": "LimewheelWin",
        "ink": "#13210A", "ink_soft": "#3B5122", "section": "#3A6B06",
        "accent": "#3F7A06", "accent_dark": "#2F5F03",
        "hairline": "rgba(30, 70, 10, 0.16)",
        "panel_stops": [(0.0, "#E6FB8E"), (0.38, "#BFEA72"), (0.72, "#A9E2BC"), (1.0, "#C8D8F4")],
        "petal_top": (245, 252, 232, 244), "petal_bottom": (231, 245, 214, 244),
        "hot_top": "#5E9E10", "hot_bottom": "#3F7A06",
        "rim": (170, 210, 118, 170), "hole": (128, 178, 78, 150), "pill": (246, 252, 236, 235),
        "menu_bg": "#F3FBE6", "shadow": (25, 60, 10),
        "unchecked": "rgba(40,90,20,0.55)",
        "tone": (40, 80, 20), "tone_dark": (30, 70, 10), "accent_rgb": (63, 122, 6), "focus_dark": "#1E4002",
        "select_soft": (175, 228, 120), "wave_off": (80, 130, 50), "popup": "#F3FBE6",
    },
}


def _selected() -> str:
    env = os.environ.get("LIMEWHEEL_BRAND", "").strip().lower()
    if env in BRANDS:
        return env
    f = Path(__file__).with_name("brand.txt")
    try:
        name = f.read_text(encoding="utf-8").strip().lower()
        if name in BRANDS:
            return name
    except OSError:
        pass
    return "limewheel"


ID = _selected()
B = BRANDS[ID]
NAME: str = B["name"]
