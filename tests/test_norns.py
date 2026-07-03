"""Tests for the norns-compatible API layer (softcut.norns) and _wavio.

Tier A (attribute passthrough) and Tier B (buffer/disk ops in numpy + stdlib
WAV). All offline: a small buffer, ``mode="playback"``, no audio device.
"""

from __future__ import annotations

import numpy as np
import pytest

from softcut import norns
from softcut._wavio import read_wav, read_wav_mono, write_wav

SR = 48000.0


@pytest.fixture
def sc():
    # Small power-of-two buffer keeps assertions cheap; playback mode never
    # touches an audio device.
    return norns.NornsSoftcut(sample_rate=SR, voices=6, buffer_frames=4096, mode="playback")


def sec(frames: int) -> float:
    return frames / SR


# --- _wavio round-trips --------------------------------------------------


def test_wavio_mono_roundtrip(tmp_path):
    data = np.linspace(-0.9, 0.9, 500, dtype=np.float32)
    p = write_wav(tmp_path / "m.wav", data, int(SR))
    back, sr = read_wav_mono(p)
    assert sr == int(SR)
    assert back.dtype == np.float32
    assert np.allclose(back, data, atol=1e-3)


def test_wavio_stereo_keeps_channels(tmp_path):
    left = np.full(64, 0.5, dtype=np.float32)
    right = np.full(64, -0.5, dtype=np.float32)
    write_wav(tmp_path / "s.wav", np.stack([left, right], axis=1), int(SR))
    data, sr = read_wav(tmp_path / "s.wav")
    assert data.shape == (64, 2)
    assert np.allclose(data[:, 0], 0.5, atol=1e-3)
    assert np.allclose(data[:, 1], -0.5, atol=1e-3)


def test_wavio_clips_out_of_range(tmp_path):
    data = np.array([2.0, -2.0, 0.0], dtype=np.float32)
    write_wav(tmp_path / "c.wav", data, int(SR))
    back, _ = read_wav_mono(tmp_path / "c.wav")
    assert back[0] <= 1.0 and back[1] >= -1.0
    assert abs(back[0] - 1.0) < 1e-3 and abs(back[1] + 1.0) < 1e-3


# --- Tier A: attribute passthrough ---------------------------------------


def test_float_passthrough(sc):
    sc.rate(1, 1.5)
    sc.level(2, 0.8)
    sc.pan(3, -0.25)
    sc.pre_filter_fc(1, 1200.0)
    sc.post_filter_dry(1, 0.5)
    sc.fade_time(4, 0.02)
    assert sc.engine[0].rate == 1.5
    assert abs(sc.engine[1].level - 0.8) < 1e-6
    assert abs(sc.engine[2].pan + 0.25) < 1e-6
    assert sc.engine[0].pre_filter_fc == 1200.0
    assert abs(sc.engine[0].post_filter_dry - 0.5) < 1e-6
    assert abs(sc.engine[3].fade_time - 0.02) < 1e-6


def test_bool_passthrough_coerces_0_1(sc):
    sc.play(1, 1)
    sc.loop(1, 0)
    sc.rec(2, 1)
    assert sc.engine[0].play is True
    assert sc.engine[0].loop is False
    assert sc.engine[1].rec is True


def test_indexing_is_one_based(sc):
    sc.rate(1, 3.0)
    assert sc.engine[0].rate == 3.0
    with pytest.raises(IndexError):
        sc.rate(0, 1.0)
    with pytest.raises(IndexError):
        sc.rate(7, 1.0)


def test_position_maps_to_cut_to(sc):
    # cut_to sets the play head; position() should not raise and should target
    # the requested time within the loop.
    sc.loop_start(1, 0.0)
    sc.loop_end(1, sec(2048))
    sc.position(1, sec(100))
    assert 0.0 <= sc.engine[0].position <= sec(2048)


def test_buffer_assignment(sc):
    sc.buffer(4, 2)
    assert sc.engine[3].buffer is sc.buffers[2]
    sc.buffer(4, 1)
    assert sc.engine[3].buffer is sc.buffers[1]
    with pytest.raises(ValueError):
        sc.buffer(4, 3)


def test_level_cut_cut_sets_feedback(sc):
    sc.level_cut_cut(1, 2, 0.4)
    assert abs(sc.engine.feedback(0, 1) - 0.4) < 1e-6


def test_voice_sync(sc):
    sc.loop_start(2, 0.0)
    sc.loop_end(2, sec(2048))
    sc.position(2, sec(500))
    sc.voice_sync(1, 2, 0.0)  # voice 1 follows voice 2
    assert abs(sc.engine[0].position - sc.engine[1].position) < 1e-3


def test_reset_restores_default_routing(sc):
    sc.buffer(3, 2)
    sc.rate(3, 9.0)
    sc.reset()
    assert sc.engine[2].buffer is sc.buffers[1]  # back to buffer 1


def test_module_proxy_reaches_default_singleton():
    norns.rate(1, 2.5)
    assert norns._default.engine[0].rate == 2.5
    with pytest.raises(AttributeError):
        norns.definitely_not_a_function  # noqa: B018


# --- Tier B: buffer operations -------------------------------------------


def test_buffer_clear(sc):
    for b in (1, 2):
        sc.buffers[b][:] = 1.0
    sc.buffer_clear()
    assert not sc.buffers[1].any()
    assert not sc.buffers[2].any()


def test_buffer_clear_channel(sc):
    sc.buffers[1][:] = 1.0
    sc.buffers[2][:] = 1.0
    sc.buffer_clear_channel(2)
    assert sc.buffers[1].all()
    assert not sc.buffers[2].any()


