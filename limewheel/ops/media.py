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

# CRF 20: visibly cleaner than 23 (VMAF 93 vs 90 in our tests); native AAC needs 192k to keep the top octave.
H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
VIDEO_ARGS = {
    "mp4": H264 + ["-movflags", "+faststart"],
    "mov": H264 + ["-movflags", "+faststart"],
    "mkv": H264,
    "webm": ["-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0", "-deadline", "good", "-cpu-used", "4",
             "-row-mt", "1", "-tile-columns", "2", "-c:a", "libopus", "-b:a", "128k"],
    "avi": ["-c:v", "mpeg4", "-vtag", "XVID", "-q:v", "3", "-mbd", "rd", "-trellis", "1",
            "-c:a", "libmp3lame", "-q:a", "3"],
    "wmv": ["-c:v", "wmv2", "-q:v", "5", "-c:a", "wmav2", "-b:a", "192k"],
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
    a = re.search(r"Audio:.*?, \d+ Hz, ([^,]+)", text)
    layout = a[1].strip() if a else ""
    m = re.match(r"(\d+)(?:\.(\d+))? channels", layout)
    channels = (1 if layout == "mono" else 2 if layout.startswith("stereo") else
                (int(m[1]) + int(m[2] or 0)) if m else 6 if layout.startswith("5.1") else 2 if a else 0)
    v = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", text)
    size = (int(v[1]), int(v[2])) if v else (0, 0)
    return {"duration": dur, "video": video, "audio": "Audio:" in text, "channels": channels, "size": size}


def atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5–2.0 per stage; chain stages for anything outside."""
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.6g}")
    return ",".join(parts)


def streams(p: Path) -> dict:
    """Codecs of the video/audio streams, HDR transfer, audio sample rate and display rotation."""
    t = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW).stderr.decode("utf-8", "replace")
    video = [m[1] for line in t.splitlines() if "attached pic" not in line
             for m in [re.search(r"Video: (\w+)", line)] if m]
    audio = re.findall(r"Audio: (\w+)", t)
    rate = re.search(r"Audio:.*?(\d+) Hz", t)
    rot = re.search(r"rotation of (-?[\d.]+) degrees", t)
    return {"video": video, "audio": audio, "hdr": bool(re.search(r"Video:.*(smpte2084|arib-std-b67)", t)),
            "rate": int(rate[1]) if rate else 48000, "rotation": float(rot[1]) if rot else 0.0}


# Containers that can take the source streams unchanged -> remux (lossless, ~20x faster).
COPY_OK = {"mp4": ({"h264", "hevc", "av1"}, {"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac"}),
           "mov": ({"h264", "hevc", "prores"}, {"aac", "mp3", "alac", "pcm_s16le", "pcm_s24le"}),
           "mkv": (None, None),
           "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"})}
AUDIO_COPY = {("m4a", "aac"), ("mp3", "mp3"), ("opus", "opus"), ("ogg", "vorbis"), ("flac", "flac")}
TONEMAP = ["-vf", "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                  "zscale=t=bt709:m=bt709:r=tv,format=yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]


def _with_tonemap(args: list, hdr: bool) -> list:
    """HDR sources re-encoded to 8-bit get tone-mapped to SDR (else they look washed out)."""
    if not hdr or "-vf" in args:
        return args
    return TONEMAP + args


def _convert(ext: str, is_audio_target: bool):
    def run(p: Path, opts, ctx):
        dst = out_path(p, ext)
        s = streams(p)
        if is_audio_target:
            if s["audio"] and (ext, s["audio"][0]) in AUDIO_COPY:
                ff("-i", p, "-vn", "-map", "0:a:0", "-map_metadata", "0", "-c:a", "copy", dst)
            else:
                ff("-i", p, "-vn", "-map_metadata", "0", *AUDIO_ARGS[ext], dst)
            return dst
        vok, aok = COPY_OK.get(ext, (set(), set()))
        if s["video"] and ext in COPY_OK and (vok is None or set(s["video"]) <= vok) \
                and (aok is None or set(s["audio"]) <= aok):
            extra = ["-c:s", "mov_text", "-movflags", "+faststart"] if ext in ("mp4", "mov") else []
            extra += ["-tag:v", "hvc1"] if "hevc" in s["video"] and ext in ("mp4", "mov") else []
            try:
                ff("-i", p, "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-c", "copy",
                   *extra, dst)
                return dst
            except OpError:
                dst.unlink(missing_ok=True)          # e.g. image subtitles: fall through to a re-encode
        maps = [] if ext == "gif" else ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0"]
        ff("-i", p, *maps, *_with_tonemap(VIDEO_ARGS[ext], s["hdr"] and ext != "gif"), dst)
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


def _cut(p: Path, start: float, length: float | None, dst: Path):
    """Frame-accurate cut. Video is re-encoded at high quality (stream copy can only cut on keyframes,
    which gave wrong first frames and ~1 s audio drift); audio-only files are cut without re-encoding."""
    args = ["-ss", f"{start:.3f}", "-i", p] + (["-t", f"{length:.3f}"] if length is not None else [])
    if probe(p)["video"]:
        codec = VIDEO_ARGS.get(ext_of(p), H264)
        codec = [("18" if c == "20" and i and codec[i - 1] == "-crf" else c) for i, c in enumerate(codec)]
        s = streams(p)
        ff(*args, "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", *_with_tonemap(codec, s["hdr"]), dst)
    else:
        ff(*args, "-map", "0:a", "-c", "copy", "-avoid_negative_ts", "make_zero", dst)


def _trim(p: Path, opts, ctx):
    dst = out_path(p, ext_of(p), "-trimmed")
    _cut(p, opts["start"], None if opts["end"] is None else opts["end"] - opts["start"], dst)
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
        _cut(p, i * step, step, dst)
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
    # CRF 26: about half the size with little visible loss (CRF 28 cost ~8 VMAF points in our tests)
    dst = out_path(p, "mp4", "-compressed")
    s = streams(p)
    vf = "scale='min(1920,iw)':-2"
    if s["hdr"]:
        vf = TONEMAP[1] + "," + vf
    audio = ["-c:a", "copy"] if s["audio"] and s["audio"][0] == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    ff("-i", p, "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-vf", vf, "-c:v", "libx264",
       "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", dst)
    return dst


def _compress_audio(p: Path, opts, ctx):
    info = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True,
                          creationflags=CREATE_NO_WINDOW).stderr.decode("utf-8", "replace")
    br = re.search(r"Audio: mp3.*?(\d+) kb/s", info)
    if br and int(br[1]) <= 160:
        raise OpError(f"Already a compact MP3 ({br[1]} kb/s) — compressing again would only lose quality")
    dst = out_path(p, "mp3", "-compressed")
    ff("-i", p, "-vn", "-map_metadata", "0", "-c:a", "libmp3lame", "-q:a", "5", dst)   # VBR ~130 kb/s
    return dst


def _rotate(p: Path, opts, ctx):
    """Lossless rotate: change the display rotation tag instead of re-encoding (MP4/MOV/MKV)."""
    ext = ext_of(p)
    s = streams(p)
    dst = out_path(p, ext if ext in ("mp4", "mov", "mkv", "m4v") else "mp4", "-rotated")
    if ext in ("mp4", "mov", "mkv", "m4v"):
        try:
            ff("-display_rotation:v:0", f"{(s['rotation'] - 90) % 360:g}", "-i", p, "-map", "0", "-c", "copy", dst)
            return dst
        except OpError:
            dst.unlink(missing_ok=True)
    ff("-i", p, "-map", "0:v:0", "-map", "0:a?", "-vf", "transpose=1", *H264, "-movflags", "+faststart", dst)
    return dst


def _resize720(p: Path, opts, ctx):
    s = streams(p)
    dst = out_path(p, "mp4", "-720p")
    vf = "scale=-2:'min(720,ih)'"
    if s["hdr"]:
        vf = TONEMAP[1] + "," + vf
    video = [a for a in H264 if a not in ("-c:a", "aac", "-b:a", "192k")]
    audio = ["-c:a", "copy"] if s["audio"] and s["audio"][0] == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    ff("-i", p, "-map", "0:v:0", "-map", "0:a?", "-vf", vf, *video, *audio, "-movflags", "+faststart", dst)
    return dst


def _normalize(p: Path, opts, ctx):
    """Two-pass loudnorm (linear): reaches -16 LUFS without squashing the dynamics, keeps the sample rate."""
    import json
    ln = "I=-16:TP=-1.5:LRA=11"
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p), "-af", f"loudnorm={ln}:print_format=json",
                        "-f", "null", "-"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    text = r.stderr.decode("utf-8", "replace")
    try:
        j = json.loads(text[text.rindex("{"):text.rindex("}") + 1])
    except ValueError:
        raise OpError("Couldn't measure the loudness of this file")
    af = (f"loudnorm={ln}:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}"
          f":measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true")
    ext = ext_of(p)
    dst = out_path(p, ext, "-normalized")
    ff("-i", p, "-map_metadata", "0", "-af", af, "-ar", str(streams(p)["rate"]),
       *AUDIO_ARGS.get(ext, AUDIO_ARGS["mp3"]), dst)
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


def _ask_speed(paths):
    from ..dialogs import speed_dialog
    info = probe(paths[0])
    return speed_dialog(info["duration"], info["video"])


def _speed(p: Path, opts, ctx):
    s = opts["speed"]
    info = probe(p)
    ext = ext_of(p)
    dst = out_path(p, ext, f"-{s:g}x")
    sr = streams(p)["rate"]
    no_pitch = f"asetrate={sr}*{s:.6g},aresample={sr}"           # follows the file's real sample rate
    if info["video"]:
        args = ["-i", p, "-filter:v", f"setpts=PTS/{s:.6g}"]
        if info["audio"] and not opts.get("mute"):
            af = atempo_chain(s) if opts.get("pitch", True) else no_pitch
            args += ["-filter:a", af]
        else:
            args += ["-an"]
        codec = VIDEO_ARGS.get(ext, H264)
        ff(*args, *[c for c in codec if c not in ("-loop",)], dst)
    else:
        af = atempo_chain(s) if opts.get("pitch", True) else no_pitch
        ff("-i", p, "-vn", "-filter:a", af, *AUDIO_ARGS.get(ext, AUDIO_ARGS["mp3"]), dst)
    return dst


def _ask_channels(paths):
    from ..dialogs import channels_dialog
    return channels_dialog(probe(paths[0])["channels"] or 2)


def _channels(p: Path, opts, ctx):
    L, R = opts["left"], opts["right"]
    mode = opts["mode"]
    pan = {
        "Stereo": f"pan=stereo|c0={L:.3g}*c0|c1={R:.3g}*c1",
        "Mono": f"pan=mono|c0={0.5 * L:.3g}*c0+{0.5 * R:.3g}*c1",
        "Swap L/R": f"pan=stereo|c0={L:.3g}*c1|c1={R:.3g}*c0",
        "Left only": f"pan=stereo|c0={L:.3g}*c0|c1={L:.3g}*c0",
        "Right only": f"pan=stereo|c0={R:.3g}*c1|c1={R:.3g}*c1",
    }[mode]
    af = f"aformat=channel_layouts=stereo,{pan}"
    ext = ext_of(p)
    dst = out_path(p, ext, f"-{mode.lower().replace(' ', '').replace('/', '')}")
    if probe(p)["video"]:
        ff("-i", p, "-map", "0:v?", "-map", "0:a", "-c:v", "copy", "-filter:a", af, "-c:a", "aac", "-b:a", "192k",
           dst)
    else:
        ff("-i", p, "-vn", "-filter:a", af, *AUDIO_ARGS.get(ext, AUDIO_ARGS["mp3"]), dst)
    return dst


def _ask_vcrop(paths):
    from PySide6.QtGui import QImage
    from ..editors import crop_editor
    p = paths[0]
    info = probe(p)
    t = min(1.0, info["duration"] / 10) if info["duration"] else 0
    r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(p),
                        "-frames:v", "1", "-vf", "scale='min(1100,iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    img = QImage.fromData(r.stdout)
    if img.isNull() or not info["size"][0]:
        raise OpError("Couldn't read a frame from this video")
    return crop_editor(img, info["size"], "Crop Video", "The crop applies to the whole video.")


def _vcrop(p: Path, opts, ctx):
    x, y, w, h = opts["box"]
    w, h, x, y = w // 2 * 2, h // 2 * 2, x // 2 * 2, y // 2 * 2        # H.264 wants even sizes
    dst = out_path(p, ext_of(p), "-cropped")
    codec = VIDEO_ARGS.get(ext_of(p), H264)
    ff("-i", p, "-filter:v", f"crop={w}:{h}:{x}:{y}", *codec, dst)
    return dst


def bleep_filter(ranges, sound: str, duration: float) -> str:
    """filter_complex string: mute ranges and (optionally) lay a 1 kHz tone over them."""
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges) or "0"
    muted = f"[0:a]aformat=channel_layouts=stereo,volume=0:enable='{expr}'[m]"
    if sound == "silence":
        return muted.replace("[m]", "[out]")
    tone = (f"sine=frequency=1000:sample_rate=48000:duration={duration + 1:.3f},aformat=channel_layouts=stereo,"
            f"volume=0.35,volume=0:enable='not({expr})'[b]")
    return f"{muted};{tone};[m][b]amix=inputs=2:duration=first:normalize=0[out]"


def _bleep_tools():
    import tempfile
    import winsound
    from ..brand import NAME
    tmp = Path(tempfile.gettempdir()) / f"{NAME.lower()}-bleep-preview.wav"
    state = {}

    def make_preview(ranges, sound):
        src, dur = state["src"], state["dur"]
        winsound.PlaySound(None, 0)
        ff("-i", src, "-t", "120", "-filter_complex", bleep_filter(ranges, sound, min(dur, 120)), "-map", "[out]",
           "-c:a", "pcm_s16le", tmp)
        return tmp

    def play(f):
        winsound.PlaySound(str(f), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def stop():
        winsound.PlaySound(None, 0)

    return state, make_preview, play, stop


def _ask_bleep(paths):
    from ..dialogs import bleep_dialog
    info = probe(paths[0])
    if not info["audio"] or not info["duration"]:
        raise OpError("This file has no audio track")
    state, make_preview, play, stop = _bleep_tools()
    state.update(src=paths[0], dur=info["duration"])
    return bleep_dialog(paths[0], info["duration"], make_preview, play, stop)


def _bleep(p: Path, opts, ctx):
    info = probe(p)
    ext = ext_of(p)
    dst = out_path(p, ext, "-bleeped")
    fc = bleep_filter(opts["ranges"], opts["sound"], info["duration"])
    if info["video"]:
        ff("-i", p, "-filter_complex", fc, "-map", "0:v", "-map", "[out]", "-c:v", "copy", "-c:a", "aac",
           "-b:a", "192k", dst)
    else:
        ff("-i", p, "-filter_complex", fc, "-map", "[out]", *AUDIO_ARGS.get(ext, AUDIO_ARGS["mp3"]), dst)
    return dst


VID = VIDEO_IN - {"gif"}
register("aud.bleep", "Bleep…", "tools", AUDIO_IN, _bleep, prompt=_ask_bleep, max_files=1)
register("vid.bleep", "Bleep…", "tools", VID, _bleep, prompt=_ask_bleep, max_files=1)
register("vid.crop", "Crop…", "tools", VID, _vcrop, prompt=_ask_vcrop, max_files=1)
register("vid.speed", "Speed…", "tools", VID, _speed, prompt=_ask_speed)
register("aud.speed", "Speed…", "tools", AUDIO_IN, _speed, prompt=_ask_speed)
register("aud.channels", "Channels…", "tools", AUDIO_IN, _channels, prompt=_ask_channels)
register("vid.channels", "Channels…", "tools", VID, _channels, prompt=_ask_channels)
register("vid.compress", "Compress", "tools", VID, _compress_video)
register("vid.trim", "Trim…", "tools", VID, _trim, prompt=_ask_trim)
register("vid.split", "Split…", "tools", VID, _split, prompt=_ask_split)
register("vid.merge", "Merge", "tools", VID, _merge, batch=True, min_files=2)
register("vid.mute", "Mute", "tools", VID, _v("-muted", "-c:v", "copy", "-an"))
register("vid.720", "Resize 720p", "tools", VID, _resize720)
register("vid.rotate", "Rotate 90°", "tools", VID, _rotate)
register("vid.frame", "Snapshot", "tools", VID, _thumbnail)

register("aud.compress", "Compress", "tools", AUDIO_IN, _compress_audio)
register("aud.trim", "Trim…", "tools", AUDIO_IN, _trim, prompt=_ask_trim)
register("aud.split", "Split…", "tools", AUDIO_IN, _split, prompt=_ask_split)
register("aud.merge", "Merge", "tools", AUDIO_IN, _merge, batch=True, min_files=2)
register("aud.normalize", "Normalize", "tools", AUDIO_IN, _normalize)
