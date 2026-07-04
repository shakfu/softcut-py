"""Tests for the optional native OSC codec (vendored tinyosc) in ``_core``.

The native codec is opt-in at build time via the CMake option
``SOFTCUT_ENABLE_TINYOSC`` (see ``make build-tinyosc``); the default build ships
without it and uses the pure-Python OSC option (python-osc) at runtime. These
tests exercise the codec through the ``_osc_selftest`` hook when it is present
and are skipped otherwise.
"""

import pytest

from softcut import _core

pytestmark = pytest.mark.skipif(
    not getattr(_core, "HAVE_TINYOSC", False),
    reason="native OSC codec not built (enable SOFTCUT_ENABLE_TINYOSC)",
)


def test_osc_codec_roundtrips_through_extension():
    """tinyosc serialize+parse round-trips address and typed args (i/f/s)."""
    address, i, f, s = _core._osc_selftest()
    assert address == "/sc/test"
    assert i == 42
    assert f == 3.5
    assert s == "hi"
