"""Overdub, replace, partial-replace, and Frippertronics-style decay.

Softcut records with `buffer = buffer * pre_level + input * rec_level` at the
write head. That single rule covers a lot of looping technique:

  - pre_level = 1, rec_level = 1  -> overdub (sum new on top of old)
  - pre_level = 0, rec_level = 1  -> replace (erase old, write new)
  - automating pre/rec with a slew -> partial replace that fades in and out
  - pre_level < 1, recording continuously -> feedback decay: every pass the old
    material is multiplied down while new material is layered in, the way a
    Frippertronics tape loop slowly dissolves and rebuilds.

Each section pre-loads its first layer straight into the buffer (so the loop is
audible immediately) and then transforms it by recording on top. The demo plays
three labelled sections in sequence.

Run:  uv run python demos/07_frippertronics.py [--play]
Out:  build/out/07_frippertronics.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav


def make_voice(sr: int, loop: float, preload: np.ndarray | None = None):
    """A one-voice engine looping `loop` seconds, optionally pre-loaded."""
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    n = int(loop * sr)
    buf = to_buffer(np.zeros(n, dtype=np.float32))
    if preload is not None:
        buf[:n] = preload[:n]
    v.buffer = buf
    v.configure(
        loop_region=(0, loop), rate=1.0, fade_time=0.01, rec_level=1.0, level=0.9
    )
    v.play = True
    v.cut_to(0.0)
    return eng, v


def section_overdub_then_replace(sr: int, a, b, c, loop: float = 2.5) -> np.ndarray:
    """Loop A, overdub B on top, then replace everything with C."""
    eng, v = make_voice(sr, loop, preload=a)
    out = [render_seconds(eng, loop)]  # hear A

    v.pre_level = 1.0  # overdub: keep A, add B
    v.rec = True
    v.cut_to(0.0)
    out.append(render_seconds(eng, loop, input=b))
    v.rec = False
    v.cut_to(0.0)
    out.append(render_seconds(eng, loop))  # hear A + B

    v.pre_level = 0.0  # replace: erase the layers, write only C
    v.rec = True
    v.cut_to(0.0)
    out.append(render_seconds(eng, loop, input=c))
    v.rec = False
    v.cut_to(0.0)
    out.append(render_seconds(eng, loop))  # hear C alone
    return np.concatenate(out)


def section_partial_replace(sr: int, base, newmat, loop: float = 3.0) -> np.ndarray:
    """Replace only the middle of the loop, fading the new material in and out."""
    eng, v = make_voice(sr, loop, preload=base)
    out = [render_seconds(eng, loop)]  # hear the base loop

    # Drive rec/pre levels with a slew so the windowed replace crossfades at its
    # edges instead of clicking. recFlag stays on for the whole pass.
    v.rec = True
    v.rec_pre_slew_time = 0.08
    w0, w1 = loop * 0.35, loop * 0.65
    chunk = 0.02
    v.cut_to(0.0)
    t = 0.0
    while t < loop - 1e-9:
        inside = w0 <= t < w1
        v.rec_level = 1.0 if inside else 0.0
        v.pre_level = 0.0 if inside else 1.0  # keep old outside, swap to new inside
        i = int(t * sr)
        out.append(render_seconds(eng, chunk, input=newmat[i : i + int(chunk * sr)]))
        t += chunk

    v.rec = False
    v.rec_level = 1.0
    v.cut_to(0.0)
    out.append(render_seconds(eng, loop))  # hear the partially-replaced loop
    return np.concatenate(out)


def section_frippertronics(
    sr, seed, phrase, loop: float = 2.0, passes: int = 9
) -> np.ndarray:
    """Loop a seed, then let it decay (pre<1) while new phrases drift in."""
    eng, v = make_voice(sr, loop, preload=seed)
    out = [render_seconds(eng, loop)]  # hear the seed

    v.rec = True
    v.pre_level = 0.82  # each pass multiplies the old content down
    for p in range(passes):
        inp = phrase if p in (1, 4) else None  # new material on a couple of passes
        v.cut_to(0.0)
        out.append(render_seconds(eng, loop, input=inp))
    return np.concatenate(out)


def silence(sr: int, seconds: float) -> np.ndarray:
    return np.zeros((int(seconds * sr), 2), dtype=np.float32)


def build(sr: int) -> np.ndarray:
    a, _ = load_wav_mono(DATA / "m07.wav")
    b, _ = load_wav_mono(DATA / "m02.wav")
    c, _ = load_wav_mono(DATA / "m05.wav")
    base, _ = load_wav_mono(DATA / "m08.wav")
    seed, _ = load_wav_mono(DATA / "m05.wav")
    phrase, _ = load_wav_mono(DATA / "m07.wav")

    gap = silence(sr, 0.4)
    print("  section 1: loop A -> overdub B -> replace with C")
    s1 = section_overdub_then_replace(sr, a, b, c)
    print("  section 2: partial replace (middle of the loop, fades in/out)")
    s2 = section_partial_replace(sr, base, b)
    print("  section 3: Frippertronics decay (loop + overdubs degrade over time)")
    s3 = section_frippertronics(sr, seed, phrase)
    return np.concatenate([s1, gap, s2, gap, s3])


def main(play: bool = False) -> None:
    sr = 44100
    out = build(sr)
    path = write_wav(OUT / "07_frippertronics.wav", out, sr)
    print(f"wrote {path}  ({len(out) / sr:.1f}s)")

    if play:
        from _util import play as play_live

        # replay the full rendered mix from a single pre-loaded buffer
        mono = out.mean(axis=1)
        eng, v = make_voice(sr, len(mono) / sr, preload=mono)
        play_live(eng, len(mono) / sr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
