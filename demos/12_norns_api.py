"""The norns softcut API, end to end -- nothing but the compatibility layer.

Imported the way a norns script does it::

    from softcut import norns as softcut

Every audio operation below is a flat, 1-based ``softcut.<fn>`` call against the
global singleton (6 voices, 2 mono buffers numbered 1/2) -- no Engine/Voice
object API. Rather than layer everything at once, the demo steps through one
feature at a time, each separated by a second of silence, so the effect of each
is clear:

  1. forward loop        -- a plain buffer loop (baseline)
  2. low-pass filter     -- the same loop through the post filter
  3. octave down         -- rate 0.5 (half speed)
  4. reverse             -- rate -1.0, head runs backwards
  5. stereo L/R          -- two voices from the two buffers, panned apart
  6. reversed copy layer -- Tier B buffer_copy_mono(reverse=1), played forward

Along the way it exercises Tier B (buffer_clear, buffer_read_stereo,
buffer_copy_mono, buffer_write_mono) and Tier A (rate, level, pan, loop /
loop_start / loop_end, position, fade_time, and the post filter).

``softcut.render`` / ``softcut.start`` are the softcut-py additions that drive
audio (norns runs its audio continuously; here we render offline, or perform the
sequence live with --play).

Run:  uv run python demos/12_norns_api.py [--play]
Out:  build/out/12_norns_api.wav          (the narrated progression, stereo)
      build/out/12_norns_api_buffer1.wav  (buffer 1, saved via the norns API)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from softcut import norns as softcut  # the norns-compatible layer
from _util import DATA, OUT, write_wav  # demo plumbing (paths + artifact dump)

SR = 48000  # the norns singleton runs at 48kHz; m04.wav is 48kHz, so no pitch shift
LOOP = 2.5  # forward loop length, seconds
SECTION = 3.0  # how long each feature plays
GAP = 1.0  # silence between features
REV_START = 3.0  # where the reversed copy lives in buffer 1
REV_DUR = 2.0  # length of the reversed copy


def load() -> None:
    """Fill the two buffers from a stereo file (Tier B)."""
    softcut.buffer_clear()
    softcut.buffer_read_stereo(str(DATA / "m04.wav"))  # L -> buffer 1, R -> buffer 2


def all_off() -> None:
    """Silence every voice (used for the gaps between features)."""
    for i in range(1, 7):
        softcut.play(i, 0)
        softcut.rec(i, 0)


# --- one setup per feature; each fully (re)configures the patch it needs -----


def s1_forward() -> None:
    """Plain forward loop of buffer 1, centred, filter bypassed."""
    all_off()
    softcut.buffer(1, 1)
    softcut.rate(1, 1.0)
    softcut.level(1, 0.85)
    softcut.pan(1, 0.0)
    softcut.loop(1, 1)
    softcut.loop_start(1, 0.0)
    softcut.loop_end(1, LOOP)
    softcut.fade_time(1, 0.01)
    softcut.post_filter_dry(1, 1.0)  # filter bypassed (all-dry)
    softcut.post_filter_lp(1, 0.0)
    softcut.position(1, 0.0)
    softcut.play(1, 1)


def s2_lowpass() -> None:
    """Same forward loop, now through the post low-pass at 800 Hz."""
    s1_forward()
    softcut.post_filter_dry(1, 0.0)  # route the voice through the filter...
    softcut.post_filter_lp(1, 1.0)  # ...as a low-pass...
    softcut.post_filter_fc(1, 800.0)  # ...at 800 Hz


def s3_octave_down() -> None:
    """Half speed -> one octave down (varispeed via rate)."""
    s1_forward()
    softcut.rate(1, 0.5)


def s4_reverse() -> None:
    """Negative rate: the head runs backwards from the loop end."""
    s1_forward()
    softcut.rate(1, -1.0)
    softcut.position(1, LOOP)  # start at the end, move backwards


def s5_stereo() -> None:
    """Two voices, buffers 1 and 2, panned hard left/right."""
    all_off()
    for i, (buf, pan) in enumerate(((1, -0.6), (2, 0.6)), start=1):
        softcut.buffer(i, buf)
        softcut.rate(i, 1.0)
        softcut.level(i, 0.8)
        softcut.pan(i, pan)
        softcut.loop(i, 1)
        softcut.loop_start(i, 0.0)
        softcut.loop_end(i, LOOP)
        softcut.fade_time(i, 0.01)
        softcut.position(i, 0.0)
        softcut.play(i, 1)


def s6_reversed_copy() -> None:
    """Tier B: copy a region reversed into buffer 1's tail, then play it forward."""
    softcut.buffer_copy_mono(1, 1, 0.0, REV_START, REV_DUR, reverse=1)
    all_off()
    softcut.buffer(1, 1)
    softcut.rate(1, 1.0)
    softcut.level(1, 0.85)
    softcut.pan(1, 0.0)
    softcut.loop(1, 1)
    softcut.loop_start(1, REV_START)
    softcut.loop_end(1, REV_START + REV_DUR)
    softcut.fade_time(1, 0.02)
    softcut.post_filter_dry(1, 1.0)
    softcut.post_filter_lp(1, 0.0)
    softcut.position(1, REV_START)
    softcut.play(1, 1)


STEPS = [
    ("1. forward loop", s1_forward),
    ("2. low-pass filter (800 Hz)", s2_lowpass),
    ("3. octave down (rate 0.5)", s3_octave_down),
    ("4. reverse (rate -1.0)", s4_reverse),
    ("5. stereo L/R (two buffers)", s5_stereo),
    ("6. reversed buffer copy", s6_reversed_copy),
]


def render_offline() -> np.ndarray:
    """Render each feature in turn, separated by a second of silence."""
    gap = np.zeros((int(GAP * SR), 2), dtype=np.float32)
    parts: list[np.ndarray] = []
    for label, setup in STEPS:
        print(f"  {label}")
        setup()
        parts.append(softcut.render(np.zeros(int(SECTION * SR), dtype=np.float32)))
        parts.append(gap)
    return np.concatenate(parts)


def perform_live() -> None:
    """Perform the same sequence on the audio device, muting between features."""
    print(f"  performing live ({len(STEPS) * (SECTION + GAP):g}s) ...")
    try:
        softcut.start()
    except RuntimeError as e:
        print(f"  (no audio device available: {e})")
        return
    try:
        for label, setup in STEPS:
            print(f"  > {label}")
            setup()
            time.sleep(SECTION)
            all_off()  # a beat of silence before the next feature
            time.sleep(GAP)
    finally:
        softcut.stop()


def main(play: bool = False) -> None:
    load()

    mix = render_offline()
    path = write_wav(OUT / "12_norns_api.wav", mix, SR)
    print(f"wrote {path}  ({len(mix) / SR:.1f}s)")

    # Tier B write: save buffer 1 (with its reversed tail) via the norns API.
    buf_path = OUT / "12_norns_api_buffer1.wav"
    buf_path.parent.mkdir(parents=True, exist_ok=True)
    softcut.buffer_write_mono(str(buf_path), 0.0, REV_START + REV_DUR, ch=1)
    print(f"wrote {buf_path}  (buffer 1 via softcut.buffer_write_mono)")

    if play:
        perform_live()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also perform the sequence live"
    )
    main(**vars(ap.parse_args()))
