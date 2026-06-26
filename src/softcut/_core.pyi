"""Type stubs for the softcut._core nanobind extension."""

from collections.abc import Sequence
from contextlib import AbstractContextManager

from numpy.typing import NDArray
import numpy as np

class Voice:
    """A single softcut DSP voice over a caller-owned float32 buffer."""

    def __init__(self, sample_rate: float = 48000.0) -> None: ...

    sample_rate: float
    buffer: NDArray[np.float32]

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

    # read-only state
    @property
    def position(self) -> float: ...
    @property
    def saved_position(self) -> float: ...
    @property
    def quant_phase(self) -> float: ...

    # actions
    def process(self, input: NDArray[np.float32]) -> NDArray[np.float32]: ...
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
    def render(self, input: NDArray[np.float32]) -> NDArray[np.float32]: ...
