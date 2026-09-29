"""Tests for the softcut nanobind extension, Engine host, and Python sugar."""

import os
import re
from pathlib import Path

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


def rendered(eng, input) -> np.ndarray:
    """Engine.render's flat interleaved output, as the 2-D numpy view.

    `render` returns an `array.array("f")` -- the extension allocates nothing
    and imports nothing -- so numpy callers take the shape themselves. The wrap
    is zero-copy, so assertions below still inspect the real output.
    """
    out = eng.render(input)
    return np.asarray(out).reshape(-1, eng.out_channels)


def test_version_and_exports():
    assert {"Voice", "Engine", "next_power_of_two"} <= set(softcut.__all__)


def test_version_matches_the_packaging_metadata():
    """`softcut.__version__` and pyproject's version are two hand-kept copies.

    They are edited together by `make release`; this catches the case where one
    moves without the other, which is silent otherwise -- an installed package
    reporting a version it was not built as.
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.exists():  # pragma: no cover - installed-only test runs
        pytest.skip("pyproject.toml not present (testing an installed package)")

    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
    assert declared is not None, "no version in pyproject.toml"
    assert softcut.__version__ == declared.group(1)


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
    assert out.typecode == "f"  # a stdlib buffer; numpy is not a dependency
    assert len(out) == 512

    # ... and it writes into a buffer you supply, of either kind
    mine = np.empty(512, dtype=np.float32)
    assert v.process(np.zeros(512, dtype=np.float32), mine) is mine


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


def test_quant_phase_starts_at_zero_on_a_dirty_heap():
    """A voice reports a real phase before the audio thread has written one.

    `quantPhase` and `phaseQuant` in the vendored softcut-lib used to be left
    uninitialized -- softcut assumes zeroed static storage, which a host
    allocation does not give it -- so a freshly constructed voice read whatever
    the recycled block held, and `updateQuantPhase` divided by a garbage
    quantum. The garbage only appears once the allocator has dirty blocks to
    hand back, hence the churn first.
    """
    churn = [np.full(1 << 16, 0xFF, dtype=np.uint8) for _ in range(64)]
    for used in churn:
        used[:] = 0xFF
    del churn

    for _ in range(8):
        voice = Voice(SR)
        assert voice.quant_phase == 0.0
        assert voice.position == 0.0


def test_reset_restores_the_reported_phase():
    """reset() puts the phase mirrors back, so a reset voice is not still there."""
    v, _ = make_voice()
    v.phase_quant = 0.25
    v.play = True
    v.process(np.zeros(1 << 15, dtype=np.float32))
    assert v.quant_phase > 0.0  # the head moved and the poll would report it

    v.reset()
    assert v.quant_phase == 0.0


def test_quant_phase_tracks_the_quantum():
    """With a quantum set, the reported phase is a multiple of it."""
    v, _ = make_voice()
    v.phase_quant = 0.1
    v.play = True
    v.process(np.zeros(int(0.45 * SR), dtype=np.float32))
    quantized = v.quant_phase
    assert quantized == pytest.approx(0.4, abs=1e-6)
    assert quantized <= v.position


def test_reset_restores_the_parameter_read_backs():
    """reset() puts the Python-visible parameters back, not just the DSP.

    The mirrors exist because softcut-lib's parameters are write-only. They were
    seeded with reset()'s defaults at construction but never restored by reset()
    itself, so a reset voice kept reporting its pre-reset settings -- and
    `/softcut/reset` over OSC left every read-back stale.
    """
    v, _ = make_voice()
    v.play = v.rec = True
    v.rate = 2.0
    v.level = 0.25
    v.post_filter_dry = 0.0
    v.pre_filter_fc = 400.0

    v.reset()

    assert v.play is False and v.rec is False
    assert v.rate == pytest.approx(1.0)
    assert v.post_filter_dry == pytest.approx(1.0)
    assert v.pre_filter_fc == pytest.approx(16000.0)
    assert v.fade_time == pytest.approx(0.1)  # upstream: sch.init() overrides 0.01
    # level is an engine mix scalar, not a softcut param, and reset() is the
    # DSP's; it is deliberately left alone.
    assert v.level == pytest.approx(0.25)


# --- Voice: read-backs report DSP state -------------------------------------
# softcut-lib changes rec, rec_once and fade_time itself; the mirrors miss it.


def _rec_once_voice(loop: float = 0.25) -> Voice:
    v, _ = make_voice()
    v.loop_start, v.loop_end = 0.0, loop
    v.rec_level, v.pre_level = 1.0, 0.0
    v.play = True
    v.rec_once = True
    v.rec = True
    v.cut_to(0.0)
    return v


def test_rec_once_reads_true_until_its_pass_completes():
    v = _rec_once_voice()
    v.process(np.full(int(0.1 * SR), 0.5, dtype=np.float32))
    assert v.rec_once is True
    assert v.rec is True


def test_rec_once_clears_rec_and_rec_once_after_one_pass():
    v = _rec_once_voice()
    v.process(np.full(int(0.6 * SR), 0.5, dtype=np.float32))
    assert v.rec_once is False
    assert v.rec is False


def test_rec_off_cancels_an_armed_rec_once():
    v = _rec_once_voice()
    v.process(np.full(int(0.1 * SR), 0.5, dtype=np.float32))
    v.rec = False
    assert v.rec_once is False


def test_rec_once_read_backs_track_the_audio_thread():
    """Live: a set reads back at once, and the pass's end is seen too."""
    import time

    eng = Engine(voices=1, sample_rate=SR, mode="playback", null_device=True)
    v = eng[0]
    eng.allocate(seconds=1.0)
    v.configure(loop_region=(0, 0.1), rec_level=1.0, pre_level=0.0)
    v.play = True
    v.cut_to(0.0)
    with eng:
        v.rec_once = True
        assert v.rec_once is True and v.rec is True  # queued, not yet applied
        deadline = time.monotonic() + 2.0
        while v.rec_once and time.monotonic() < deadline:
            time.sleep(0.01)
        assert v.rec_once is False
        assert v.rec is False
        v.rec = True
        assert v.rec is True


