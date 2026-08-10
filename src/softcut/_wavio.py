"""WAV file I/O for softcut buffers, on the stdlib ``wave`` module alone.

softcut buffers hold ``float32`` samples in [-1, 1]. These helpers decode a WAV
into that representation and write it back, with no third-party dependency and
no numpy: ``wave`` parses the container and the per-sample conversion runs in C
(``_core._pcm_decode`` / ``_pcm_encode_s16``), because a Python loop over a few
million samples is not an option and numpy is the thing being made optional.

Reading handles 8/16/24/32-bit integer PCM; writing emits 16-bit PCM. WAV is the
only format the norns-compatible layer needs, so non-PCM/float WAV and other
containers are intentionally out of scope (``wave`` raises on them).

Buffers here are ``array.array("f")``. They satisfy the buffer protocol, so they
go straight into a voice or into any of the ``_core`` buffer helpers, and
``numpy.asarray`` wraps one without copying for callers that want an ndarray.
"""

from __future__ import annotations

import array
import wave
from pathlib import Path
from typing import Any

from softcut import _core

#: Sample widths in bytes that `_pcm_decode` understands.
_WIDTHS = (1, 2, 3, 4)


def _empty(n: int) -> array.array:
    """A zeroed float32 buffer of ``n`` samples."""
    return array.array("f", bytes(4 * max(n, 0)))


def read_wav(path: str | Path) -> tuple[array.array, int, int]:
    """Decode a WAV file to interleaved float32 samples in [-1, 1].

    Returns ``(data, channels, sample_rate)``. Channels stay interleaved rather
    than being split or summed, so a caller can pull out whichever it wants with
    `_core._buffer_extract_channel`; that mirrors what the standalone server
    does with the same audio.

    Raises:
        ValueError: If the sample width is not 1, 2, 3 or 4 bytes.
    """
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sr = w.getframerate()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if width not in _WIDTHS:
        raise ValueError(f"unsupported sample width: {width} bytes")

    data = _empty(len(raw) // width)
    _core._pcm_decode(data, raw, width)
    return data, channels, sr


def read_wav_mono(path: str | Path) -> tuple[array.array, int]:
    """Decode a WAV as a mono float32 buffer (channels averaged)."""
    data, channels, sr = read_wav(path)
    frames = len(data) // channels if channels else 0
    if channels == 1:
        return data, sr

    mono = _empty(frames)
    scratch = _empty(frames)
    for col in range(channels):
        _core._buffer_extract_channel(scratch, data, channels, col, 0, frames)
        # preserve=1, mix=1 accumulates rather than overwriting.
        _core._buffer_apply(mono, 0, scratch, 1.0, 1.0, 0)
    # No source and a preserve scales what is already there: the divide by n.
    _core._buffer_apply(mono, 0, None, 1.0 / channels, 0.0, 0, frames)
    return mono, sr


def write_wav(
    path: str | Path, data: Any, sr: int, channels: int | None = None
) -> Path:
    """Write float32 samples as 16-bit PCM. Returns the written path.

    ``data`` is anything exposing a C-contiguous float32 buffer -- an
    ``array.array``, a ``memoryview``, or a numpy array, 1-D for mono or
    ``(frames, channels)`` for multichannel. The channel count is taken from a
    2-D shape when there is one, so existing callers passing an ``(n, 2)`` array
    keep working; pass ``channels`` to say so explicitly for a flat interleaved
    buffer. Samples are clipped to [-1, 1] before quantizing, and parent
    directories are created as needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    view = memoryview(data)
    if channels is None:
        channels = view.shape[1] if view.ndim == 2 else 1
    # Flatten to 1-D float32 for the encoder; `cast` demands C-contiguity, which
    # is what the encoder needs anyway.
    flat = view.cast("B").cast("f")

    with wave.open(str(path), "wb") as w:
        w.setnchannels(int(channels))
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(_core._pcm_encode_s16(flat))
    return path
