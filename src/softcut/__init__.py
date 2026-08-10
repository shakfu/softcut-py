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

import array
import contextlib
import time
import warnings
from collections.abc import Iterator, Sequence
from typing import Any

from softcut._core import Voice, _Engine
from softcut._core import list_devices as _list_devices
from softcut._wavio import read_wav, read_wav_mono, write_wav


def _zeros(n: int) -> array.array:
    """A zeroed float32 buffer of ``n`` samples."""
    return array.array("f", bytes(4 * int(n)))


def _samples(buffer: Any) -> int:
    """How many float32 samples a buffer holds, refusing anything else.

    The extension takes 1-D C-contiguous float32 only. Checking here rather than
    letting the binding fail means the message names the actual problem -- most
    often a float64 array, which used to be coerced silently and now is not.
    """
    view = memoryview(buffer)
    if view.ndim != 1 or view.format not in ("f", "<f"):
        raise ValueError(
            "expected a 1-D C-contiguous float32 buffer, got "
            f"ndim={view.ndim} format={view.format!r}"
            + (
                " (a float64 array? cast it with .astype('float32'))"
                if view.format in ("d", "<d")
                else ""
            )
        )
    return int(view.nbytes // view.itemsize)


__all__ = [
    "Voice",
    "Engine",
    "Softcut",
    "next_power_of_two",
    "list_devices",
    "read_wav",
    "read_wav_mono",
    "write_wav",
]
__version__ = "0.4.1"


def list_devices() -> list[dict]:
    """List the system audio devices.

    Returns a list of dicts with keys ``index``, ``name``, ``type``
    (``"playback"`` or ``"capture"``) and ``is_default``. The ``index`` is what
    ``Engine(output_device=...)`` / ``Engine(input_device=...)`` expect for the
    matching type.
    """
    return _list_devices()


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


_voice_process_native = Voice.process


def _voice_process(self: Voice, input: Any, out: Any = None) -> Any:
    """Process one mono block, returning the output buffer.

    ``input`` is any 1-D C-contiguous float32 buffer. The result is written into
    ``out`` and returned; omit it and a zeroed ``array.array("f")`` of the same
    length is allocated. The extension itself never allocates, which is what
    keeps numpy out of it.
    """
    n = _samples(input)
    if out is None:
        out = _zeros(n)
    return _voice_process_native(self, input, out)


# Attached via setattr so the type checker uses the declarations in _core.pyi
# rather than flagging assignment to the compiled class.
setattr(Voice, "process", _voice_process)
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
    from pre-loaded buffers). ``output_device``/``input_device`` select a device
    by its index from :func:`list_devices` (``-1`` uses the system default).
    Voices mix to stereo via their ``level``/``pan``; ``feedback()`` routes one
    voice's output into another's input.
    """

    def __init__(
        self,
        voices: int = 2,
        sample_rate: float = 48000.0,
        mode: str = "duplex",
        block_size: int = 512,
        out_channels: int = 2,
        output_device: int = -1,
        input_device: int = -1,
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
            int(output_device),
            int(input_device),
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

    @property
    def out_channels(self) -> int:
        """How many channels the mix is written to (2 unless configured otherwise)."""
        return self._core.out_channels

    def allocate(
        self,
        seconds: float | None = None,
        frames: int | None = None,
        shared: bool = True,
    ) -> Any:
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
            buf = _zeros(n)
            for v in self._voices:
                v.buffer = buf
            return buf

        bufs = [_zeros(n) for _ in self._voices]
        for v, buf in zip(self._voices, bufs):
            v.buffer = buf
        return bufs

    def sync(self, follow: int, lead: int, offset: float = 0.0) -> None:
        """Cut the ``follow`` voice to the ``lead`` voice's position + offset."""
        self._voices[follow].cut_to(self._voices[lead].position + offset)

    def feedback(self, src: int, dst: int, amount: float | None = None):
        """Get or set the feedback gain from voice ``src`` into voice ``dst``.

        With ``amount`` omitted, returns the current gain. Otherwise sets it (a
        voice's output is mixed into another's input, delayed by one block) and
        returns ``self``. ``src == dst`` is self-feedback (a delay line).
        """
        if amount is None:
            return self._core.get_feedback(src, dst)
        self._core.set_feedback(src, dst, float(amount))
        return self

    def render(
        self, input: Any = None, out: Any = None, *, seconds: float | None = None
    ) -> Any:
        """Offline: process a mono input block through all voices.

        Give it either ``input`` -- any 1-D C-contiguous float32 buffer, which is
        what the voices record -- or ``seconds``, which feeds them that much
        silence. The second is what most offline work wants: there is nothing to
        record, the voices are playing material already in their buffers, and an
        input buffer of zeros is pure ceremony.

        The mixed output is written into ``out`` and returned; omit it and a
        zeroed ``array.array("f")`` of ``n * out_channels`` samples is allocated
        for you. Frames are interleaved, so ``numpy.asarray(out).reshape(-1,
        engine.out_channels)`` is the 2-D view, taken without copying.

        Head positions persist across calls, so successive renders concatenate
        into continuous audio.

        Raises:
            RuntimeError: If the device is running -- use the live path then.
            ValueError: If neither or both of ``input`` and ``seconds`` is given.
        """
        if self.running:
            raise RuntimeError(
                "cannot render() while the device is running; stop() first"
            )
        if (input is None) == (seconds is None):
            raise ValueError("provide exactly one of input or seconds")
        if seconds is not None:
            input = _zeros(int(round(float(seconds) * self._sample_rate)))
        n = _samples(input)
        if out is None:
            out = _zeros(n * self.out_channels)
        return self._core.render(input, out)

    def render_to(
        self,
        path: Any,
        input: Any = None,
        out: Any = None,
        *,
        seconds: float | None = None,
    ) -> Any:
        """Render and write the result to a 16-bit PCM WAV. Returns the path.

        Takes ``input`` or ``seconds`` exactly as `render` does, and supplies the
        writer with the sample rate and channel count the engine already knows:

        ```python
        eng.render_to("out.wav", seconds=4)
        ```

        ``out`` is passed through, so a loop can reuse one buffer.
        """
        frames = self.render(input, out, seconds=seconds)
        return write_wav(
            path, frames, int(self._sample_rate), channels=self.out_channels
        )

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
