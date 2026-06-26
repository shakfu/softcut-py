"""Rate slew: tape-stop, spin-up, and pitch glides.

`rate_slew_time` makes rate changes glide instead of jumping. Sliding the rate
to 0 is a tape-stop; sliding it back up is a spin-up; stepping between rates with
a slew gives portamento/varispeed glissando.

Run:  uv run python demos/08_tape_stop.py [--play]
Out:  build/out/08_tape_stop.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav


def build(sr: int) -> np.ndarray:
    samples, fsr = load_wav_mono(DATA / "m05.wav")
    sr = fsr
    loop = len(samples) / sr
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(samples)
    v.configure(loop_region=(0, loop), rate=1.0, fade_time=0.01, level=0.9)
    v.play = True
    v.cut_to(0.0)

    out = [render_seconds(eng, 2.0)]  # play at normal speed

    print("  tape-stop, then spin back up")
    v.rate_slew_time = 1.5
    v.rate = 0.0  # glide to a halt
    out.append(render_seconds(eng, 2.5))
    v.rate = 1.0  # glide back up to speed
    out.append(render_seconds(eng, 2.5))

    print("  pitch glides between rates")
    v.rate_slew_time = 0.4
    for rate in [1.5, 0.75, 2.0, 1.0]:
        v.rate = rate
        out.append(render_seconds(eng, 1.4))

    return np.concatenate(out)


def main(play: bool = False) -> None:
    sr = 44100
    out = build(sr)
    path = write_wav(OUT / "08_tape_stop.wav", out, sr)
    print(f"wrote {path}  ({len(out) / sr:.1f}s)")

    if play:
        from _util import play as play_live

        mono = out.mean(axis=1)
        eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
        v = eng[0]
        v.buffer = to_buffer(mono)
        v.configure(loop_region=(0, len(mono) / sr), rate=1.0)
        v.play = True
        v.cut_to(0.0)
        play_live(eng, len(mono) / sr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
