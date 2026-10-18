"""Audio/video conversions and tools via ffmpeg."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path

from .base import (CREATE_NO_WINDOW, OpError, ask_fields, ask_int, ext_of, fmt_time,
                   out_path, parse_time, register, run_proc)

VIDEO_IN = frozenset("mp4 mov mkv webm avi wmv m4v flv mpg mpeg 3gp ts gif".split())
AUDIO_IN = frozenset("mp3 m4a aac wav flac ogg opus aiff wma".split())
MEDIA_IN = VIDEO_IN | AUDIO_IN

H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
VIDEO_ARGS = {
    "mp4": H264 + ["-movflags", "+faststart"],
    "mov": H264 + ["-movflags", "+faststart"],
    "mkv": H264,
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"],
    "avi": ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-q:a", "3"],
    "wmv": ["-c:v", "wmv2", "-b:v", "3M", "-c:a", "wmav2", "-b:a", "192k"],
    "gif": ["-vf", "fps=12,scale='min(640,iw)':-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
            "-loop", "0", "-an"],
}
AUDIO_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
    "aiff": ["-c:a", "pcm_s16be"],
    "wma": ["-c:a", "wmav2", "-b:a", "192k"],
}


@cache
def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise OpError("ffmpeg was not found")
        return found


def ff(*args) -> None:
    run_proc([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args])


def probe(p: Path) -> dict:
    """Duration and whether the file has video / audio, parsed from `ffmpeg -i`."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    text = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    video = bool(re.search(r"Stream #.*Video:", text)) and "attached pic" not in text.split("Video:")[-1][:200]
    return {"duration": dur, "video": video, "audio": "Audio:" in text}


def _convert(ext: str, is_audio_target: bool):
    def run(p: Path, opts, ctx):
        dst = out_path(p, ext)
        if is_audio_target:
            ff("-i", p, "-vn", "-map_metadata", "0", *AUDIO_ARGS[ext], dst)
        else:
            ff("-i", p, *VIDEO_ARGS[ext], dst)
        return dst
    return run


for _ext in VIDEO_ARGS:
    register(f"vid.to.{_ext}", f"to {_ext.upper()}", "convert", VIDEO_IN, _convert(_ext, False),
             badge=_ext.upper(), skip_same=_ext)
for _ext in ("mp3", "m4a", "wav"):
    register(f"vid.audio.{_ext}", f"Audio {_ext.upper()}", "convert", VIDEO_IN - {"gif"},
             _convert(_ext, True), badge=_ext.upper())
for _ext in AUDIO_ARGS:
    register(f"aud.to.{_ext}", f"to {_ext.upper()}", "convert", AUDIO_IN, _convert(_ext, True),
             badge=_ext.upper(), skip_same=_ext)


# ------------------------------------------------------------------- tools

def _ask_trim(paths):
    dur = probe(paths[0])["duration"]
    if dur:
        from ..dialogs import trim_dialog
        return trim_dialog(paths[0], dur, "Video" if ext_of(paths[0]) in VIDEO_IN else "Audio")
    vals = ask_fields("Trim", [("Start", "0:00"), ("End", "")],
                      "Couldn't read the length. Use m:ss or seconds; leave End blank for the end.")
    if vals is None:
        return None
    try:
        start, end = parse_time(vals[0]) or 0.0, parse_time(vals[1])
    except ValueError:
        raise OpError("Times must look like 1:23 or 83.5")
    if end is not None and end <= start:
        raise OpError("End must be after start")
    return {"start": start, "end": end}


def _trim(p: Path, opts, ctx):
    dst = out_path(p, ext_of(p), "-trimmed")
    args = ["-ss", f"{opts['start']:.3f}", "-i", p]
    if opts["end"] is not None:
        args += ["-t", f"{opts['end'] - opts['start']:.3f}"]
    ff(*args, "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", dst)
    return dst


def _ask_split(paths):
    from ..dialogs import fmt, slider_dialog
    dur = probe(paths[0])["duration"]
    n = slider_dialog("Split", "Equal parts", 2, 20, 2,
                      lambda v: (f"{v} parts", f"Each part ≈ {fmt(dur / v)}" if dur else ""))
    return None if n is None else {"parts": n}


def _split(p: Path, opts, ctx):
    dur = probe(p)["duration"]
    if not dur:
        raise OpError("Could not read the duration")
    step = dur / opts["parts"]
    outs = []
    for i in range(opts["parts"]):
        dst = out_path(p, ext_of(p), f"-part{i + 1}")
        ff("-ss", f"{i * step:.3f}", "-i", p, "-t", f"{step:.3f}", "-map", "0", "-c", "copy",
           "-avoid_negative_ts", "make_zero", dst)
        outs.append(dst)
    return outs


def _merge(paths: list[Path], opts, ctx):
    ext = ext_of(paths[0])
    dst = out_path(paths[0], ext, "-merged")
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "list.txt"
        lst.write_text("".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                               for p in paths), encoding="utf-8")
        try:
            ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", dst)
        except OpError:
            dst.unlink(missing_ok=True)
            codec = AUDIO_ARGS.get(ext) or VIDEO_ARGS.get(ext) or H264
            ff("-f", "concat", "-safe", "0", "-i", lst, *codec, dst)
    return dst


def _compress_video(p: Path, opts, ctx):
    dst = out_path(p, "mp4", "-compressed")
    ff("-i", p, "-vf", "scale='min(1920,iw)':-2", "-c:v", "libx264", "-preset", "medium", "-crf", "28",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst)
    return dst


def _compress_audio(p: Path, opts, ctx):
    dst = out_path(p, "mp3", "-compressed")
    ff("-i", p, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", dst)
    return dst


def _v(suffix, *args, ext=None):
    def run(p: Path, opts, ctx):
        dst = out_path(p, ext or ext_of(p), suffix)
        ff("-i", p, *args, dst)
        return dst
    return run


def _thumbnail(p: Path, opts, ctx):
    dur = probe(p)["duration"]
    dst = out_path(p, "jpg", "-frame")
    ff("-ss", f"{min(1.0, dur / 10):.3f}", "-i", p, "-frames:v", "1", "-q:v", "2", dst)
    return dst


VID = VIDEO_IN - {"gif"}
register("vid.compress", "Compress", "tools", VID, _compress_video)
register("vid.trim", "Trim…", "tools", VID, _trim, prompt=_ask_trim)
register("vid.split", "Split…", "tools", VID, _split, prompt=_ask_split)
register("vid.merge", "Merge", "tools", VID, _merge, batch=True, min_files=2)
register("vid.mute", "Mute", "tools", VID, _v("-muted", "-c:v", "copy", "-an"))
register("vid.720", "Resize 720p", "tools", VID,
         _v("-720p", "-vf", "scale=-2:'min(720,ih)'", *H264, ext="mp4"))
register("vid.rotate", "Rotate 90°", "tools", VID, _v("-rotated", "-vf", "transpose=1", *H264, ext="mp4"))
register("vid.frame", "Snapshot", "tools", VID, _thumbnail)

register("aud.compress", "Compress", "tools", AUDIO_IN, _compress_audio)
register("aud.trim", "Trim…", "tools", AUDIO_IN, _trim, prompt=_ask_trim)
register("aud.split", "Split…", "tools", AUDIO_IN, _split, prompt=_ask_split)
register("aud.merge", "Merge", "tools", AUDIO_IN, _merge, batch=True, min_files=2)
register("aud.normalize", "Normalize", "tools", AUDIO_IN,
         lambda p, o, c: _v("-normalized", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                            *AUDIO_ARGS.get(ext_of(p), AUDIO_ARGS["mp3"]))(p, o, c))
