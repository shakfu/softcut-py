"""Shared helpers for the softcut demos.

Audio I/O uses only the Python standard library (``wave``) plus numpy, so the
demos run without any extra dependency. softcut voices are mono, so files are
summed to mono on load; the engine mixes voices back to stereo.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

import softcut

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"
OUT = ROOT / "build" / "out"  # out-of-source; cleaned by `make clean`


def load_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Load a WAV file as a mono float32 array in [-1, 1]; returns (data, sr).

    Handles 8/16/24/32-bit integer PCM and averages channels to mono.
    """
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sr = w.getframerate()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if width == 1:  # unsigned 8-bit
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:  # packed little-endian signed 24-bit
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), sr


def write_wav(path: str | Path, data: np.ndarray, sr: int) -> Path:
    """Write a float32 array (mono 1-D or interleaved (n, ch)) as 16-bit PCM."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(data, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    ints = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[1])
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(ints.tobytes())
    return path


def to_buffer(samples: np.ndarray) -> np.ndarray:
    """Place mono samples into a zeroed power-of-two float32 softcut buffer."""
    n = softcut.next_power_of_two(len(samples))
    buf = np.zeros(n, dtype=np.float32)
    buf[: len(samples)] = samples
    return buf


def render_seconds(
    engine: softcut.Engine, seconds: float, input: np.ndarray | None = None
) -> np.ndarray:
    """Render ``seconds`` of output. ``input`` (mono) defaults to silence.

    Voice head positions persist across calls, so several renders concatenate
    into continuous audio.
    """
    n = int(round(seconds * engine.sample_rate))
    if input is None:
        input = np.zeros(n, dtype=np.float32)
    else:
        input = np.asarray(input, dtype=np.float32)[:n]
        if len(input) < n:
            input = np.concatenate([input, np.zeros(n - len(input), dtype=np.float32)])
    return engine.render(input)


def play(engine: softcut.Engine, seconds: float) -> None:
    """Open the device, run for ``seconds``, then stop (for live --play mode)."""
    try:
        engine.start()
    except RuntimeError as e:
        print(f"  (no audio device available: {e})")
        return
    print(f"  playing live for {seconds:g}s ...")
    try:
        time.sleep(seconds)
    finally:
        engine.stop()
