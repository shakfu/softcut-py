"""Tests for the softcut nanobind extension, Engine host, and Python sugar."""

import os

import numpy as np
import pytest

import softcut
from softcut import Engine, Softcut, Voice, next_power_of_two

SR = 48000.0


def make_voice(frames: int = 65536) -> tuple[Voice, np.ndarray]:
    """A voice with a zeroed power-of-two buffer looping over its full span.

    The voice is positioned at 0 with ``cut_to`` so a head is active; softcut
    only begins reading/recording once a head has been cut to a position.
    """
    v = Voice(SR)
    buf = np.zeros(frames, dtype=np.float32)
    v.buffer = buf
    v.loop_start = 0.0
    v.loop_end = frames / SR
    v.loop = True
    v.fade_time = 0.001
    v.rate = 1.0
    v.cut_to(0.0)
    return v, buf


def sine_buffer(
    frames: int = 65536, freq: float = 220.0, amp: float = 0.5
) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq * np.arange(frames) / SR)).astype(np.float32)


# --- Voice: parameters and buffers ---------------------------------------


def test_version_and_exports():
    assert softcut.__version__ == "0.1.0"
    assert {"Voice", "Engine", "next_power_of_two"} <= set(softcut.__all__)


def test_param_roundtrip():
    v = Voice(SR)
    v.rate = 2.5
    v.loop_start = 0.25
    v.loop_end = 3.75
    v.loop = True
    v.rec_level = 0.8
    v.pre_filter_fc = 8000.0
    v.post_filter_dry = 0.5
    assert v.rate == pytest.approx(2.5)
    assert v.loop_start == pytest.approx(0.25)
    assert v.loop_end == pytest.approx(3.75)
    assert v.loop is True
    assert v.rec_level == pytest.approx(0.8)
    assert v.pre_filter_fc == pytest.approx(8000.0)
    assert v.post_filter_dry == pytest.approx(0.5)


def test_flag_roundtrip():
    v = Voice(SR)
    assert v.rec is False and v.play is False
    v.rec = True
    v.play = True
    assert v.rec is True and v.play is True


def test_level_pan_defaults_and_roundtrip():
    v = Voice(SR)
    assert v.level == pytest.approx(1.0)
    assert v.pan == pytest.approx(0.0)
    v.level = 0.4
    v.pan = -0.75
    assert v.level == pytest.approx(0.4)
    assert v.pan == pytest.approx(-0.75)


def test_sample_rate_property():
    v = Voice(SR)
    assert v.sample_rate == pytest.approx(SR)
    v.sample_rate = 44100.0
    assert v.sample_rate == pytest.approx(44100.0)


def test_buffer_roundtrip_and_identity():
    v = Voice(SR)
    buf = np.linspace(-1.0, 1.0, 1024, dtype=np.float32)
    v.buffer = buf
    assert v.buffer is buf
    np.testing.assert_array_equal(v.buffer, buf)


def test_buffer_can_be_shared_between_voices():
    buf = np.zeros(1024, dtype=np.float32)
    a, b = Voice(SR), Voice(SR)
    a.buffer = buf
    b.buffer = buf
    assert a.buffer is b.buffer


def test_buffer_rejects_wrong_dtype():
    v = Voice(SR)
    with pytest.raises(Exception):
        v.buffer = np.zeros(128, dtype=np.float64)


def test_buffer_rejects_non_power_of_two():
    v = Voice(SR)
    with pytest.raises(ValueError):
        v.buffer = np.zeros(1000, dtype=np.float32)


def test_next_power_of_two():
    assert next_power_of_two(1) == 1
    assert next_power_of_two(1000) == 1024
    assert next_power_of_two(1024) == 1024
    assert next_power_of_two(96000) == 131072


# --- Voice: DSP processing -----------------------------------------------


def test_process_shape_and_dtype():
    v, _ = make_voice()
    out = v.process(np.zeros(512, dtype=np.float32))
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.shape == (512,)


