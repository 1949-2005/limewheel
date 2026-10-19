"""Presets: one drop gives a file that's ready for a destination (Instagram, YouTube, email,
print...). Built-in presets plus your own in settings.json under "custom_presets" (same keys)."""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageOps

from .base import OpError, ext_of, out_path, register
from .images import IMG_IN, TARGETS, open_image, save_image
from .media import H264, VIDEO_IN, ff, streams

VID = VIDEO_IN - {"gif"}
MB = 1 << 20

# fit: "cover" fills the frame exactly (centre crop), "contain" fits inside without upscaling,
#      "page" is contain on a portrait or landscape page to match the picture.
PRESETS: list[dict] = [
    {"id": "ig-post", "kind": "image", "name": "Instagram post", "spec": "1080 × 1350 (4:5) JPG",
     "w": 1080, "h": 1350, "fit": "cover", "format": "jpg", "quality": 92, "tag": "instagram"},
    {"id": "ig-square", "kind": "image", "name": "Instagram square", "spec": "1080 × 1080 JPG",
     "w": 1080, "h": 1080, "fit": "cover", "format": "jpg", "quality": 92, "tag": "square"},
    {"id": "story", "kind": "image", "name": "Story / Status", "spec": "1080 × 1920 (9:16) JPG",
     "w": 1080, "h": 1920, "fit": "cover", "format": "jpg", "quality": 92, "tag": "story"},
    {"id": "yt-thumb", "kind": "image", "name": "YouTube thumbnail", "spec": "1280 × 720 JPG, under 2 MB",
     "w": 1280, "h": 720, "fit": "cover", "format": "jpg", "quality": 92, "max_bytes": 2 * MB, "tag": "thumbnail"},
    {"id": "web", "kind": "image", "name": "Web", "spec": "longest side 1920 px, WebP",
     "w": 1920, "h": 1920, "fit": "contain", "format": "webp", "quality": 82, "tag": "web"},
    {"id": "email", "kind": "image", "name": "Email", "spec": "longest side 2048 px, under 1 MB",
     "w": 2048, "h": 2048, "fit": "contain", "format": "jpg", "quality": 88, "max_bytes": 1 * MB, "tag": "email"},
    {"id": "print-a4", "kind": "image", "name": "Print A4", "spec": "fits A4 at 300 DPI, high quality",
     "w": 2480, "h": 3508, "fit": "page", "format": "jpg", "quality": 95, "dpi": 300, "tag": "print"},
    {"id": "reel", "kind": "video", "name": "Reel / TikTok / Short", "spec": "1080 × 1920 (9:16) MP4",
     "w": 1080, "h": 1920, "fit": "cover", "tag": "reel"},
    {"id": "yt-1080", "kind": "video", "name": "YouTube 1080p", "spec": "up to 1920 × 1080 MP4",
     "w": 1920, "h": 1080, "fit": "contain", "tag": "1080p"},
    {"id": "discord", "kind": "video", "name": "Discord", "spec": "MP4 under 10 MB", "max_bytes": 10 * MB,
     "tag": "discord"},
    {"id": "email-video", "kind": "video", "name": "Email", "spec": "MP4 under 25 MB", "max_bytes": 25 * MB,
     "tag": "email"},
]


def all_presets(settings: dict | None) -> list[dict]:
    custom = [p for p in (settings or {}).get("custom_presets", []) if isinstance(p, dict) and p.get("id")]
    return PRESETS + custom


def find(pid: str, settings: dict | None) -> dict:
    for p in all_presets(settings):
        if p["id"] == pid:
            return p
    raise OpError(f"Unknown preset: {pid}")


def kind_of(path: Path) -> str | None:
    e = ext_of(path)
    return "image" if e in IMG_IN else "video" if e in VID else None


# ------------------------------------------------------------------ images

def _box(pr: dict, im: Image.Image) -> tuple[int, int]:
    w, h = int(pr.get("w") or im.width), int(pr.get("h") or im.height)
    if pr.get("fit") == "page" and (im.width > im.height) != (w > h):
        w, h = h, w                                    # landscape picture -> landscape page
    return w, h


def _image(p: Path, pr: dict, ctx) -> Path:
    im = open_image(p)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        im = im.copy()
    w, h = _box(pr, im)
    if pr.get("fit") == "cover":
        im = ImageOps.fit(im, (w, h), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    elif im.width > w or im.height > h:
        im.thumbnail((w, h), Image.Resampling.LANCZOS)  # never upscales
    ext = pr.get("format") or (ext_of(p) if ext_of(p) in TARGETS else "jpg")
    if ext not in TARGETS:
        raise OpError(f"Preset format {ext} isn't supported")
    dst = out_path(p, ext, f"-{pr.get('tag', pr['id'])}")
    save_image(im, dst, ext, int(pr.get("quality", 85)), src_ext=ext_of(p), dpi=pr.get("dpi"))
    limit = pr.get("max_bytes")
    if limit and dst.stat().st_size > limit:
        from .extras import fit_bytes
        data = fit_bytes(im, ext, limit)
        if data is None:
            dst.unlink(missing_ok=True)
            raise OpError(f"Couldn't get this picture under {limit // 1024} KB")
        dst.write_bytes(data)
    return dst


# ------------------------------------------------------------------ video

def _video(p: Path, pr: dict, ctx) -> Path:
    tag = f"-{pr.get('tag', pr['id'])}"
    limit = pr.get("max_bytes")
    if limit and not pr.get("w"):
        if p.stat().st_size <= limit and ext_of(p) == "mp4":
            dst = out_path(p, "mp4", tag)
            shutil.copy2(p, dst)                       # already small enough
            return dst
        from .extras import _av_to_size
        made = _av_to_size(p, {"bytes": limit}, ctx)
        dst = out_path(p, "mp4", tag)
        made.replace(dst)
        return dst
    w, h = int(pr["w"]), int(pr["h"])
    if pr.get("fit") == "cover":
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    else:
        vf = (f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease,"
              "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1")
    s = streams(p)
    if s.get("hdr"):
        from .media import TONEMAP
        vf = TONEMAP[1] + "," + vf
    dst = out_path(p, "mp4", tag)
    ff("-i", p, "-map", "0:v:0", "-map", "0:a?", "-vf", vf, *H264, "-movflags", "+faststart", dst)
    if limit and dst.stat().st_size > limit:
        from .extras import _av_to_size
        made = _av_to_size(dst, {"bytes": limit}, ctx)
        made.replace(dst)
    return dst


def apply(p: Path, pid: str, ctx) -> Path:
    pr = find(pid, getattr(ctx, "settings", None))
    kind = kind_of(p)
    if kind != pr.get("kind"):
        raise OpError(f"“{pr['name']}” is for {pr.get('kind')}s")
    return _image(p, pr, ctx) if kind == "image" else _video(p, pr, ctx)


# ------------------------------------------------------------------ wheel op

def _ask(paths):
    kinds = {kind_of(p) for p in paths}
    if len(kinds) != 1 or None in kinds:
        raise OpError("Presets work on pictures or on videos (not both at once)")
    from ..tools_ui import presets_dialog
    from .. import settings as settings_mod
    choices = [p for p in all_presets(settings_mod.load()) if p.get("kind") in kinds]
    pid = presets_dialog(choices, len(paths))
    return {"preset": pid} if pid else None


def _run(p: Path, opts, ctx):
    return apply(p, opts["preset"], ctx)


register("img.preset", "Presets…", "tools", IMG_IN - {"svg"}, _run, prompt=_ask)
register("vid.preset", "Presets…", "tools", VID, _run, prompt=_ask)
