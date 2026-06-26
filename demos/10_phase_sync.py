"""Phase: voice syncing, quantized phase, and live head-position polling.

`sync()` cuts one voice to another's head position (here a canon/round). Each
voice also reports its head position in seconds (`position`) and a quantized
phase on a grid you set with `phase_quant` (`quant_phase`) -- useful for clocking
events to the loop. This demo prints those read-outs while it renders.

Run:  uv run python demos/10_phase_sync.py [--play]
Out:  build/out/10_phase_sync.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

LOOP = 3.0
OFFSET = 0.5  # seconds the follower trails the leader (a round)


def build(sr: int) -> np.ndarray:
    src, sr = load_wav_mono(DATA / "m02.wav")
    eng = softcut.Engine(voices=2, sample_rate=sr, mode="playback")
    for i, v in enumerate(eng):
        v.buffer = to_buffer(src[: int(LOOP * sr)])
        v.configure(loop_region=(0, LOOP), rate=1.0, fade_time=0.01, level=0.7)
        v.pan = -0.6 if i == 0 else 0.6  # leader left, follower right
        v.play = True
        v.cut_to(0.0)

    lead, follow = eng[0], eng[1]
    lead.phase_quant = LOOP / 8  # report phase on an eighth-of-loop grid
    lead.phase_offset = LOOP / 16  # nudge where the grid lands
    eng.sync(follow=1, lead=0, offset=OFFSET)  # lock the round

    out = []
    last_tick = None
    # position is the live (audio-thread) head; saved_position is updated once
    # per block and is the value safe to read from another thread.
    print("    t    pos0  saved0   quant0   pos1")
    chunk = 0.25
    for k in range(int(8.0 / chunk)):
        out.append(render_seconds(eng, chunk))
        tick = round(lead.quant_phase, 3)
        if tick != last_tick:  # print only when the quantized phase advances
            print(
                f"  {k * chunk:4.2f}  {lead.position:5.2f}  {lead.saved_position:5.2f}  "
                f"{tick:6.3f}  {follow.position:5.2f}"
            )
            last_tick = tick

    return np.concatenate(out)


def main(play: bool = False) -> None:
    sr = 44100
    out = build(sr)
    path = write_wav(OUT / "10_phase_sync.wav", out, sr)
    print(f"wrote {path}  ({len(out) / sr:.1f}s, stereo round)")

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