def test_silent_when_not_playing_or_recording():
    v, _ = make_voice()
    v.play = False
    v.rec = False
    out = v.process(np.ones(1024, dtype=np.float32))
    np.testing.assert_array_equal(out, np.zeros(1024, dtype=np.float32))


def test_recording_writes_into_buffer():
    v, buf = make_voice()
    v.rec = True
    v.play = True
    v.rec_level = 1.0
    v.pre_level = 0.0
    v.process(np.full(4096, 0.5, dtype=np.float32))
    assert np.any(buf != 0.0)
    assert np.abs(buf).sum() > 0.0


def test_playback_produces_output():
    v = Voice(SR)
    v.buffer = sine_buffer()
    v.loop_start, v.loop_end, v.loop = 0.0, 1.0, True
    v.rate = 1.0
    v.play = True
    v.cut_to(0.0)
    out = v.process(np.zeros(4096, dtype=np.float32))
    assert np.abs(out).sum() > 0.0


def test_position_advances():
    v, _ = make_voice()
    v.play = True
    start = v.position
    v.process(np.zeros(8192, dtype=np.float32))
    assert v.position != start


def test_actions_do_not_raise():
    v, _ = make_voice()
    v.play = True
    v.process(np.zeros(256, dtype=np.float32))
    v.cut_to(0.5)
    v.stop()
    v.reset()


# --- Voice: Pythonic sugar -----------------------------------------------


def test_configure_chains_and_sets():
    v = Voice(SR)
    result = v.configure(rate=2.0, level=0.3, pan=0.5, loop=True)
    assert result is v
    assert v.rate == pytest.approx(2.0)
    assert v.level == pytest.approx(0.3)
    assert v.pan == pytest.approx(0.5)
    assert v.loop is True


def test_loop_region_property():
    v = Voice(SR)
    v.loop_region = (0.5, 2.5)
    assert v.loop_region == (pytest.approx(0.5), pytest.approx(2.5))
    assert v.loop is True


def test_record_context_manager_toggles_rec():
    v, _ = make_voice()
    assert v.rec is False
    with v.record(at=0.0) as rv:
        assert rv is v
        assert v.rec is True
        assert v.play is True
    assert v.rec is False  # rec off on exit, keeps looping


def test_record_context_manager_off_on_exception():
    v, _ = make_voice()
    with pytest.raises(RuntimeError):
        with v.record(at=0.0):
            assert v.rec is True
            raise RuntimeError("boom")
    assert v.rec is False


def test_record_for_blocks_and_stops():
    v, buf = make_voice()
    v.rec_level = 1.0
    result = v.record_for(0.01, at=0.0)
    assert result is v
    assert v.rec is False


def test_voice_repr_contains_state():
    v = Voice(SR)
    v.configure(rate=1.5, level=0.8)
    text = repr(v)
    assert text.startswith("Voice(")
    assert "rate=1.5" in text
    assert "level=0.8" in text


# --- Engine --------------------------------------------------------------


def test_engine_is_sequence_of_voices():
    eng = Engine(voices=3, sample_rate=SR, mode="playback")
    assert len(eng) == 3
    assert all(isinstance(v, Voice) for v in eng)
    assert eng.voice(0) is eng[0]
    assert list(eng) == eng.voices


def test_engine_rejects_bad_args():
    with pytest.raises(ValueError):
        Engine(voices=0)
    with pytest.raises(ValueError):
        Engine(mode="bogus")


def test_engine_properties():
    eng = Engine(voices=2, sample_rate=SR, mode="playback", block_size=256)
    assert eng.sample_rate == pytest.approx(SR)
    assert eng.mode == "playback"
    assert eng.block_size == 256
    assert eng.running is False


def test_engine_repr():
    eng = Engine(voices=2, mode="playback")
    assert repr(eng).startswith("Engine(voices=2")


