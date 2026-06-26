"""Capture modes: one-shot record, reverse record, and record offset.

  - rec_once: record exactly one loop, then recording stops automatically -- a
    one-shot sampler. Further input is ignored.
  - reverse record: with a negative rate the head writes the input backwards,
    so playing forward afterwards reverses the captured material.
  - rec_offset: shifts the write head relative to the read head; with feedback
    recording that turns into a short delay.

Run:  uv run python demos/11_capture.py [--play]
Out:  build/out/11_capture.wav
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


def new_voice(sr: int):
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(np.zeros(int(LOOP * sr), dtype=np.float32))
    v.configure(
        loop_region=(0, LOOP), rate=1.0, fade_time=0.01, rec_level=1.0, level=0.9
    )
    v.play = True
    return eng, v


def section_rec_once(sr: int, src) -> np.ndarray:
    """Capture one loop and auto-stop; later input is ignored."""
    eng, v = new_voice(sr)
    v.pre_level = 0.0
    v.rec_once = True
    v.rec = True
    v.cut_to(0.0)
    render_seconds(eng, LOOP, input=src)  # loop 1: captured
    render_seconds(
        eng, LOOP, input=np.zeros(int(LOOP * sr), dtype=np.float32)
    )  # ignored
    v.cut_to(0.0)
    return render_seconds(eng, LOOP)  # play the one-shot


def section_reverse_record(sr: int, src) -> np.ndarray:
    """Record at a negative rate, then play forward to hear it reversed."""
    eng, v = new_voice(sr)
    v.pre_level = 0.0
    v.rec = True
    v.rate = -1.0
    v.cut_to(LOOP)  # start at the loop end, head moves backwards
    render_seconds(eng, LOOP, input=src)
    v.rec = False
    v.rate = 1.0
    v.cut_to(0.0)
    return render_seconds(eng, LOOP)  # forward playback = reversed material


def section_rec_offset(sr: int, src) -> np.ndarray:
    """Feedback recording with a large write/read offset -> a short delay."""
    eng, v = new_voice(sr)
    v.buffer[: len(src[: int(LOOP * sr)])] = src[: int(LOOP * sr)]  # preload
    v.rec_offset = 0.12  # write head trails the read head by 120 ms
    v.pre_level = 0.7  # feedback so the offset echoes build
    v.rec = True
    v.cut_to(0.0)
    out = [render_seconds(eng, LOOP) for _ in range(3)]
    return np.concatenate(out)


def build(sr: int) -> np.ndarray:
    src, sr = load_wav_mono(DATA / "m07.wav")
    src = src[: int(LOOP * sr)]
    gap = np.zeros((int(0.4 * sr), 2), dtype=np.float32)
    print("  one-shot capture (rec_once)")
    s1 = section_rec_once(sr, src)
    print("  reverse record")
    s2 = section_reverse_record(sr, src)
    print("  record offset feedback delay")
    s3 = section_rec_offset(sr, src)
    return np.concatenate([s1, gap, s2, gap, s3])


def main(play: bool = False) -> None:
    sr = 44100
    out = build(sr)
    path = write_wav(OUT / "11_capture.wav", out, sr)
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
