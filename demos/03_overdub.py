"""Sound-on-sound: record one file into a loop, then overdub another on top.

This is softcut's signature looping gesture. With `rec` and `play` both on, the
head reads the buffer (playback) and writes (input * rec_level) mixed with the
existing content scaled by pre_level. pre_level < 1 turns it into a decaying
feedback looper; pre_level = 1 is pure overdub.

Run:  uv run python demos/03_overdub.py [--play]
Out:  demos/out/03_overdub.wav   (pass 1: layer A, pass 2: +B forming, pass 3: loop)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

LOOP = 4.0  # loop length in seconds


def main(play: bool = False) -> None:
    a, sr = load_wav_mono(DATA / "m07.wav")
    b, _ = load_wav_mono(DATA / "m02.wav")

    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(np.zeros(int(LOOP * sr), dtype=np.float32))
    v.configure(
        loop_region=(0, LOOP), rate=1.0, fade_time=0.005, rec_level=1.0, level=0.9
    )

    # pass 1: record A into the empty loop (pre=0 -> clean write)
    v.pre_level = 0.0
    v.rec = v.play = True
    v.cut_to(0.0)
    seg_a = render_seconds(eng, LOOP, input=a)

    # pass 2: overdub B, keeping 80% of the existing layer
    v.pre_level = 0.8
    v.cut_to(0.0)
    seg_ab = render_seconds(eng, LOOP, input=b)

    # pass 3: stop recording and play the layered loop back
    v.rec = False
    v.cut_to(0.0)
    seg_loop = render_seconds(eng, LOOP)

    out = np.concatenate([seg_a, seg_ab, seg_loop], axis=0)
    path = write_wav(OUT / "03_overdub.wav", out, sr)
    print("  pass 1: recorded A | pass 2: overdubbed B | pass 3: loop playback")
    print(f"wrote {path}")

    if play:
        from _util import play as play_live

        # the layered loop now lives in the buffer; play it back live
        v.cut_to(0.0)
        play_live(eng, LOOP * 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play the result live to the speakers"
    )
    main(**vars(ap.parse_args()))
