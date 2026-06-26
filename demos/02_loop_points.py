"""Loop points: stutter a short window, then walk it across the file.

Setting a small `loop_region` turns any slice of the buffer into a tight loop.
Moving the region while playing scans through the source material rhythmically.

Run:  uv run python demos/02_loop_points.py [--play]
Out:  demos/out/02_loop_points.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

WINDOW = 0.18  # loop length in seconds
REPEATS = 6  # times to repeat each window
STARTS = [0.5, 1.5, 2.5, 3.5]  # window start positions (seconds)


def build(samples: np.ndarray, sr: int) -> softcut.Engine:
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(samples)
    v.configure(rate=1.0, fade_time=0.004, level=0.9)
    v.play = True
    return eng


def main(play: bool = False) -> None:
    samples, sr = load_wav_mono(DATA / "m05.wav")
    eng = build(samples, sr)
    v = eng[0]

    segments = []
    for start in STARTS:
        v.loop_region = (start, start + WINDOW)
        v.cut_to(start)
        segments.append(render_seconds(eng, WINDOW * REPEATS))
        print(f"  window @ {start:g}s x{REPEATS}")

    out = np.concatenate(segments, axis=0)
    path = write_wav(OUT / "02_loop_points.wav", out, sr)
    print(f"wrote {path}")

    if play:
        from _util import play as play_live

        eng2 = build(samples, sr)
        eng2[0].loop_region = (STARTS[0], STARTS[0] + WINDOW)
        eng2[0].cut_to(STARTS[0])
        play_live(eng2, 4.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
