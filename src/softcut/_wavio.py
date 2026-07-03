"""WAV file I/O for softcut buffers, using only numpy and the stdlib ``wave``.

softcut buffers are numpy ``float32`` arrays in [-1, 1]. These helpers decode a
WAV into that representation and write it back, with no third-party dependency.
Reading handles 8/16/24/32-bit integer PCM; writing emits 16-bit PCM. WAV is the
only format the norns-compatible layer needs, so non-PCM/float WAV and other
containers are intentionally out of scope (``wave`` raises on them).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Decode a WAV file to a ``(frames, channels)`` float32 array in [-1, 1].

    Returns ``(data, sample_rate)``. Channels are kept separate (not summed), so
    a caller can select or spread individual channels. Handles 8-bit unsigned and
    16/24/32-bit signed little-endian integer PCM.
    """
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sr = w.getframerate()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if width == 1:  # unsigned 8-bit
        flat = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:  # packed little-endian signed 24-bit
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        flat = ints.astype(np.float32) / 8388608.0
    elif width == 4:
        flat = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    data = flat.reshape(-1, channels)
    return np.ascontiguousarray(data, dtype=np.float32), sr


def read_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Decode a WAV as a mono 1-D float32 array (channels averaged)."""
    data, sr = read_wav(path)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return np.ascontiguousarray(mono, dtype=np.float32), sr


def write_wav(path: str | Path, data: np.ndarray, sr: int) -> Path:
    """Write a float32 array (mono 1-D or ``(frames, channels)``) as 16-bit PCM.

    Samples are clipped to [-1, 1] before quantizing. Returns the written path;
    parent directories are created as needed.
    """
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
