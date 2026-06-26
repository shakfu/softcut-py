"""Multi-voice: three files layered at different rates, panned across stereo.

The Engine mixes each mono voice to stereo using its `level` and `pan`. Here
three sources play simultaneously at different speeds and positions; render()
returns the stereo mix.

Run:  uv run python demos/04_stereo_layers.py [--play]
Out:  demos/out/04_stereo_layers.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import softcut
from _util import DATA, OUT, load_wav_mono, render_seconds, to_buffer, write_wav

# (file, rate, pan, level) -- all sources are 44.1 kHz mono
LAYERS = [
    ("m02.wav", 1.0, -0.7, 0.8),
    ("m05.wav", 0.5, 0.0, 0.7),
    ("m07.wav", 1.5, 0.7, 0.9),
]
DURATION = 8.0


def build(sr: int) -> softcut.Engine:
    eng = softcut.Engine(voices=len(LAYERS), sample_rate=sr, mode="playback")
    for (name, rate, pan, level), v in zip(LAYERS, eng):
        samples, fsr = load_wav_mono(DATA / name)
        assert fsr == sr, f"{name} sample rate {fsr} != {sr}"
        v.buffer = to_buffer(samples)
        v.configure(
            loop_region=(0, len(samples) / sr),
            rate=rate,
            pan=pan,
            level=level,
            fade_time=0.02,
        )
        v.play = True
        v.cut_to(0.0)
    return eng


def main(play: bool = False) -> None:
    sr = 44100
    eng = build(sr)
    out = render_seconds(eng, DURATION)
    path = write_wav(OUT / "04_stereo_layers.wav", out, sr)
    for name, rate, pan, level in LAYERS:
        print(f"  {name}: rate={rate:g} pan={pan:+g} level={level:g}")
    print(f"wrote {path}  ({out.shape[1]} channels)")

    if play:
        from _util import play as play_live

        play_live(build(sr), DURATION)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--play", action="store_true", help="also play live to the speakers"
    )
    main(**vars(ap.parse_args()))
