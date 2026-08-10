"""numpy works everywhere a buffer is taken, though nothing requires it.

softcut-py declares no dependencies. Buffers are `array.array("f")` and every
entry point takes the buffer protocol, so numpy is neither imported nor needed
by the library. But numpy is what most callers doing offline work already have,
and an ndarray satisfies that protocol, so it has to keep working everywhere a
stdlib buffer does -- as input, as a voice's buffer, as a caller-supplied `out`,
and as a zero-copy view over what softcut hands back.

That is what these tests pin down. They are skipped without numpy, which is the
supported configuration rather than a degraded one.
"""

import array

import pytest

np = pytest.importorskip("numpy")

import softcut  # noqa: E402
from softcut import _core  # noqa: E402
from softcut._wavio import read_wav, read_wav_mono, write_wav  # noqa: E402
from softcut.norns import NornsSoftcut  # noqa: E402

SR = 48000.0
FRAMES = 65536


def sine(n=FRAMES, freq=220.0, amp=0.5):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


# --- buffers -----------------------------------------------------------------


def test_a_voice_takes_an_ndarray_buffer_and_records_into_it():
    """The array stays the caller's: softcut writes into that exact memory."""
    buf = np.zeros(FRAMES, dtype=np.float32)
    v = softcut.Voice(SR)
    v.buffer = buf
    assert v.buffer is buf  # stored, not copied

    v.loop_start, v.loop_end, v.loop = 0.0, FRAMES / SR, True
    v.rec_level, v.pre_level = 1.0, 0.0
    v.rec = v.play = True
    v.cut_to(0.0)
    v.process(np.full(4096, 0.5, dtype=np.float32))

    assert np.abs(buf).sum() > 0.0  # the caller's own array holds the recording


def test_an_ndarray_and_an_array_array_buffer_behave_identically():
    """The buffer's container has no bearing on the DSP."""
    outs = []
    for make in (
        lambda: np.zeros(FRAMES, dtype=np.float32),
        lambda: array.array("f", bytes(4 * FRAMES)),
    ):
        v = softcut.Voice(SR)
        v.buffer = make()
        v.loop_start, v.loop_end, v.loop = 0.0, 1.0, True
        v.rec_level, v.rec, v.play = 1.0, True, True
        v.cut_to(0.0)
        outs.append(np.asarray(v.process(np.full(2048, 0.25, dtype=np.float32))))

    np.testing.assert_array_equal(outs[0], outs[1])


def test_allocate_returns_a_buffer_numpy_wraps_without_copying():
    eng = softcut.Engine(voices=2, sample_rate=SR, mode="playback")
    buf = eng.allocate(seconds=1.0)

    view = np.asarray(buf)
    assert view.dtype == np.float32
    view[7] = 0.5  # writing through the view writes the real buffer
    assert buf[7] == pytest.approx(0.5)


# --- render and process ------------------------------------------------------


def test_render_accepts_an_ndarray_and_reshapes_without_copying():
    eng = softcut.Engine(voices=1, sample_rate=SR, mode="playback")
    eng[0].buffer = sine()
    eng[0].configure(loop_region=(0, 1), rate=1.0, level=1.0, pan=0.0)
    eng[0].play = True
    eng[0].cut_to(0.0)

    flat = eng.render(np.zeros(4096, dtype=np.float32))
    out = np.asarray(flat).reshape(-1, eng.out_channels)

    assert out.shape == (4096, eng.out_channels)
    assert np.abs(out).sum() > 0.0
    np.testing.assert_allclose(out[:, 0], out[:, 1])  # centred


def test_render_writes_into_a_caller_supplied_ndarray():
    """An ndarray as `out` is filled in place and handed straight back."""
    eng = softcut.Engine(voices=1, sample_rate=SR, mode="playback")
    eng[0].buffer = sine()
    eng[0].configure(loop_region=(0, 1), rate=1.0)
    eng[0].play = True
    eng[0].cut_to(0.0)

    mine = np.zeros(2048 * eng.out_channels, dtype=np.float32)
    assert eng.render(np.zeros(2048, dtype=np.float32), mine) is mine
    assert np.abs(mine).sum() > 0.0


def test_render_out_must_be_flat_and_big_enough():
    eng = softcut.Engine(voices=1, sample_rate=SR, mode="playback")
    eng.allocate(seconds=1.0)

    with pytest.raises(Exception):  # 2-D: the binding takes 1-D buffers
        eng.render(np.zeros(64, np.float32), np.zeros((64, 2), np.float32))
    with pytest.raises(ValueError):
        eng.render(np.zeros(64, np.float32), np.zeros(8, np.float32))

    # ...and a flattened view of a 2-D array is the way to keep the shape
    own = np.zeros((64, eng.out_channels), dtype=np.float32)
    eng.render(np.zeros(64, np.float32), own.reshape(-1))
    assert own.shape == (64, eng.out_channels)


def test_float64_input_is_refused_rather_than_silently_converted():
    eng = softcut.Engine(voices=1, mode="playback")
    eng.allocate(seconds=1.0)
    with pytest.raises(ValueError, match="float32"):
        eng.render(np.zeros(64))  # float64


# --- the buffer primitives ---------------------------------------------------


def test_buffer_primitives_take_ndarrays():
    dst_np = np.full(128, 0.25, dtype=np.float32)
    dst_arr = array.array("f", [0.25] * 128)
    src = np.full(32, 0.75, dtype=np.float32)

    _core._buffer_apply(dst_np, 8, src, 0.0, 1.0, 0)
    _core._buffer_apply(dst_arr, 8, array.array("f", [0.75] * 32), 0.0, 1.0, 0)

    np.testing.assert_allclose(dst_np, np.asarray(dst_arr))


# --- WAV ---------------------------------------------------------------------


def test_wav_round_trip_through_numpy(tmp_path):
    """write_wav takes a 2-D ndarray and infers the channel count from it."""
    left = np.full(64, 0.5, dtype=np.float32)
    right = np.full(64, -0.25, dtype=np.float32)
    write_wav(tmp_path / "s.wav", np.stack([left, right], axis=1), int(SR))

    data, channels, sr = read_wav(tmp_path / "s.wav")
    frames = np.asarray(data).reshape(-1, channels)
    assert (channels, sr) == (2, int(SR))
    np.testing.assert_allclose(frames[:, 0], left, atol=1e-3)
    np.testing.assert_allclose(frames[:, 1], right, atol=1e-3)

    mono, _ = read_wav_mono(tmp_path / "s.wav")
    np.testing.assert_allclose(np.asarray(mono), 0.125, atol=1e-3)


# --- the norns layer ---------------------------------------------------------


def test_norns_buffers_are_writable_through_numpy(tmp_path):
    host = NornsSoftcut(sample_rate=SR, buffer_frames=2**14, mode="playback")
    view = np.asarray(host.buffers[1])
    view[:100] = 0.5  # numpy writes; the norns ops must see it

    host.buffer_copy_mono(1, 2, 0.0, 0.0, 100 / SR)
    np.testing.assert_allclose(np.asarray(host.buffers[2])[:100], 0.5, atol=1e-6)

    host.buffer_clear()
    assert np.asarray(host.buffers[1]).max() == 0.0
