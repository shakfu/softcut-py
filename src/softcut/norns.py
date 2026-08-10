"""A norns-compatible ``softcut`` API over softcut-py's object core.

Import it under the name norns scripts expect::

    from softcut import norns as softcut

    softcut.buffer_clear()
    softcut.buffer_read_mono("loop.wav", ch_dst=1)
    softcut.rate(1, 1.0)
    softcut.level(1, 0.8)
    softcut.loop(1, 1)
    softcut.loop_start(1, 0.0)
    softcut.loop_end(1, 4.0)
    softcut.play(1, 1)

The module mirrors the flat, 1-based, singleton norns
`softcut <https://monome.org/docs/norns/api/modules/softcut.html>`_ namespace:
6 voices indexed from 1, and 2 global mono buffers numbered 1 and 2. Every call
delegates to a private :class:`softcut.Engine`; a module-level ``__getattr__``
proxies each name to that singleton, so ``softcut.<fn>`` works for the whole
surface without per-name wiring.

This implements Tiers A (attribute passthrough) and B (buffer/disk operations).
The sample-level arithmetic behind Tier B lives in ``src/shared/buffer_ops.hpp``
and is shared with the standalone server; file I/O is the stdlib ``wave``
module plus that same C conversion. Phase polling (Tier C) and the slew/routing gaps that need core
changes (Tier D) are not implemented here; see ``docs/dev/norns-api.md``.

Thread-safety rule (matches norns): buffer operations write into the existing
arrays in place and never reallocate, so they are safe against a running audio
thread. Assigning a whole new buffer array is only safe while stopped.
"""

from __future__ import annotations

import array
from pathlib import Path
from typing import Any

from softcut import Engine, next_power_of_two
from softcut import _core
from softcut._wavio import read_wav, write_wav

# norns' hardware layout: 6 voices, 2 global mono buffers. The buffer length
# matches norns' softcut buffer (2**24 frames, ~349s at 48kHz). The allocation is
# zeroed pages, so it does not cost resident memory until written.
_DEFAULT_VOICES = 6
_DEFAULT_BUFFER_FRAMES = 1 << 24
_BUFFERS = (1, 2)


# Trivial passthroughs: norns function name -> Voice attribute. Generated onto
# the class below to avoid ~30 identical one-liners. Filters, slews, phase and
# level params all fall here.
_FLOAT_PARAMS = {
    "rate": "rate",
    "level": "level",
    "pan": "pan",
    "rec_level": "rec_level",
    "pre_level": "pre_level",
    "loop_start": "loop_start",
    "loop_end": "loop_end",
    "fade_time": "fade_time",
    "rec_offset": "rec_offset",
    "rate_slew_time": "rate_slew_time",
    "recpre_slew_time": "rec_pre_slew_time",
    "phase_quant": "phase_quant",
    "phase_offset": "phase_offset",
    "pre_filter_fc": "pre_filter_fc",
    "pre_filter_fc_mod": "pre_filter_fc_mod",
    "pre_filter_rq": "pre_filter_rq",
    "pre_filter_lp": "pre_filter_lp",
    "pre_filter_hp": "pre_filter_hp",
    "pre_filter_bp": "pre_filter_bp",
    "pre_filter_br": "pre_filter_br",
    "pre_filter_dry": "pre_filter_dry",
    "post_filter_fc": "post_filter_fc",
    "post_filter_rq": "post_filter_rq",
    "post_filter_lp": "post_filter_lp",
    "post_filter_hp": "post_filter_hp",
    "post_filter_bp": "post_filter_bp",
    "post_filter_br": "post_filter_br",
    "post_filter_dry": "post_filter_dry",
}

# norns passes 0/1 for these; coerce to bool for the object API.
_BOOL_PARAMS = {
    "play": "play",
    "rec": "rec",
    "loop": "loop",
    "rec_once": "rec_once",
}


