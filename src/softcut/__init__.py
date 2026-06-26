"""softcut - Python bindings for the softcut-lib per-voice DSP engine.

The C++ core (:class:`Voice`) wraps a single ``softcut::Voice``: a crossfading
read/write head over an audio buffer with rate, loop, record/play and pre/post
filtering. softcut-lib owns no buffer memory, so a voice's buffer is just a
numpy ``float32`` array that you own and may share between voices.

:class:`Engine` is the multi-voice host: it owns a set of voices and a miniaudio
device, and runs them either live (realtime mic/speaker I/O on a background
audio thread) or offline via :meth:`Engine.render`. It is a context manager, a
sequence of voices, and is built for dynamic use from a REPL.

Example (live looping)::

    import softcut, time

    with softcut.Engine(voices=2) as eng:      # opens the audio device
        eng.allocate(seconds=8)                # shared power-of-two buffer
        eng[0].configure(loop_region=(0, 4), rate=1.0, level=0.8, pan=-0.3)
        with eng[0].record(at=0):              # rec+play on; capture 4s of mic
            time.sleep(4)
        # on exit: rec off, the voice keeps looping what it captured
        time.sleep(8)
    # device closed automatically
"""

from __future__ import annotations

import contextlib
import time
import warnings
from collections.abc import Iterator, Sequence

import numpy as np

from softcut._core import Voice, _Engine

__all__ = ["Voice", "Engine", "Softcut", "next_power_of_two"]
__version__ = "0.1.0"


def next_power_of_two(n: int) -> int:
    """Smallest power of two >= ``n`` (and >= 1).

    softcut buffers must be a power of two in length, because the read/write
    head wraps its index with a bitmask. Use this to size a buffer that holds
    at least ``n`` frames.
    """
    if n <= 1:
        return 1
    return 1 << (int(n) - 1).bit_length()


# --- Pythonic sugar attached to the compiled Voice class -----------------
#
# nanobind classes accept new methods/properties on the type object, so the
# Engine can hand out the real Voice objects (the ones its audio thread drives)
# while still offering REPL-friendly helpers. These are declared in _core.pyi
# for the type checker.


def _voice_configure(self: Voice, **params: object) -> Voice:
    """Set several parameters at once and return the voice (for chaining)."""
    for key, value in params.items():
        setattr(self, key, value)
    return self


def _voice_get_loop_region(self: Voice) -> tuple[float, float]:
    return (self.loop_start, self.loop_end)


def _voice_set_loop_region(self: Voice, region: tuple[float, float]) -> None:
    start, end = region
    self.loop_start = start
    self.loop_end = end
    self.loop = True


@contextlib.contextmanager
def _voice_record(self: Voice, at: float | None = None) -> Iterator[Voice]:
    """Context manager for the canonical capture gesture (non-blocking).

    On entry: optionally cut to ``at`` seconds, then turn play and rec on. On
    exit: turn rec off (the voice keeps looping what it captured). The body runs
    immediately on the calling thread while audio continues on the device.
    """
    if at is not None:
        self.cut_to(at)
    self.play = True
    self.rec = True
    try:
        yield self
    finally:
        self.rec = False


def _voice_record_for(self: Voice, seconds: float, at: float | None = None) -> Voice:
    """Blocking capture: record for ``seconds`` then turn rec off.

    Implemented as rec-on, sleep, rec-off. Recording happens on the audio
    thread; this blocks the calling thread for the duration. Returns the voice.
    """
    if at is not None:
        self.cut_to(at)
    self.play = True
    self.rec = True
    try:
        time.sleep(seconds)
    finally:
        self.rec = False
    return self


def _voice_repr(self: Voice) -> str:
    loop = f"[{self.loop_start:g}, {self.loop_end:g}]" + ("" if self.loop else " off")
    return (
        f"Voice(rate={self.rate:g}, loop={loop}, rec={self.rec}, play={self.play}, "
        f"level={self.level:g}, pan={self.pan:g}, pos={self.position:.3f})"
    )


# Attached via setattr so the type checker uses the declarations in _core.pyi
# rather than flagging assignment to the compiled class.
setattr(Voice, "configure", _voice_configure)
setattr(
    Voice,
    "loop_region",
    property(
        _voice_get_loop_region,
        _voice_set_loop_region,
        doc="(loop_start, loop_end) as a tuple; setting it also enables looping.",
    ),
)
setattr(Voice, "record", _voice_record)
setattr(Voice, "record_for", _voice_record_for)
setattr(Voice, "__repr__", _voice_repr)


# --- Engine facade -------------------------------------------------------


