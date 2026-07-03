"""Shared helpers for the softcut demos.

Audio I/O uses only the Python standard library (``wave``) plus numpy, so the
demos run without any extra dependency. The WAV codec lives in
``softcut._wavio`` (shared with the norns compatibility layer); ``load_wav_mono``
here is the mono-summing convenience the demos use, since softcut voices are mono
and the engine mixes voices back to stereo.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

import softcut
from softcut._wavio import read_wav_mono as load_wav_mono  # noqa: F401
from softcut._wavio import write_wav  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"
OUT = ROOT / "build" / "out"  # out-of-source; cleaned by `make clean`


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