class NornsSoftcut:
    """The singleton facade. Holds one :class:`Engine` and the 2 global buffers.

    Public methods reproduce the norns ``softcut`` functions (1-based voices,
    buffers numbered 1/2). Instantiate directly for tests with a small buffer;
    the module exposes a process-wide default instance via ``__getattr__``.
    """

    def __init__(
        self,
        sample_rate: float = 48000.0,
        voices: int = _DEFAULT_VOICES,
        buffer_frames: int = _DEFAULT_BUFFER_FRAMES,
        mode: str = "duplex",
    ) -> None:
        self.sample_rate = float(sample_rate)
        self._n = int(voices)
        self._eng = Engine(voices=voices, sample_rate=sample_rate, mode=mode)
        n = next_power_of_two(int(buffer_frames))
        self._buf = {b: array.array("f", bytes(4 * n)) for b in _BUFFERS}
        self._assign: dict[int, int] = {}
        # norns default: every voice reads/writes buffer 1 until reassigned.
        for i in range(1, self._n + 1):
            self.buffer(i, 1)

    # --- voice access ----------------------------------------------------

    def _v(self, i: int):
        idx = int(i) - 1
        if idx < 0 or idx >= self._n:
            raise IndexError(f"voice {i} out of range 1..{self._n}")
        return self._eng[idx]

    # --- Tier A: non-trivial passthroughs --------------------------------

    def position(self, i: int, pos: float) -> None:
        """Jump voice ``i``'s play head to ``pos`` seconds (norns ``position``)."""
        self._v(i).cut_to(float(pos))

    def buffer(self, i: int, b: int) -> None:
        """Assign global buffer ``b`` (1 or 2) to voice ``i``."""
        b = int(b)
        if b not in self._buf:
            raise ValueError(f"buffer must be one of {sorted(self._buf)}, got {b}")
        self._v(i).buffer = self._buf[b]
        self._assign[int(i)] = b

    def voice_sync(self, dst: int, src: int, offset: float = 0.0) -> None:
        """Cut voice ``dst`` to voice ``src``'s position plus ``offset`` seconds."""
        self._eng.sync(int(dst) - 1, int(src) - 1, float(offset))

    def level_cut_cut(self, src: int, dst: int, amount: float) -> None:
        """Route voice ``src``'s output into voice ``dst``'s input (feedback)."""
        self._eng.feedback(int(src) - 1, int(dst) - 1, float(amount))

    def reset(self) -> None:
        """Reset all voices to defaults and restore the default buffer routing.

        Buffer *contents* are left intact (use ``buffer_clear`` to zero them),
        matching norns, which resets voice parameters rather than audio.
        """
        for v in self._eng:
            v.reset()
        for i in range(1, self._n + 1):
            self.buffer(i, 1)

    # --- Tier B: buffer / disk operations --------------------------------

    def _apply(
        self,
        buf: Any,
        start_frame: int,
        src: Any,
        preserve: float,
        mix: float,
        fade_frames: int,
    ) -> None:
        """In-place ``dst = dst*(1-env) + (dst*preserve + src*mix)*env`` write.

        Writes the smaller of ``len(src)`` and the buffer tail, never reallocating.
        ``env`` applies the optional edge fade. This one primitive backs read,
        copy and clear: read passes preserve/mix, copy passes mix=1, clear passes
        no ``src`` at all with mix=0.

        The arithmetic lives in ``src/shared/buffer_ops.hpp`` and is shared with
        the standalone server, so the two hosts cannot drift; it also drops the
        GIL while looping, which a numpy expression of the same shape does not.
        ``buf`` and ``src`` are passed through as they are -- any C-contiguous
        float32 buffer, ndarray or not -- so callers hand over what they already
        have rather than paying for a conversion.
        """
        _core._buffer_apply(
            buf,
            int(start_frame),
            src,
            float(preserve),
            float(mix),
            int(fade_frames),
        )

    def _frames(self, seconds: float) -> int:
        return int(round(float(seconds) * self.sample_rate))

    @staticmethod
    def _channel(
        data: array.array,
        channels: int,
        col: int,
        start_src: float,
        dur: float,
        file_sr: int,
    ) -> array.array:
        """One channel of an interleaved read, as a contiguous float32 buffer.

        The de-interleave runs in C and pads anything outside the file with
        silence, so a start past the end yields silence rather than an error --
        which is what slicing a numpy view used to do here.
        """
        total = len(data) // channels if channels else 0
        s0 = int(round(float(start_src) * file_sr))
        avail = max(total - s0, 0)
        n = avail if float(dur) < 0 else min(int(round(float(dur) * file_sr)), avail)
        out = array.array("f", bytes(4 * max(n, 0)))
        if n > 0:
            _core._buffer_extract_channel(out, data, channels, col, s0, total)
        return out

    def buffer_read_mono(
        self,
        file: str | Path,
        start_src: float = 0.0,
        start_dst: float = 0.0,
        dur: float = -1.0,
        ch_src: int = 1,
        ch_dst: int = 1,
        preserve: float = 0.0,
        mix: float = 1.0,
    ) -> None:
        """Read one file channel into a buffer at file rate (no resampling).

        A sample-rate mismatch shifts pitch, as on norns. ``ch_src`` selects the
        1-based file channel; ``ch_dst`` the target buffer. ``dur < 0`` reads to
        the end of the file.
        """
        data, channels, file_sr = read_wav(file)
        col = min(max(int(ch_src), 1), channels) - 1
        src = self._channel(data, channels, col, start_src, dur, file_sr)
        self._apply(
            self._buf[int(ch_dst)], self._frames(start_dst), src, preserve, mix, 0
        )

    def buffer_read_stereo(
        self,
        file: str | Path,
        start_src: float = 0.0,
        start_dst: float = 0.0,
        dur: float = -1.0,
        preserve: float = 0.0,
        mix: float = 1.0,
    ) -> None:
        """Read a stereo file into buffers 1 and 2 at file rate (no resampling).

        A mono file spreads its single channel to both buffers.
        """
        data, channels, file_sr = read_wav(file)
        d0 = self._frames(start_dst)
        for b in _BUFFERS:
            col = min(b, channels) - 1
            src = self._channel(data, channels, col, start_src, dur, file_sr)
            self._apply(self._buf[b], d0, src, preserve, mix, 0)

    def buffer_write_mono(
        self,
        file: str | Path,
        start: float = 0.0,
        dur: float = -1.0,
        ch: int = 1,
    ) -> None:
        """Write a region of buffer ``ch`` to a mono 16-bit PCM WAV."""
        buf = self._buf[int(ch)]
        s0 = self._frames(start)
        seg = buf[s0:] if float(dur) < 0 else buf[s0 : s0 + self._frames(dur)]
        write_wav(file, seg, int(self.sample_rate))

    def buffer_write_stereo(
        self,
        file: str | Path,
        start: float = 0.0,
        dur: float = -1.0,
    ) -> None:
        """Write buffers 1 and 2 to an interleaved stereo 16-bit PCM WAV."""
        s0 = self._frames(start)
        if float(dur) < 0:
            a, b = self._buf[1][s0:], self._buf[2][s0:]
        else:
            end = s0 + self._frames(dur)
            a, b = self._buf[1][s0:end], self._buf[2][s0:end]
        n = min(len(a), len(b))
        # Interleave L/R for the writer. array.array assigns an extended slice
        # from another array in C, so this costs one pass and no numpy.
        frames = array.array("f", bytes(4 * 2 * n))
        frames[0::2] = a[:n]
        frames[1::2] = b[:n]
        write_wav(file, frames, int(self.sample_rate), channels=2)

    def buffer_clear(self) -> None:
        """Zero both global buffers."""
        for b in _BUFFERS:
            self.buffer_clear_channel(b)

    def buffer_clear_channel(self, ch: int) -> None:
        """Zero buffer ``ch`` (1 or 2)."""
        self._clear_region(ch, 0.0, -1.0, 0.0, 0.0)

    def _clear_region(
        self, ch: int, start: float, dur: float, fade_time: float, preserve: float
    ) -> None:
        # A clear is the same blended write with no source, so it costs no
        # zero-filled array to pass in -- which for a whole-buffer clear was an
        # allocation the size of the buffer.
        buf = self._buf[int(ch)]
        s0 = self._frames(start)
        cnt = (len(buf) - s0) if float(dur) < 0 else self._frames(dur)
        _core._buffer_apply(
            buf,
            s0,
            None,
            float(preserve),
            0.0,
            self._frames(fade_time),
            max(cnt, 0),
        )

    def buffer_clear_region(
        self, start: float, dur: float, fade_time: float = 0.0, preserve: float = 0.0
    ) -> None:
        """Clear a region of both buffers, with optional edge fade / preserve."""
        for b in _BUFFERS:
            self._clear_region(b, start, dur, fade_time, preserve)

    def buffer_clear_region_channel(
        self,
        ch: int,
        start: float,
        dur: float,
        fade_time: float = 0.0,
        preserve: float = 0.0,
    ) -> None:
        """Clear a region of buffer ``ch``, with optional edge fade / preserve."""
        self._clear_region(ch, start, dur, fade_time, preserve)

    def buffer_copy_mono(
        self,
        src_ch: int,
        dst_ch: int,
        start_src: float = 0.0,
        start_dst: float = 0.0,
        dur: float = -1.0,
        fade_time: float = 0.0,
        preserve: float = 0.0,
        reverse: int = 0,
    ) -> None:
        """Copy a region from buffer ``src_ch`` to ``dst_ch``.

        The source region is copied out first, so an overlapping in-buffer copy
        does not alias. ``reverse`` flips the copied audio; ``fade_time`` and
        ``preserve`` blend it into the destination.
        """
        src_buf = self._buf[int(src_ch)]
        ss = self._frames(start_src)
        cnt = (len(src_buf) - ss) if float(dur) < 0 else self._frames(dur)
        # Slicing an array.array copies, which is what keeps an overlapping
        # in-buffer copy from aliasing, and a reversed slice is contiguous too.
        src = src_buf[ss : ss + max(cnt, 0)]
        if reverse:
            src = src[::-1]
        self._apply(
            self._buf[int(dst_ch)],
            self._frames(start_dst),
            src,
            preserve,
            1.0,
            self._frames(fade_time),
        )

    def buffer_copy_stereo(
        self,
        start_src: float = 0.0,
        start_dst: float = 0.0,
        dur: float = -1.0,
        fade_time: float = 0.0,
        preserve: float = 0.0,
        reverse: int = 0,
    ) -> None:
        """Copy a stereo region within buffers 1 and 2 (each channel to itself)."""
        for b in _BUFFERS:
            self.buffer_copy_mono(
                b, b, start_src, start_dst, dur, fade_time, preserve, reverse
            )

    # --- engine control (additive: norns audio always runs; here explicit) --

    @property
    def engine(self) -> Engine:
        """The underlying :class:`Engine` (for live start/stop and offline render)."""
        return self._eng

    @property
    def buffers(self) -> dict[int, array.array]:
        """The two global buffer arrays, keyed by norns buffer number (1, 2)."""
        return self._buf

    def start(self) -> NornsSoftcut:
        """Open and start the audio device."""
        self._eng.start()
        return self

    def stop(self) -> NornsSoftcut:
        """Stop the audio device."""
        self._eng.stop()
        return self

    def render(self, input: Any, out: Any = None) -> Any:
        """Offline: process a mono input block through all voices."""
        return self._eng.render(input, out)


def _make_float_setter(attr: str):
    def setter(self: NornsSoftcut, i: int, value: float, _attr: str = attr) -> None:
        setattr(self._v(i), _attr, float(value))

    return setter


def _make_bool_setter(attr: str):
    def setter(self: NornsSoftcut, i: int, value: object, _attr: str = attr) -> None:
        setattr(self._v(i), _attr, bool(value))

    return setter


for _name, _attr in _FLOAT_PARAMS.items():
    setattr(NornsSoftcut, _name, _make_float_setter(_attr))
for _name, _attr in _BOOL_PARAMS.items():
    setattr(NornsSoftcut, _name, _make_bool_setter(_attr))


# Process-wide default singleton, plus a module-level proxy so that
# ``from softcut import norns as softcut; softcut.rate(1, 1.0)`` resolves each
# norns function name to the singleton's method.
_default = NornsSoftcut()


def __getattr__(name: str):
    try:
        return getattr(_default, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