class Engine(Sequence[Voice]):
    """A multi-voice realtime host owning its voices and an audio device.

    Construct with a voice count and sample rate. The engine is a context
    manager (entering starts the device, exiting stops it) and a sequence of
    its voices (``len``, indexing, iteration). Set parameters live on the
    voices; changes are heard on the next audio block.

    ``mode`` is ``"duplex"`` (live mic input feeds recording voices and voice
    outputs go to the speakers) or ``"playback"`` (speakers only; recording is
    from pre-loaded buffers). Routing, mixing and file I/O are otherwise left to
    plain Python/numpy.
    """

    def __init__(
        self,
        voices: int = 2,
        sample_rate: float = 48000.0,
        mode: str = "duplex",
        block_size: int = 512,
        out_channels: int = 2,
    ) -> None:
        if voices < 1:
            raise ValueError("voices must be >= 1")
        if mode not in ("duplex", "playback"):
            raise ValueError("mode must be 'duplex' or 'playback'")
        self._sample_rate = float(sample_rate)
        self._mode = mode
        self._block_size = int(block_size)
        self._voices: list[Voice] = [Voice(self._sample_rate) for _ in range(voices)]
        self._core = _Engine(
            self._voices,
            self._sample_rate,
            self._block_size,
            mode == "duplex",
            int(out_channels),
        )

    # sequence protocol
    def __len__(self) -> int:
        return len(self._voices)

    def __getitem__(self, index):  # type: ignore[override]
        return self._voices[index]

    def __iter__(self) -> Iterator[Voice]:
        return iter(self._voices)

    @property
    def voices(self) -> list[Voice]:
        """The list of voices."""
        return self._voices

    def voice(self, index: int) -> Voice:
        """Return the voice at ``index``."""
        return self._voices[index]

    @property
    def sample_rate(self) -> float:
        """Sample rate in Hz."""
        return self._sample_rate

    @property
    def mode(self) -> str:
        """``"duplex"`` or ``"playback"``."""
        return self._mode

    @property
    def block_size(self) -> int:
        """Processing block size in frames."""
        return self._block_size

    @property
    def running(self) -> bool:
        """True while the audio device is started."""
        return self._core.running

    def allocate(
        self,
        seconds: float | None = None,
        frames: int | None = None,
        shared: bool = True,
    ) -> np.ndarray | list[np.ndarray]:
        """Allocate and assign zeroed ``float32`` buffer(s) to the voices.

        Provide exactly one of ``seconds`` or ``frames``. The length is rounded
        up to the next power of two (a softcut requirement). With ``shared=True``
        every voice points at the same buffer (norns-style shared memory); with
        ``shared=False`` each voice gets its own. Returns the shared buffer, or
        the list of per-voice buffers.
        """
        if (seconds is None) == (frames is None):
            raise ValueError("provide exactly one of seconds or frames")
        if frames is not None:
            requested = int(frames)
        else:
            assert seconds is not None  # guaranteed by the check above
            requested = int(round(seconds * self._sample_rate))
        if requested < 1:
            raise ValueError("buffer length must be >= 1 frame")
        n = next_power_of_two(requested)

        if shared:
            buf = np.zeros(n, dtype=np.float32)
            for v in self._voices:
                v.buffer = buf
            return buf

        bufs = [np.zeros(n, dtype=np.float32) for _ in self._voices]
        for v, buf in zip(self._voices, bufs):
            v.buffer = buf
        return bufs

    def sync(self, follow: int, lead: int, offset: float = 0.0) -> None:
        """Cut the ``follow`` voice to the ``lead`` voice's position + offset."""
        self._voices[follow].cut_to(self._voices[lead].position + offset)

    def render(self, input: np.ndarray) -> np.ndarray:
        """Offline: process a mono input block through all voices.

        ``input`` is a 1-D array of mono samples. Returns an ``(n, out_channels)``
        float32 array of the mixed output. Raises if the device is running (use
        the live path then, not render).
        """
        if self.running:
            raise RuntimeError(
                "cannot render() while the device is running; stop() first"
            )
        arr = np.ascontiguousarray(input, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError("render input must be a 1-D mono array")
        return self._core.render(arr)

    def start(self) -> Engine:
        """Open (if needed) and start the audio device. Non-blocking."""
        self._core.start()
        return self

    def stop(self) -> Engine:
        """Stop the audio device."""
        self._core.stop()
        return self

    def __enter__(self) -> Engine:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __del__(self) -> None:
        # Ensure the audio thread is stopped before the voices it reads are
        # freed, regardless of attribute teardown order.
        core = getattr(self, "_core", None)
        if core is not None:
            try:
                core.stop()
            except Exception:
                pass

    def __repr__(self) -> str:
        return (
            f"Engine(voices={len(self._voices)}, sr={self._sample_rate:g}, "
            f"mode={self._mode!r}, block_size={self._block_size}, running={self.running})"
        )


class Softcut(Engine):
    """Deprecated alias for :class:`Engine` (defaults to offline/playback).

    Kept for the pre-1.0 transition; use :class:`Engine` instead.
    """

    def __init__(self, voices: int = 6, sample_rate: float = 48000.0) -> None:
        warnings.warn(
            "softcut.Softcut is deprecated; use softcut.Engine",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(voices=voices, sample_rate=sample_rate, mode="playback")