def test_clear_region_preserve(sc):
    sc.buffers[1][:] = 1.0
    sc.buffer_clear_region_channel(1, 0.0, sec(8), preserve=0.25)
    assert np.allclose(sc.buffers[1][:8], 0.25)
    assert np.allclose(sc.buffers[1][8:16], 1.0)  # outside region untouched


def test_clear_region_both_buffers(sc):
    sc.buffers[1][:] = 1.0
    sc.buffers[2][:] = 1.0
    sc.buffer_clear_region(0.0, sec(4))
    assert np.allclose(sc.buffers[1][:4], 0.0)
    assert np.allclose(sc.buffers[2][:4], 0.0)


def test_copy_mono_basic(sc):
    b = sc.buffers[1]
    b[:] = 0.0
    b[100:110] = 0.5
    sc.buffer_copy_mono(1, 1, sec(100), sec(200), sec(10))
    assert np.allclose(b[200:210], 0.5)


def test_copy_mono_reverse(sc):
    b = sc.buffers[1]
    b[:] = 0.0
    b[0:4] = [0.1, 0.2, 0.3, 0.4]
    sc.buffer_copy_mono(1, 1, 0.0, sec(500), sec(4), reverse=1)
    assert np.allclose(b[500:504], [0.4, 0.3, 0.2, 0.1])


def test_copy_mono_overlap_no_alias(sc):
    # Overlapping shift within one buffer must copy the original source, not the
    # partially-written destination.
    b = sc.buffers[1]
    b[:] = 0.0
    b[0:8] = np.arange(1, 9, dtype=np.float32)
    sc.buffer_copy_mono(1, 1, 0.0, sec(4), sec(8))
    assert np.allclose(b[4:12], np.arange(1, 9, dtype=np.float32))


def test_copy_stereo(sc):
    sc.buffers[1][:] = 0.0
    sc.buffers[2][:] = 0.0
    sc.buffers[1][0:4] = 0.7
    sc.buffers[2][0:4] = -0.7
    sc.buffer_copy_stereo(0.0, sec(100), sec(4))
    assert np.allclose(sc.buffers[1][100:104], 0.7)
    assert np.allclose(sc.buffers[2][100:104], -0.7)


def test_copy_preserve_blend(sc):
    b = sc.buffers[1]
    b[:] = 0.0
    b[0:4] = 0.5  # source
    b[100:104] = 1.0  # destination pre-fill
    sc.buffer_copy_mono(1, 1, 0.0, sec(100), sec(4), preserve=0.5)
    # dst = dst*preserve + src*mix(=1) = 1.0*0.5 + 0.5 = 1.0
    assert np.allclose(b[100:104], 1.0)


def test_read_write_mono_roundtrip(sc, tmp_path):
    b1 = sc.buffers[1]
    b2 = sc.buffers[2]
    tone = (0.5 * np.sin(np.linspace(0, 4 * np.pi, 480))).astype(np.float32)
    b1[:480] = tone
    sc.buffer_write_mono(tmp_path / "m.wav", 0.0, sec(480), ch=1)
    b2[:] = 0.0
    sc.buffer_read_mono(tmp_path / "m.wav", ch_dst=2)
    assert np.allclose(b2[:480], tone, atol=1e-3)


def test_read_write_stereo_roundtrip(sc, tmp_path):
    tone = (0.5 * np.sin(np.linspace(0, 4 * np.pi, 480))).astype(np.float32)
    sc.buffers[1][:480] = tone
    sc.buffers[2][:480] = -tone
    sc.buffer_write_stereo(tmp_path / "s.wav", 0.0, sec(480))
    sc.buffers[1][:] = 0.0
    sc.buffers[2][:] = 0.0
    sc.buffer_read_stereo(tmp_path / "s.wav")
    assert np.allclose(sc.buffers[1][:480], tone, atol=1e-3)
    assert np.allclose(sc.buffers[2][:480], -tone, atol=1e-3)


def test_read_mono_preserve_mix(sc, tmp_path):
    src = np.full(480, 0.5, dtype=np.float32)
    write_wav(tmp_path / "x.wav", src, int(SR))
    b2 = sc.buffers[2]
    b2[:480] = 1.0
    sc.buffer_read_mono(tmp_path / "x.wav", ch_dst=2, preserve=0.5, mix=0.5)
    # dst = 1.0*0.5 + 0.5*0.5 = 0.75
    assert np.allclose(b2[:480], 0.75, atol=1e-3)


def test_read_offsets_and_dur(sc, tmp_path):
    ramp = np.arange(1000, dtype=np.float32) / 1000.0
    write_wav(tmp_path / "r.wav", ramp, int(SR))
    b1 = sc.buffers[1]
    b1[:] = 0.0
    # read 10 frames starting at source frame 500 into dest frame 20
    sc.buffer_read_mono(tmp_path / "r.wav", start_src=sec(500), start_dst=sec(20), dur=sec(10))
    assert np.allclose(b1[20:30], ramp[500:510], atol=1e-3)
    assert not b1[:20].any()  # nothing written before the dest offset


def test_ops_do_not_reallocate(sc):
    # The thread-safety contract: buffer ops mutate in place, keeping the same
    # array object the voices reference.
    before = sc.buffers[1]
    sc.buffer_clear()
    sc.buffer_copy_mono(1, 1, 0.0, sec(10), sec(4))
    sc.buffer_clear_region(0.0, sec(2))
    assert sc.buffers[1] is before
    assert sc.engine[0].buffer is before
