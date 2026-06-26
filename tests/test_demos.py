"""Tests for the demo audio helpers and an offline render through real files.

Skipped when the audio fixtures in tests/data are absent (e.g. a lean sdist).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

import softcut

DATA = Path(__file__).resolve().parent / "data"
DEMOS = Path(__file__).resolve().parent.parent / "demos"
WAVS = sorted(DATA.glob("*.wav")) if DATA.exists() else []

pytestmark = pytest.mark.skipif(
    not WAVS, reason="tests/data/*.wav fixtures not present"
)

sys.path.insert(0, str(DEMOS))
from _util import load_wav_mono, to_buffer, write_wav  # type: ignore[import-not-found]  # noqa: E402


@pytest.mark.parametrize("path", WAVS, ids=lambda p: p.name)
def test_load_wav_mono(path):
    data, sr = load_wav_mono(path)
    assert data.ndim == 1
    assert data.dtype == np.float32
    assert sr > 0
    assert np.isfinite(data).all()
    assert np.abs(data).max() <= 1.0
    assert np.any(data != 0.0)  # not silent


def test_write_read_roundtrip(tmp_path):
    sr = 44100
    original = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
    out = write_wav(tmp_path / "tone.wav", original, sr)
    back, back_sr = load_wav_mono(out)
    assert back_sr == sr
    # 16-bit quantization tolerance
    np.testing.assert_allclose(back[: len(original)], original, atol=1e-3)


def test_to_buffer_is_power_of_two_and_holds_samples():
    samples = np.linspace(-1, 1, 1000, dtype=np.float32)
    buf = to_buffer(samples)
    n = len(buf)
    assert n & (n - 1) == 0  # power of two
    assert n >= len(samples)
    np.testing.assert_array_equal(buf[: len(samples)], samples)
    assert np.all(buf[len(samples) :] == 0.0)


def test_offline_render_through_engine():
    samples, sr = load_wav_mono(WAVS[0])
    eng = softcut.Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = to_buffer(samples)
    v.configure(loop_region=(0, len(samples) / sr), rate=1.0, level=0.9)
    v.play = True
    v.cut_to(0.0)

    out = eng.render(np.zeros(sr // 2, dtype=np.float32))  # render 0.5s
    assert out.shape == (sr // 2, 2)
    assert out.dtype == np.float32
    assert np.isfinite(out).all()
    assert np.abs(out).sum() > 0.0  # playback produced sound