def _crossfade_seconds(v: Voice) -> float:
    """Time for output to fall to silence after a cut from ones into zeros."""
    n = 1 << 17
    buf = np.zeros(n, dtype=np.float32)
    buf[: n // 2] = 1.0
    v.buffer = buf
    v.loop_start, v.loop_end, v.loop = 0.0, n / SR, True
    v.rate = 1.0
    v.play = True
    v.cut_to(0.1)
    v.process(np.zeros(4800, dtype=np.float32))
    v.cut_to(2.0)  # into the zero half
    out = np.asarray(v.process(np.zeros(16384, dtype=np.float32)))
    return int(np.argmax(out <= 0.001)) / SR


def test_fade_time_reports_the_crossfade_a_new_voice_uses():
    v = Voice(SR)
    assert _crossfade_seconds(v) == pytest.approx(v.fade_time, rel=0.01)


def test_fade_time_reports_the_crossfade_a_reset_voice_uses():
    v = Voice(SR)
    v.fade_time = 0.05
    v.reset()
    assert _crossfade_seconds(v) == pytest.approx(v.fade_time, rel=0.01)


# --- Voice: quirks ----------------------------------------------------------
# "upstream" matches softcut-lib (and norns) sample for sample; "fixed" corrects
# its defects, as softcut-rs `Quirks::Fixed` does.


def _recorded_gain(v: Voice) -> float:
    """Buffer value per unit of DC input, recorded at unity rec level."""
    buf = np.zeros(65536, dtype=np.float32)
    v.buffer = buf
    v.loop_start, v.loop_end, v.loop = 0.0, 65536 / SR, True
    v.rec_level, v.pre_level = 1.0, 0.0
    v.rec = v.play = True
    v.cut_to(0.0)
    v.process(np.full(int(0.3 * SR), 0.1, dtype=np.float32))
    return float(buf[int(0.2 * SR)]) / 0.1  # past a 0.1 s record fade-in


def test_quirks_default_to_upstream():
    assert Voice(SR).quirks == "upstream"
    assert all(v.quirks == "upstream" for v in Engine(voices=2, mode="playback"))


def test_upstream_quirks_record_polarity_inverted():
    # SoftClip's gain of 1.2 applies below its knee in both modes.
    assert _recorded_gain(Voice(SR, quirks="upstream")) == pytest.approx(-1.2, rel=1e-3)


def test_fixed_quirks_record_with_input_polarity():
    assert _recorded_gain(Voice(SR, quirks="fixed")) == pytest.approx(1.2, rel=1e-3)


def test_fixed_quirks_crossfade_a_new_voice_over_10ms():
    v = Voice(SR, quirks="fixed")
    assert v.fade_time == pytest.approx(0.01)
    assert _crossfade_seconds(v) == pytest.approx(0.01, rel=0.01)


def test_fixed_quirks_crossfade_a_reset_voice_over_10ms():
    v = Voice(SR, quirks="fixed")
    v.fade_time = 0.05
    v.reset()
    assert v.fade_time == pytest.approx(0.01)
    assert _crossfade_seconds(v) == pytest.approx(0.01, rel=0.01)


def test_reset_keeps_quirks():
    v = Voice(SR, quirks="fixed")
    v.reset()
    assert v.quirks == "fixed"


def test_quirks_are_read_only():
    v = Voice(SR)
    with pytest.raises(AttributeError):
        v.quirks = "fixed"  # type: ignore[misc]


def test_quirks_rejects_unknown_mode():
    with pytest.raises(ValueError):
        Voice(SR, quirks="bogus")
    with pytest.raises(ValueError):
        Engine(voices=1, mode="playback", quirks="bogus")


def test_engine_passes_quirks_to_every_voice():
    eng = Engine(voices=3, mode="playback", quirks="fixed")
    assert all(v.quirks == "fixed" for v in eng)


# --- Voice: crossfade curves and heads --------------------------------------


def test_fade_curve_defaults_roundtrip_and_reset():
    v = Voice(SR)
    assert (v.rec_fade_shape, v.pre_fade_shape) == ("raised", "linear")
    assert v.rec_delay_ratio == pytest.approx(1 / 128)
    assert v.pre_window_ratio == pytest.approx(1 / 8)
    v.configure(
        rec_fade_shape="sine",
        pre_fade_shape="raised",
        rec_delay_ratio=0.25,
        pre_window_ratio=0.5,
    )
    assert (v.rec_fade_shape, v.pre_fade_shape) == ("sine", "raised")
    assert (v.rec_delay_ratio, v.pre_window_ratio) == (0.25, 0.5)
    v.reset()
    assert (v.rec_fade_shape, v.pre_fade_shape) == ("raised", "linear")
    assert v.pre_window_ratio == pytest.approx(1 / 8)


def test_fade_shape_rejects_unknown_names():
    v = Voice(SR)
    with pytest.raises(ValueError):
        v.rec_fade_shape = "cosine"
    assert v.rec_fade_shape == "raised"


def _overdub_across_cuts(v: Voice, **curves: object) -> np.ndarray:
    """Buffer after recording over existing content through two crossfades."""
    buf = np.full(65536, 0.5, dtype=np.float32)
    v.buffer = buf
    v.configure(loop_region=(0, 1), rec_level=1.0, pre_level=0.0, **curves)
    v.fade_time = 0.02
    v.rec = v.play = True
    v.cut_to(0.1)
    v.process(np.full(9600, 0.25, dtype=np.float32))
    v.cut_to(0.5)
    v.process(np.full(9600, 0.25, dtype=np.float32))
    return buf.copy()


def test_fade_curves_change_what_a_crossfade_records():
    base = _overdub_across_cuts(Voice(SR))
    for curves in (
        {"rec_fade_shape": "sine"},
        {"pre_fade_shape": "sine"},
        {"rec_delay_ratio": 0.5},
        {"pre_window_ratio": 0.5},
    ):
        assert not np.array_equal(_overdub_across_cuts(Voice(SR), **curves), base), (
            curves
        )


@pytest.mark.parametrize("ratio", [5.0, -1.0, float("nan")])
def test_out_of_range_fade_ratios_are_clamped(ratio):
    # Upstream indexes its 1001-point tables with the unclamped ratio.
    out = _overdub_across_cuts(Voice(SR), rec_delay_ratio=ratio, pre_window_ratio=ratio)
    assert np.isfinite(out).all()


def test_upstream_quirks_ignore_a_raised_pre_curve_under_another_rec_curve():
    # The rec shape is set first: upstream checks it when building the pre curve.
    linear = {"rec_fade_shape": "linear"}
    raised_pre = {"rec_fade_shape": "linear", "pre_fade_shape": "raised"}
    np.testing.assert_array_equal(
        _overdub_across_cuts(Voice(SR, quirks="upstream"), **raised_pre),
        _overdub_across_cuts(Voice(SR, quirks="upstream"), **linear),
    )
    assert not np.array_equal(
        _overdub_across_cuts(Voice(SR, quirks="fixed"), **raised_pre),
        _overdub_across_cuts(Voice(SR, quirks="fixed"), **linear),
    )


def test_heads_follow_a_crossfade():
    v, _ = make_voice()
    v.fade_time = 0.02
    v.play = True
    v.process(np.zeros(512, dtype=np.float32))

    def by_role():
        a, b = v.heads
        assert a.active != b.active
        return (b, a) if a.active else (a, b)  # (fading out, fading in)

    out, into = by_role()
    assert (into.fade, into.gain, out.fade) == (1.0, 1.0, 0.0)

    v.cut_to(0.5)
    v.process(np.zeros(240, dtype=np.float32))  # a quarter of the 960-frame fade
    out, into = by_role()
    assert into.fade == pytest.approx(0.25, abs=0.01)
    assert out.fade == pytest.approx(0.75, abs=0.01)
    assert into.position == pytest.approx(0.5 + 240 / SR, abs=1e-4)
    assert into.gain == pytest.approx(np.sin(into.fade * np.pi / 2))

    v.process(np.zeros(960, dtype=np.float32))
    out, into = by_role()
    assert (into.fade, out.fade) == (1.0, 0.0)


def test_heads_read_while_the_engine_runs():
    import time

    eng = Engine(voices=1, sample_rate=SR, mode="playback", null_device=True)
    v = eng[0]
    eng.allocate(seconds=1.0)
    v.configure(loop_region=(0, 1))
    v.play = True
    v.cut_to(0.0)
    with eng:
        time.sleep(0.1)
        heads = v.heads
    assert sum(h.active for h in heads) == 1
    assert max(h.position for h in heads) > 0.0


def test_dropped_commands_starts_at_zero_and_is_read_only():
    """The overflow counter is the visible half of the queue's drop policy.

    A full queue drops the update and counts it rather than applying it on the
    calling thread, which would race the audio thread reading the same field.
    Nonzero here means control changes were lost.
    """
    v, _ = make_voice()
    assert v.dropped_commands == 0
    v.rate = 2.0  # no engine: applied directly, nothing queued
    assert v.dropped_commands == 0
    with pytest.raises(AttributeError):
        v.dropped_commands = 1


def test_a_burst_of_live_parameter_changes_is_not_dropped():
    """The bounded retry should absorb a realistic control burst.

    This is the case the drop policy exists for: with the device running, every
    set goes through the queue. If this starts failing, either the queue is too
    small or the retry too short for the burst rates callers actually use.
    """
    import time

    eng = Engine(voices=1, sample_rate=SR, null_device=True)
    eng.allocate(seconds=1.0)
    eng.start()
    try:
        for i in range(5000):
            eng[0].rate = 1.0 + (i % 8) * 0.01
        time.sleep(0.1)
    finally:
        eng.stop()

    assert eng[0].dropped_commands == 0


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
    # A stdlib float32 buffer, not an ndarray: numpy is optional now.
    assert buf.typecode == "f"
    assert len(buf) == next_power_of_two(int(round(SR * 2.0)))
    assert all(v.buffer is buf for v in eng)


def test_engine_allocate_per_voice():
    eng = Engine(voices=3, sample_rate=SR, mode="playback")
    bufs = eng.allocate(frames=1000, shared=False)
    assert len(bufs) == 3
    assert all(len(b) == 1024 for b in bufs)
    assert all(v.buffer is b for v, b in zip(eng, bufs))
    assert bufs[0] is not bufs[1]


def test_render_to_writes_a_wav_the_engine_describes(tmp_path):
    """render_to takes the sample rate and channel count from the engine.

    Those two are exactly what a caller kept having to restate, and getting
    `channels` wrong writes a file of the wrong length rather than failing.
    """
    eng = Engine(voices=1, sample_rate=SR, mode="playback")
    eng[0].buffer = sine_buffer()  # play existing material: a read lap needs no
    eng[0].configure(loop_region=(0, 1), rate=1.0)  # prior record lap to sound
    eng[0].play = True
    eng[0].cut_to(0.0)

    path = eng.render_to(tmp_path / "out.wav", np.zeros(4096, dtype=np.float32))

    data, channels, sr = softcut.read_wav(path)
    assert channels == eng.out_channels
    assert sr == int(SR)
    assert len(data) == 4096 * eng.out_channels
    assert max(abs(x) for x in data) > 0.0


def test_render_takes_seconds_instead_of_a_silent_input():
    """`seconds` is the common offline case: play material, feed nothing in."""

    def fresh():
        eng = Engine(voices=1, sample_rate=SR, mode="playback")
        eng[0].buffer = sine_buffer()
        eng[0].configure(loop_region=(0, 1), rate=1.0)
        eng[0].play = True
        eng[0].cut_to(0.0)
        return eng

    # A separate engine each time: filter state persists across renders, so
    # reusing one would compare a cold pass against a warm one.
    by_seconds = np.asarray(fresh().render(seconds=0.25))
    by_buffer = np.asarray(fresh().render(np.zeros(int(0.25 * SR), dtype=np.float32)))

    assert len(by_seconds) == int(0.25 * SR) * 2
    np.testing.assert_array_equal(by_seconds, by_buffer)


def test_render_wants_exactly_one_of_input_or_seconds():
    eng = Engine(voices=1, mode="playback")
    eng.allocate(seconds=1.0)
    with pytest.raises(ValueError, match="exactly one"):
        eng.render()
    with pytest.raises(ValueError, match="exactly one"):
        eng.render(np.zeros(8, dtype=np.float32), seconds=1.0)


def test_render_to_takes_seconds_too(tmp_path):
    eng = Engine(voices=1, sample_rate=SR, mode="playback")
    eng[0].buffer = sine_buffer()
    eng[0].configure(loop_region=(0, 1), rate=1.0)
    eng[0].play = True
    eng[0].cut_to(0.0)

    path = eng.render_to(tmp_path / "s.wav", seconds=0.5)
    data, channels, sr = softcut.read_wav(path)
    assert len(data) // channels == int(0.5 * SR)
    assert sr == int(SR)
    assert max(abs(x) for x in data) > 0.0


def test_wav_helpers_are_public():
    """They back the norns layer and every demo; they are not internals."""
    assert {"read_wav", "read_wav_mono", "write_wav"} <= set(softcut.__all__)
    from softcut import _wavio

    assert softcut.write_wav is _wavio.write_wav
    assert softcut.read_wav is _wavio.read_wav
    assert softcut.read_wav_mono is _wavio.read_wav_mono


def test_engine_allocate_requires_one_of():
    eng = Engine(voices=1, mode="playback")
    with pytest.raises(ValueError):
        eng.allocate()
    with pytest.raises(ValueError):
        eng.allocate(seconds=1.0, frames=100)


def test_render_shape_and_silence():
    eng = Engine(voices=2, sample_rate=SR, mode="playback")
    eng.allocate(seconds=1.0)
    out = rendered(eng, np.zeros(800, dtype=np.float32))
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

    out = rendered(eng, np.zeros(4096, dtype=np.float32))
    assert np.abs(out).sum() > 0.0
    # centered: left and right are equal
    np.testing.assert_allclose(out[:, 0], out[:, 1])

    v.pan = -1.0
    out = rendered(eng, np.zeros(4096, dtype=np.float32))
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


def _record_pass(v, eng, sr, signal):
    """Record one full loop of `signal` into voice `v`; return the buffer."""
    v.cut_to(0.0)
    eng.render(np.asarray(signal, dtype=np.float32))
    return np.asarray(v.buffer)


def test_pre_level_zero_replaces_content():
    """pre_level=0 erases existing buffer content (destructive replace)."""
    sr = 48000
    eng = Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = np.zeros(65536, dtype=np.float32)
    loop = 65536 / sr
    v.configure(loop_region=(0, loop), rate=1.0, rec_level=1.0, fade_time=0.001)
    v.rec = v.play = True

    first = np.full(65536, 0.5, dtype=np.float32)
    v.pre_level = 0.0
    _record_pass(v, eng, sr, first)
    energy_after_first = np.abs(v.buffer).sum()
    assert energy_after_first > 0.0

    # replace with silence at pre_level=0 -> buffer is wiped
    v.pre_level = 0.0
    _record_pass(v, eng, sr, np.zeros(65536, dtype=np.float32))
    # most of the loop should now be near zero (allow fade-region residue)
    mid = np.asarray(v.buffer)[8000:60000]
    assert np.abs(mid).max() < 0.05 * np.abs(first).max()


def test_pre_level_feedback_decays():
    """pre_level<1 with silent input multiplies content down each pass."""
    sr = 48000
    eng = Engine(voices=1, sample_rate=sr, mode="playback")
    v = eng[0]
    v.buffer = 0.6 * np.ones(65536, dtype=np.float32)
    loop = 65536 / sr
    v.configure(loop_region=(0, loop), rate=1.0, rec_level=1.0, fade_time=0.001)
    v.rec = v.play = True
    v.pre_level = 0.5  # halve each pass

    def loop_rms():
        return float(
            np.sqrt((np.asarray(v.buffer)[8000:60000].astype(float) ** 2).mean())
        )

    levels = [loop_rms()]
    for _ in range(3):
        _record_pass(v, eng, sr, np.zeros(65536, dtype=np.float32))
        levels.append(loop_rms())
    # strictly decreasing toward silence
    assert all(levels[i + 1] < levels[i] for i in range(len(levels) - 1))
    assert levels[-1] < 0.3 * levels[0]


def test_engine_sync():
    eng = Engine(voices=2, sample_rate=SR, mode="playback")
    eng.allocate(seconds=2.0)
    for v in eng:
        v.configure(loop_region=(0, 2))
        v.play = True
        v.cut_to(0.0)
    eng.render(np.zeros(1024, dtype=np.float32))
    eng.sync(follow=1, lead=0, offset=0.0)  # should not raise


# --- Routing, feedback, and devices --------------------------------------


def _recording_voice(eng, vi, sr, n):
    buf = np.zeros(n, dtype=np.float32)
    v = eng[vi]
    v.buffer = buf
    v.configure(
        loop_region=(0, n / sr), rate=1.0, rec_level=1.0, pre_level=0.0, fade_time=0.001
    )
    v.rec = True
    v.play = True
    v.cut_to(0.0)
    return buf


def test_input_gain_default_and_roundtrip():
    v = Voice(SR)
    assert v.input_gain == pytest.approx(1.0)
    v.input_gain = 0.25
    assert v.input_gain == pytest.approx(0.25)


def test_input_gain_scales_external_input():
    sr, n = 48000, 65536
    eng = Engine(voices=1, sample_rate=sr, mode="playback")
    buf = _recording_voice(eng, 0, sr, n)

    eng[0].input_gain = 0.0  # gate the input off
    eng.render(np.full(8192, 0.5, dtype=np.float32))
    assert np.abs(buf).sum() == 0.0

    buf[:] = 0.0
    eng[0].input_gain = 1.0
    eng[0].cut_to(0.0)
    eng.render(np.full(8192, 0.5, dtype=np.float32))
    assert np.abs(buf).sum() > 0.0


def test_feedback_default_zero_and_roundtrip():
    eng = Engine(voices=2, mode="playback")
    assert eng.feedback(0, 1) == pytest.approx(0.0)
    assert eng.feedback(1, 0, 0.5) is eng  # setter returns self
    assert eng.feedback(1, 0) == pytest.approx(0.5)


def test_feedback_routes_one_voice_into_another():
    sr, n = 48000, 65536
    eng = Engine(voices=2, sample_rate=sr, mode="playback")
    # voice 0 plays a tone
    eng[0].buffer = (0.5 * np.sin(2 * np.pi * 200 * np.arange(n) / sr)).astype(
        np.float32
    )
    eng[0].configure(loop_region=(0, n / sr), rate=1.0)
    eng[0].play = True
    eng[0].cut_to(0.0)
    # voice 1 records whatever reaches its input (external is silent here)
    buf1 = _recording_voice(eng, 1, sr, n)

    eng.render(np.zeros(8192, dtype=np.float32))
    assert np.abs(buf1).sum() == 0.0  # no feedback yet -> nothing to record

    eng.feedback(0, 1, 1.0)
    eng.render(np.zeros(8192, dtype=np.float32))
    assert np.abs(buf1).sum() > 0.0  # voice 0's output recorded into voice 1


def test_feedback_index_out_of_range():
    eng = Engine(voices=2, mode="playback")
    with pytest.raises(Exception):
        eng.feedback(0, 5, 1.0)
    with pytest.raises(Exception):
        eng.feedback(-1, 0, 1.0)


def test_engine_accepts_device_args():
    eng = Engine(voices=1, mode="playback", output_device=-1, input_device=-1)
    assert len(eng) == 1


def test_list_devices_structure():
    try:
        devices = softcut.list_devices()
    except RuntimeError:
        pytest.skip("no audio context available")
    assert isinstance(devices, list)
    for d in devices:
        assert {"index", "name", "type", "is_default"} <= set(d)
        assert d["type"] in ("playback", "capture")


# --- Deprecated alias ----------------------------------------------------


def test_softcut_alias_is_deprecated():
    with pytest.warns(DeprecationWarning):
        sc = Softcut(voices=3)
    assert len(sc) == 3
    assert isinstance(sc, Engine)


# --- Running device ------------------------------------------------------
#
# These drive the real callback on miniaudio's null backend (silence in, output
# discarded), so the audio thread, the command queue and the start/stop
# lifecycle are covered without hardware. `SOFTCUT_TEST_AUDIO=1` adds the one
# test below that needs a real device.


def test_live_device_smoke():
    import time

    eng = Engine(voices=1, mode="playback", block_size=256, null_device=True)
    eng.allocate(seconds=1.0)
    eng[0].configure(loop_region=(0, 1))
    eng[0].play = True
    eng[0].cut_to(0.0)
    eng.start()
    try:
        time.sleep(0.05)
        assert eng.running is True
    finally:
        eng.stop()
    assert eng.running is False


def test_command_queue_applies_param_while_running():
    """A param set while the device runs is applied on the audio thread."""
    import time

    sr, n = 48000, 65536
    eng = Engine(
        voices=1, sample_rate=sr, mode="playback", block_size=256, null_device=True
    )
    eng[0].buffer = (0.2 * np.sin(2 * np.pi * 200 * np.arange(n) / sr)).astype(
        np.float32
    )
    eng[0].configure(loop_region=(0, n / sr), rate=1.0)
    eng[0].play = True
    eng[0].cut_to(0.0)
    eng.start()
    try:
        time.sleep(0.05)
        eng[0].rate = 3.0  # enqueued; applied on the audio thread
        time.sleep(0.1)
    finally:
        eng.stop()  # drains any remaining commands

    # the queued rate change took effect: the head now advances ~3x real time
    p0 = eng[0].position
    eng.render(np.zeros(int(0.1 * sr), dtype=np.float32))
    assert eng[0].position - p0 > 0.2


def test_setters_racing_repeated_start_stop_stay_on_the_queue():
    """Setters hammering the engine across start/stop cycles must not corrupt it.

    The window this guards: a setter reads "not running" and applies on its own
    thread, while the callback the flag had not yet announced is already live.
    The engine survives, keeps its restart behaviour, and the last value written
    is the one that is read back.
    """
    import threading
    import time

    eng = Engine(
        voices=1, sample_rate=SR, mode="playback", block_size=64, null_device=True
    )
    eng.allocate(seconds=1.0)
    eng[0].configure(loop_region=(0, 1))
    eng[0].play = True

    stop_setting = threading.Event()

    def hammer():
        i = 0
        while not stop_setting.is_set():
            i += 1
            eng[0].rate = 1.0 + (i % 16) * 0.01
            eng[0].pre_level = (i % 4) * 0.25
            eng[0].cut_to((i % 8) * 0.1)

    t = threading.Thread(target=hammer, daemon=True)
    t.start()
    try:
        for _ in range(20):
            eng.start()
            assert eng.running is True
            time.sleep(0.005)
            eng.stop()
            assert eng.running is False
    finally:
        stop_setting.set()
        t.join(timeout=5.0)
        assert not t.is_alive()

    # Quiet now: a set with the device stopped still lands, and reads back.
    eng[0].rate = 0.5
    assert eng[0].rate == pytest.approx(0.5)
    eng.start()
    try:
        time.sleep(0.02)
        assert eng.running is True
    finally:
        eng.stop()


def test_changing_buffer_length_while_running_is_refused():
    """A different frame count under a live callback is an out-of-bounds write.

    ReadWriteHead::setBuffer stores the pointer and the count separately, once
    per subhead, so the audio thread can read a new count against an old
    pointer -- and it pokes as well as peeks.
    """
    eng = Engine(
        voices=1, sample_rate=SR, mode="playback", block_size=64, null_device=True
    )
    buf = eng.allocate(seconds=1.0)
    eng.start()
    try:
        with pytest.raises(RuntimeError, match="buffer length while the engine"):
            eng[0].buffer = np.zeros(len(buf) * 2, dtype=np.float32)
        with pytest.raises(RuntimeError, match="buffer length while the engine"):
            eng.allocate(seconds=0.25)
        # refused, not half-applied: the voice still holds the original array
        assert eng[0].buffer is buf
    finally:
        eng.stop()

    # a different length is allowed again once stopped
    eng[0].buffer = np.zeros(4096, dtype=np.float32)
    assert len(eng[0].buffer) == 4096


def test_swapping_to_a_same_length_buffer_while_running_is_queued():
    """The norns operation: softcut.buffer(voice, b) between two equal buffers.

    Only the pointer moves, so it goes through the command queue like any other
    DSP change and both subheads move together at a block boundary.
    """
    import time

    sr, n = 48000, 65536
    eng = Engine(
        voices=1, sample_rate=sr, mode="playback", block_size=64, null_device=True
    )
    a = np.zeros(n, dtype=np.float32)
    b = np.full(n, 0.5, dtype=np.float32)
    eng[0].buffer = a
    eng[0].configure(loop_region=(0, n / sr), rate=1.0)
    eng[0].play = True
    eng[0].cut_to(0.0)
    eng.start()
    try:
        time.sleep(0.02)
        eng[0].buffer = b  # queued, applied on the audio thread
        assert eng[0].buffer is b
        time.sleep(0.02)
        assert eng[0].dropped_commands == 0
    finally:
        eng.stop()

    # the swap reached the DSP: rendering now reads b's samples, not a's zeros
    out = eng.render(np.zeros(256, dtype=np.float32))
    assert np.max(np.abs(np.asarray(out))) > 0.1


@pytest.mark.skipif(
    not os.environ.get("SOFTCUT_TEST_AUDIO"),
    reason="set SOFTCUT_TEST_AUDIO=1 to exercise a real audio device",
)
def test_real_device_smoke():
    """The one case the null backend cannot stand in for: actual hardware."""
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
