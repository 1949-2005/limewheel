"""Checks the audio/video quality fixes by their results.

    python tests/test_av_quality.py
"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from limewheel.app import Ctx, execute  # noqa: E402
from limewheel.ops.base import REGISTRY  # noqa: E402
from limewheel.ops.media import ff, ffmpeg, probe, streams  # noqa: E402
from limewheel import settings as settings_mod  # noqa: E402

W = Path(tempfile.mkdtemp(prefix="av-"))
ops = {o.id: o for o in REGISTRY}
ctx = Ctx.__new__(Ctx)
ctx.app, ctx.settings = None, settings_mod.load()
ok_all = []


def info(p):
    return subprocess.run([ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True).stderr.decode("utf-8", "replace")


def check(name, fn):
    try:
        ok, msg = fn()
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    ok_all.append(ok)
    print(f"{'OK  ' if ok else 'FAIL'} {name:34} {msg}")


# Source: 10 s 720p, H.264 + AAC 48 kHz in MOV, 2-second GOP (keyframes every 60 frames)
src = W / "phone.mov"
ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=10", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=10",
   "-c:v", "libx264", "-g", "60", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", src)


def remux():
    t = time.time()
    out = execute(ops["vid.to.mp4"], [src], {}, ctx)[0]
    dt = time.time() - t
    same = "h264" in info(out) and "aac" in info(out)
    return same and dt < 2, f"MOV->MP4 in {dt:.2f}s, streams copied={same}, {out.stat().st_size // 1024} KB (src {src.stat().st_size // 1024} KB)"


def trim():
    out = execute(ops["vid.trim"], [src], {"start": 3.0, "end": 6.0}, ctx)[0]
    r = subprocess.run([ffmpeg(), "-i", str(out), "-map", "0:v", "-f", "null", "-"], capture_output=True).stderr.decode()
    frames = int(re.findall(r"frame=\s*(\d+)", r)[-1])
    adur = re.search(r"Duration: [\d:.]+", info(out))[0]
    return frames == 90, f"3.0-6.0 s -> {frames} frames (want 90), {adur}"


def speed48k():
    out = execute(ops["vid.speed"], [src], {"speed": 2.0, "pitch": False, "mute": False}, ctx)[0]
    d = probe(out)["duration"]
    return abs(d - 5.0) < 0.1, f"2x without pitch -> {d:.2f} s (want 5.00)"


def rotate():
    mp4 = execute(ops["vid.to.mp4"], [src], {}, ctx)[0]
    t = time.time()
    out = execute(ops["vid.rotate"], [mp4], {}, ctx)[0]
    dt = time.time() - t
    rot = streams(out)["rotation"]
    return abs(abs(rot) - 90) < 1 and dt < 2, f"rotation tag {rot:g} deg in {dt:.2f}s (no re-encode)"


def normalize():
    wav = W / "speech.wav"
    ff("-f", "lavfi", "-i", "sine=f=300:r=44100:d=8", "-af", "volume=0.05", wav)
    out = execute(ops["aud.normalize"], [wav], {}, ctx)[0]
    rate = streams(out)["rate"]
    r = subprocess.run([ffmpeg(), "-i", str(out), "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
                       capture_output=True).stderr.decode()
    lufs = float(re.search(r"Input Integrated:\s*(-?[\d.]+)", r)[1])
    return rate == 44100 and abs(lufs + 16) < 1.5, f"{rate} Hz kept (was 192000 before), loudness {lufs:.1f} LUFS (target -16)"


def compress_mp3_skip():
    mp3 = W / "small.mp3"
    ff("-f", "lavfi", "-i", "sine=f=500:d=5", "-c:a", "libmp3lame", "-b:a", "128k", mp3)
    try:
        execute(ops["aud.compress"], [mp3], {}, ctx)
        return False, "re-compressed an already small MP3"
    except Exception as e:
        return "Already" in str(e), str(e)


for name, fn in (("remux MOV->MP4 (lossless)", remux), ("trim frame-accurate", trim),
                 ("speed keeps 48 kHz timing", speed48k), ("rotate without re-encode", rotate),
                 ("normalize two-pass", normalize), ("compress skips small MP3", compress_mp3_skip)):
    check(name, fn)
print(f"\n{sum(ok_all)}/{len(ok_all)} passed  ({W})")
sys.exit(0 if all(ok_all) else 1)
