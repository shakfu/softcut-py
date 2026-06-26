"""Varispeed: play one loop back at several rates, including reverse.

softcut's `rate` is a playback-speed multiplier on the buffer: 2.0 is an octave
up at half the loop time, 0.5 an octave down, negative values play in reverse.

Run:  uv run python demos/01_varispeed.py [--play]
Out:  demos/out/01_varispeed.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

RATES = [1.0, 2.0, 0.5, -1.0]
SEG = 3.0  # seconds rendered per rate


def build(samples: np.ndarray, sr: int) -> tuple[softcut.Engine, np.ndarray]:
    dur = len(samples) / sr
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(samples)
    v.configure(loop_region=(0, dur), fade_time=0.01, level=0.9)
    v.play = True
    return eng, np.array([dur], dtype=np.float64)


def main(play: bool = False) -> None:
    samples, sr = load_wav_mono(DATA / "m02.wav")
    eng, (dur,) = build(samples, sr)
    v = eng[0]

    segments = []
    for rate in RATES:
        v.rate = rate
        v.cut_to(dur if rate < 0 else 0.0)  # reverse starts at the loop end
        segments.append(render_seconds(eng, SEG))
        print(f"  rate {rate:+.1f}: rendered {SEG:g}s")

    out = np.concatenate(segments, axis=0)
    path = write_wav(OUT / "01_varispeed.wav", out, sr)
    print(f"wrote {path}")

    if play:
        from _util import play as play_live

        eng2, (d2,) = build(samples, sr)
        eng2[0].rate = 1.0
        eng2[0].cut_to(0.0)
        play_live(eng2, 4.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