def test_engine_allocate_shared():
    eng = Engine(voices=3, sample_rate=SR, mode="playback")
    buf = eng.allocate(seconds=2.0, shared=True)
    assert buf.shape == (next_power_of_two(int(round(SR * 2.0))),)
    assert all(v.buffer is buf for v in eng)


def test_engine_allocate_per_voice():
    eng = Engine(voices=3, sample_rate=SR, mode="playback")
    bufs = eng.allocate(frames=1000, shared=False)
    assert len(bufs) == 3
    assert all(b.shape == (1024,) for b in bufs)
    assert all(v.buffer is b for v, b in zip(eng, bufs))
    assert bufs[0] is not bufs[1]


def test_engine_allocate_requires_one_of():
    eng = Engine(voices=1, mode="playback")
    with pytest.raises(ValueError):
        eng.allocate()
    with pytest.raises(ValueError):
        eng.allocate(seconds=1.0, frames=100)


def test_render_shape_and_silence():
    eng = Engine(voices=2, sample_rate=SR, mode="playback")
    eng.allocate(seconds=1.0)
    out = eng.render(np.zeros(800, dtype=np.float32))
    assert out.shape == (800, 2)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, np.zeros((800, 2), dtype=np.float32))


def test_render_rejects_non_1d():
    eng = Engine(voices=1, mode="playback")
    with pytest.raises(ValueError):
        eng.render(np.zeros((10, 2), dtype=np.float32))


def test_render_playback_and_pan():
    eng = Engine(voices=1, sample_rate=SR, mode="playback")
    v = eng[0]
    v.buffer = sine_buffer()
    v.configure(loop_region=(0, 1), rate=1.0, level=1.0, pan=0.0)
    v.play = True
    v.cut_to(0.0)

    out = eng.render(np.zeros(4096, dtype=np.float32))
    assert np.abs(out).sum() > 0.0
    # centered: left and right are equal
    np.testing.assert_allclose(out[:, 0], out[:, 1])

    v.pan = -1.0
    out = eng.render(np.zeros(4096, dtype=np.float32))
    assert np.abs(out[:, 0]).sum() > 0.0
    assert np.abs(out[:, 1]).sum() == 0.0  # hard left -> no right


def test_render_feeds_input_to_recording_voice():
    eng = Engine(voices=1, sample_rate=SR, mode="playback")
    v = eng[0]
    buf = eng.allocate(seconds=1.0)
    v.configure(loop_region=(0, 1), rate=1.0, rec_level=1.0, fade_time=0.001)
    v.rec = True
    v.play = True
    v.cut_to(0.0)
    eng.render(np.full(4096, 0.5, dtype=np.float32))
    assert np.any(buf != 0.0)


def test_engine_sync():
    eng = Engine(voices=2, sample_rate=SR, mode="playback")
    eng.allocate(seconds=2.0)
    for v in eng:
        v.configure(loop_region=(0, 2))
        v.play = True
        v.cut_to(0.0)
    eng.render(np.zeros(1024, dtype=np.float32))
    eng.sync(follow=1, lead=0, offset=0.0)  # should not raise


# --- Deprecated alias ----------------------------------------------------


def test_softcut_alias_is_deprecated():
    with pytest.warns(DeprecationWarning):
        sc = Softcut(voices=3)
    assert len(sc) == 3
    assert isinstance(sc, Engine)


# --- Live device (opt-in) ------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("SOFTCUT_TEST_AUDIO"),
    reason="set SOFTCUT_TEST_AUDIO=1 to exercise a real audio device",
)
def test_live_device_smoke():
    import time

    eng = Engine(voices=1, mode="playback", block_size=256)
    eng.allocate(seconds=1.0)
    eng[0].configure(loop_region=(0, 1))
    eng[0].play = True
    eng[0].cut_to(0.0)
    try:
        eng.start()
    except RuntimeError as e:
        pytest.skip(f"no audio device available: {e}")
    try:
        time.sleep(0.05)
        assert eng.running is True
    finally:
        eng.stop()
    assert eng.running is False
