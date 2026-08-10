"""The shared buffer arithmetic (`src/shared/buffer_ops.hpp`), bound into _core.

These primitives exist in two places by necessity -- the standalone server needs
them in C++ with no interpreter, and softcut-py has had them in numpy -- so the
tests that matter are the ones pinning the two implementations together. Every
buffer operation softcut offers (read, copy, clear) reduces to `_buffer_apply`,
so a divergence here is a divergence in all of them.

They also stand as the numpy-free path: the bindings take any C-contiguous
float32 buffer, so the assertions below run the same arithmetic over
`array.array` and `memoryview` as over an ndarray.
"""

import array

import numpy as np
import pytest

from softcut import _core

SR = 48000.0
FRAMES = 128

#: start, source length, preserve, mix, fade -- spanning the combinations the
#: buffer operations actually use: overwrite (read), blend (preserve/mix),
#: a negative destination (trimmed source head), and a write clipped by the end
#: of the buffer.
CASES = [
    (0, 64, 0.0, 1.0, 0),
    (10, 64, 0.5, 0.5, 8),
    (-5, 64, 0.0, 1.0, 4),
    (100, 64, 0.25, 0.75, 16),
    (120, 64, 0.0, 1.0, 0),
    (0, FRAMES, 1.0, 1.0, 32),
    (64, 8, 0.0, 1.0, 9),  # fade wider than the write; capped to n/2
]


def reference(dst, start, src, preserve, mix, fade):
    """The numpy implementation of the primitive, kept here on purpose.

    `softcut.norns` used to hold this and now delegates to the binding, so
    comparing against it there would be comparing the binding with itself. It
    lives in the test instead, as an independent statement of the contract:
    whatever the C++ does, it must agree with this.
    """
    out = np.array(dst, dtype=np.float32)
    src = np.asarray(src, dtype=np.float32)
    if start < 0:  # a negative destination trims the source head
        src = src[-start:]
        start = 0
    n = min(len(src), len(out) - start)
    if n <= 0:
        return out

    env = np.ones(n, dtype=np.float32)
    f = min(int(fade), n // 2)
    if f > 0:
        ramp = np.linspace(0.0, 1.0, f, endpoint=False, dtype=np.float32)
        env[:f] = ramp
        env[n - f :] = ramp[::-1]

    view = out[start : start + n]
    target = view * float(preserve) + src[:n] * float(mix)
    out[start : start + n] = view * (1.0 - env) + target * env
    return out


@pytest.mark.parametrize("start,nsrc,preserve,mix,fade", CASES)
def test_apply_matches_the_numpy_implementation(start, nsrc, preserve, mix, fade):
    rng = np.random.default_rng(abs(start) * 31 + nsrc)
    dst = rng.standard_normal(FRAMES).astype(np.float32)
    src = rng.standard_normal(nsrc).astype(np.float32)

    got = np.array(dst)
    _core._buffer_apply(got, start, src, preserve, mix, fade)

    assert got == pytest.approx(reference(dst, start, src, preserve, mix, fade))


def test_apply_clears_when_given_no_source():
    """src=None with a count is a clear -- the same primitive, mix 0."""
    dst = np.ones(FRAMES, dtype=np.float32)
    expected = reference(dst, 16, np.zeros(32, np.float32), 0.0, 0.0, 0)

    got = np.array(dst)
    _core._buffer_apply(got, 16, None, 0.0, 0.0, 0, 32)

    assert got == pytest.approx(expected)
    assert got[16:48] == pytest.approx(np.zeros(32))
    assert got[:16] == pytest.approx(np.ones(16))  # outside the range, untouched


@pytest.mark.parametrize("fade", [0, 16])
def test_clear_with_preserve_attenuates_in_place(fade):
    """src=None with preserve set scales rather than zeroing -- clear_region's blend.

    This is the one shape with no source *and* a preserve, so it exercises the
    unfaded fast path's middle branch as well as the general loop.
    """
    dst = np.full(FRAMES, 0.8, dtype=np.float32)
    expected = reference(dst, 0, np.zeros(FRAMES, np.float32), 0.25, 0.0, fade)

    got = np.array(dst)
    _core._buffer_apply(got, 0, None, 0.25, 0.0, fade, FRAMES)

    assert got == pytest.approx(expected)


def test_apply_is_a_no_op_outside_the_buffer():
    dst = np.ones(FRAMES, dtype=np.float32)
    got = np.array(dst)
    _core._buffer_apply(got, FRAMES + 10, np.zeros(8, np.float32), 0.0, 1.0, 0)
    _core._buffer_apply(got, -100, np.zeros(8, np.float32), 0.0, 1.0, 0)
    assert got == pytest.approx(dst)


def test_apply_takes_stdlib_buffers():
    """No numpy at the boundary: array.array and memoryview carry the same result."""
    values = [0.25] * FRAMES
    src = [0.75] * 32

    as_array = array.array("f", values)
    as_view = memoryview(bytearray(array.array("f", values).tobytes())).cast("f")
    as_numpy = np.array(values, dtype=np.float32)

    for buf in (as_array, as_view, as_numpy):
        _core._buffer_apply(buf, 8, array.array("f", src), 0.0, 1.0, 0)

    assert list(as_array) == pytest.approx(list(as_numpy))
    assert list(as_view) == pytest.approx(list(as_numpy))
    assert as_array[8] == pytest.approx(0.75) and as_array[7] == pytest.approx(0.25)


@pytest.mark.parametrize("col", [0, 1])
@pytest.mark.parametrize("start", [0, 5, -3, 60])
def test_extract_channel_matches_numpy_deinterleaving(col, start):
    """De-interleaving pads out-of-range frames with silence rather than refusing."""
    total = 64
    frames = np.arange(total * 2, dtype=np.float32).reshape(total, 2)

    out = np.zeros(16, dtype=np.float32)
    _core._buffer_extract_channel(out, frames.reshape(-1), 2, col, start, total)

    expected = np.array(
        [frames[start + i, col] if 0 <= start + i < total else 0.0 for i in range(16)],
        dtype=np.float32,
    )
    assert out == pytest.approx(expected)


def test_extract_channel_infers_the_frame_count():
    """total=-1 derives the frame count from the interleaved length."""
    frames = np.arange(20, dtype=np.float32)  # 10 frames of 2 channels
    out = np.zeros(10, dtype=np.float32)
    _core._buffer_extract_channel(out, frames, 2, 1, 0)
    assert out == pytest.approx(frames[1::2])
