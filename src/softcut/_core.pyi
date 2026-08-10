"""Type stubs for the softcut._core nanobind extension."""

from collections.abc import Sequence
from typing import Any, TypeAlias
from contextlib import AbstractContextManager

# Anything exposing a C-contiguous float32 buffer: an `array.array("f")`, a
# `memoryview`, a numpy array. There is no static type for that -- the buffer
# protocol only became expressible as `collections.abc.Buffer` in 3.12, and even
# that says nothing about dtype or contiguity -- so this is `Any` rather than a
# union. It must not name numpy: these stubs ship in a package that does not
# depend on it, and an unconditional numpy import here fails type-checking for
# anyone who does not have it installed.
Buffer: TypeAlias = Any

class Voice:
    """A single softcut DSP voice over a caller-owned float32 buffer."""

    def __init__(self, sample_rate: float = 48000.0) -> None: ...

    sample_rate: float
    buffer: Buffer

    # transport / loop
    rate: float
    loop_start: float
    loop_end: float
    loop: bool
    fade_time: float

    # record / play
    rec_level: float
    pre_level: float
    rec: bool
    rec_once: bool
    play: bool
    rec_offset: float

    # slew
    rec_pre_slew_time: float
    rate_slew_time: float

    # phase
    phase_quant: float
    phase_offset: float

    # pre filter
    pre_filter_fc: float
    pre_filter_rq: float
    pre_filter_lp: float
    pre_filter_hp: float
    pre_filter_bp: float
    pre_filter_br: float
    pre_filter_dry: float
    pre_filter_fc_mod: float

    # post filter
    post_filter_fc: float
    post_filter_rq: float
    post_filter_lp: float
    post_filter_hp: float
    post_filter_bp: float
    post_filter_br: float
    post_filter_dry: float

    # engine mix
    level: float
    pan: float
    input_gain: float

    # read-only state
    @property
    def position(self) -> float: ...
    @property
    def saved_position(self) -> float: ...
    @property
    def quant_phase(self) -> float: ...

    # actions
    def process(self, input: Buffer, out: Buffer | None = None) -> Any: ...
    def cut_to(self, sec: float) -> None: ...
    def stop(self) -> None: ...
    def reset(self) -> None: ...

    # Pythonic sugar attached at runtime in __init__.py
    loop_region: tuple[float, float]
    def configure(self, **params: object) -> Voice: ...
    def record(self, at: float | None = ...) -> AbstractContextManager[Voice]: ...
    def record_for(self, seconds: float, at: float | None = ...) -> Voice: ...

class _Engine:
    def __init__(
        self,
        voices: Sequence[Voice],
        sample_rate: float,
        block_size: int,
        duplex: bool,
        out_channels: int,
        output_device: int,
        input_device: int,
    ) -> None: ...
    @property
    def running(self) -> bool: ...
    @property
    def block_size(self) -> int: ...
    @property
    def out_channels(self) -> int: ...
    @property
    def duplex(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def render(self, input: Buffer, out: Buffer | None = None) -> Any: ...
    def set_feedback(self, src: int, dst: int, amount: float) -> None: ...
    def get_feedback(self, src: int, dst: int) -> float: ...

def list_devices() -> list[dict]: ...

# Present only when built with SOFTCUT_ENABLE_TINYOSC (the default). Guarded at
# runtime by HAVE_TINYOSC / softcut.osc.NATIVE_OSC_AVAILABLE.
HAVE_TINYOSC: bool

class _OscReceiver:
    def __init__(
        self, host: str, port: int, callback: Any, engine: Any = ..., /
    ) -> None: ...
    @property
    def port(self) -> int: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...

class _OscPhasePoll:
    def __init__(
        self, engine: Any, reply_host: str, reply_port: int, period: float, /
    ) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def reset(self) -> None: ...
    def poll_once(self) -> None: ...
    @property
    def running(self) -> bool: ...

# Shared buffer arithmetic (src/shared/buffer_ops.hpp). Any C-contiguous float32
# buffer is accepted -- ndarray, array.array, memoryview -- and written in place.
def _buffer_apply(
    dst: Buffer,
    start: int,
    src: Buffer | None,
    preserve: float = 0.0,
    mix: float = 1.0,
    fade: int = 0,
    count: int = -1,
) -> None: ...
def _pcm_decode(out: Buffer, raw: bytes, width: int) -> None: ...
def _pcm_encode_s16(src: Buffer) -> bytes: ...
def _buffer_extract_channel(
    out: Buffer,
    interleaved: Buffer,
    channels: int,
    col: int,
    start: int = 0,
    total: int = -1,
) -> None: ...
