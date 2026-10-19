"""Wheel sounds: a soft, short tick when a petal lights up. Synthesised here (no sound files),
written once to the settings folder and played asynchronously with the Windows sound API."""
from __future__ import annotations

import ctypes
import io
import math
import random
import struct
import time
import wave
from pathlib import Path

RATE = 44100
_files: dict[str, Path] = {}
_last = 0.0


def _bandpass(x: list[float], f0: float, q: float) -> list[float]:
    """RBJ biquad band-pass (constant 0 dB peak gain)."""
    w = 2 * math.pi * f0 / RATE
    alpha = math.sin(w) / (2 * q)
    b0, b2, a0, a1, a2 = alpha, -alpha, 1 + alpha, -2 * math.cos(w), 1 - alpha
    y, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
    for v in x:
        o = (b0 * v + b2 * x2 - a1 * y1 - a2 * y2) / a0
        x2, x1, y2, y1 = x1, v, y1, o
        y.append(o)
    return y


def _tick(ms: float = 60.0, peak: float = 0.15) -> bytes:
    """A crisp UI tick: a ~2 ms burst of bright noise (energy mostly 8-16 kHz) with a short
    -20 dB tail, a little 1-4 kHz body and a faint low thump. Mono content, stereo file."""
    n = int(RATE * ms / 1000)
    rnd = random.Random(11)
    noise = [rnd.random() * 2 - 1 for _ in range(n)]
    bright = _bandpass(noise, 10500.0, 1.0)
    body = _bandpass(noise, 1800.0, 0.8)
    t0 = 0.006                                          # leave 6 ms of near-silence like a pre-roll
    s = []
    for i in range(n):
        t = i / RATE - t0
        if t < 0:
            env = 0.06 * math.exp(t / 0.002)               # tiny swell into the hit
            s.append(bright[i] * env)
            continue
        hit = math.exp(-t / 0.0016)                         # main click, ~2 ms
        tail = 0.12 * math.exp(-t / 0.007)                  # soft decay to ~20 ms
        thump = 0.35 * math.sin(2 * math.pi * 150 * t) * math.exp(-t / 0.004)
        s.append(bright[i] * (hit + tail) + 0.55 * body[i] * hit + thump * hit ** 0.3 * 0.6)
    k = peak / (max(abs(v) for v in s) or 1)
    frames = bytearray()
    for i, v in enumerate(s):
        fade = min(1.0, (n - i) / (RATE * 0.004))
        q = int(max(-1.0, min(1.0, v * k * fade)) * 32767)
        frames += struct.pack("<hh", q, q)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _path(name: str) -> Path | None:
    """The sound file to play. A wav you put in the settings folder as `<name>-custom.wav`
    (for example highlight-custom.wav) replaces the built-in tick."""
    if name in _files:
        return _files[name]
    try:
        from .settings import DIR
        DIR.mkdir(parents=True, exist_ok=True)
        custom = DIR / f"{name}-custom.wav"
        if custom.exists():
            _files[name] = custom
            return custom
        p = DIR / f"{name}.wav"
        data = {"highlight": _tick}[name]()
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)
        _files[name] = p
        return p
    except Exception:
        return None


class _WaveOut:
    """A waveOut device opened once and kept open: starts a sound in a few ms, where
    PlaySound re-opens the device every time and lags 50-100 ms behind the petal."""

    class FMT(ctypes.Structure):
        _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                    ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                    ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                    ("cbSize", ctypes.c_ushort)]

    class HDR(ctypes.Structure):
        pass

    HDR._fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint),
                    ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_size_t),
                    ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                    ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

    def __init__(self, path: Path):
        with wave.open(str(path)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
                raise ValueError("needs 16-bit PCM")
            ch, rate, self.pcm = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
        self.mm = ctypes.windll.winmm
        fmt = self.FMT(1, ch, rate, rate * ch * 2, ch * 2, 16, 0)
        self.h = ctypes.c_void_p()
        if self.mm.waveOutOpen(ctypes.byref(self.h), ctypes.c_uint(0xFFFFFFFF), ctypes.byref(fmt), 0, 0, 0):
            raise OSError("waveOutOpen failed")
        self.buf = ctypes.create_string_buffer(self.pcm, len(self.pcm))
        self.hdr = self.HDR()
        self.prepared = False

    def play(self):
        mm, hdr, size = self.mm, ctypes.byref(self.hdr), ctypes.sizeof(self.HDR)
        mm.waveOutReset(self.h)                        # cut the previous tick, like the site does
        if self.prepared:
            mm.waveOutUnprepareHeader(self.h, hdr, size)
        self.hdr.lpData = ctypes.cast(self.buf, ctypes.c_void_p).value
        self.hdr.dwBufferLength = len(self.pcm)
        self.hdr.dwFlags = 0
        self.prepared = mm.waveOutPrepareHeader(self.h, hdr, size) == 0
        if self.prepared:
            mm.waveOutWrite(self.h, hdr, size)


_players: dict[str, object] = {}


def play(name: str = "highlight"):
    """Fire and forget; at most one tick every 25 ms so fast sweeps don't buzz."""
    global _last
    now = time.monotonic()
    if name == "highlight" and now - _last < 0.025:
        return
    _last = now
    p = _path(name)
    if p is None:
        return
    try:
        if name not in _players:
            try:
                _players[name] = _WaveOut(p)
            except Exception:
                _players[name] = None
        player = _players[name]
        if player is not None:
            player.play()
            return
        import winsound
        winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass