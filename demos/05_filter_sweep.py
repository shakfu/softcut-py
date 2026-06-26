"""Filter sweep: play a loop while automating the post filter cutoff.

Each voice has a state-variable filter on its output (the post filter). Here we
enable its lowpass and sweep the cutoff across the render by processing in small
chunks and changing `post_filter_fc` between them -- the head position carries
over, so the result is one continuous filter sweep.

Run:  uv run python demos/05_filter_sweep.py [--play]
Out:  demos/out/05_filter_sweep.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

DURATION = 8.0
CHUNK = 0.05  # seconds between cutoff updates
FC_LO, FC_HI = 250.0, 9000.0


def build(samples: np.ndarray, sr: int) -> softcut.Engine:
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(samples)
    v.configure(loop_region=(0, len(samples) / sr), rate=1.0, fade_time=0.02, level=0.9)
    # pure lowpass on the output: enable lp, mute the dry path, sharpen resonance
    v.configure(post_filter_lp=1.0, post_filter_dry=0.0, post_filter_rq=0.6)
    v.play = True
    v.cut_to(0.0)
    return eng


def main(play: bool = False) -> None:
    samples, sr = load_wav_mono(DATA / "m08.wav")
    eng = build(samples, sr)
    v = eng[0]

    n_chunks = int(round(DURATION / CHUNK))
    segments = []
    for i in range(n_chunks):
        # triangle sweep lo -> hi -> lo, mapped exponentially (musical)
        tri = 1.0 - abs(2.0 * i / (n_chunks - 1) - 1.0)
        v.post_filter_fc = float(FC_LO * (FC_HI / FC_LO) ** tri)
        segments.append(render_seconds(eng, CHUNK))

    out = np.concatenate(segments, axis=0)
    path = write_wav(OUT / "05_filter_sweep.wav", out, sr)
    print(
        f"  swept post-filter cutoff {FC_LO:g}->{FC_HI:g}->{FC_LO:g} Hz over {DURATION:g}s"
    )
    print(f"wrote {path}")

    if play:
        from _util import play as play_live

        play_live(build(samples, sr), DURATION)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
