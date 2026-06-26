"""Filters: record colouration (pre filter) and output modes (post filter).

Each voice has two state-variable filters. The *pre* filter sits on the record
path, so it colours material as it is written into the loop. The *post* filter
sits on the output and can run as lowpass / highpass / bandpass / band-reject.

Run:  uv run python demos/09_filters.py [--play]
Out:  build/out/09_filters.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

POST_MODES = [
    (
        "lowpass",
        dict(post_filter_lp=1, post_filter_hp=0, post_filter_bp=0, post_filter_br=0),
    ),
    (
        "highpass",
        dict(post_filter_lp=0, post_filter_hp=1, post_filter_bp=0, post_filter_br=0),
    ),
    (
        "bandpass",
        dict(post_filter_lp=0, post_filter_hp=0, post_filter_bp=1, post_filter_br=0),
    ),
    (
        "band-reject",
        dict(post_filter_lp=0, post_filter_hp=0, post_filter_bp=0, post_filter_br=1),
    ),
]


def record_through_pre_filter(eng, v, src, loop, fc, rq=2.0, fc_mod=1.0) -> np.ndarray:
    """Record `src` into the loop through the pre filter, return the playback."""
    v.configure(
        pre_filter_lp=1.0,
        pre_filter_hp=0.0,
        pre_filter_bp=0.0,
        pre_filter_br=0.0,
        pre_filter_dry=0.0,  # record only the filtered signal
        pre_filter_fc=fc,
        pre_filter_rq=rq,
        pre_filter_fc_mod=fc_mod,
    )
    v.pre_level = 0.0  # clean write (replace)
    v.rec = True
    v.play = True
    v.cut_to(0.0)
    render_seconds(eng, loop, input=src)  # record pass
    v.rec = False
    v.cut_to(0.0)
    return render_seconds(eng, loop)  # playback of the coloured recording


def build(sr: int) -> np.ndarray:
    src, sr = load_wav_mono(DATA / "m05.wav")
    loop = min(3.0, len(src) / sr)
    n = int(loop * sr)

    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(np.zeros(n, dtype=np.float32))
    v.configure(
        loop_region=(0, loop), rate=1.0, fade_time=0.01, rec_level=1.0, level=0.9
    )
    v.play = True

    out = []
    print("  pre filter: record bright, then record dark (lo-fi)")
    out.append(record_through_pre_filter(eng, v, src[:n], loop, fc=16000.0))
    out.append(record_through_pre_filter(eng, v, src[:n], loop, fc=500.0))

    print("  post filter modes: lowpass -> highpass -> bandpass -> band-reject")
    # bright source in the buffer, dry pre filter so we hear the post filter only
    v.configure(pre_filter_fc=16000.0)
    record_through_pre_filter(eng, v, src[:n], loop, fc=16000.0)
    v.configure(post_filter_dry=0.0, post_filter_fc=1200.0, post_filter_rq=1.2)
    for name, mix in POST_MODES:
        v.configure(**mix)
        v.cut_to(0.0)
        out.append(render_seconds(eng, 2.0))

    return np.concatenate(out)


def main(play: bool = False) -> None:
    sr = 44100
    out = build(sr)
    path = write_wav(OUT / "09_filters.wav", out, sr)
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
