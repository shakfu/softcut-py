"""Live looper: capture the microphone into a loop, then overdub on top.

This is the realtime, interactive demo -- it opens a duplex audio device, so it
needs a microphone and speakers (and mic permission). Unlike the offline demos,
audio runs on a background thread; the script just decides when to record.

Run:  uv run python demos/06_live_looper.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut

LOOP = 4.0


def main(loops: int = 3) -> None:
    eng = softcut.Engine(voices=1, sample_rate=48000, mode="duplex", block_size=256)
    eng.allocate(seconds=LOOP)
    v = eng[0]
    v.configure(loop_region=(0, LOOP), rate=1.0, fade_time=0.01, rec_level=1.0)

    try:
        eng.start()
    except RuntimeError as e:
        print(f"no audio device available ({e}); this demo needs live I/O.")
        return

    try:
        print(f"recording {LOOP:g}s from the mic ...")
        v.pre_level = 0.0  # first pass: clean record
        v.record_for(LOOP, at=0.0)  # blocking capture; the loop now plays

        for n in range(loops):
            print(f"overdub pass {n + 1}/{loops}: play something ...")
            v.pre_level = 0.75  # keep 75% of the existing layer
            v.record_for(LOOP, at=0.0)

        print("looping the result; Ctrl-C to stop.")
        while True:
            time.sleep(LOOP)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        print("stopped.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loops", type=int, default=3, help="number of overdub passes")
    main(**vars(ap.parse_args()))
