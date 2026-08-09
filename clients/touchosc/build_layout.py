#!/usr/bin/env python3
"""Generate a TouchOSC control surface (.tosc) for the softcut OSC namespace.

The layout drives ``softcut.osc`` (or the standalone ``clients/softcut-osc``
binary) over the monome softcut wire protocol: every control sends a real
address from that namespace, with the voice index carried as a constant integer
argument and the control's own value as the second argument, scaled into the
parameter's range. Indices are 0-based on the wire, as the protocol requires,
while the tab labels and captions count voices from 1 the way norns does.

Built with `py2tosc <https://pypi.org/project/py2tosc/>`_::

    pip install py2tosc
    python clients/touchosc/build_layout.py

Pages:

===========  ==================================================================
``mix``      a vertical strip per voice: level, pan, rate, play / rec / loop
``loop``     loop flag, loop points, position, fade and rate slew, per voice
``rec``      record flags and levels, pre level, record offset and slew
``pre``      the pre-filter: cutoff, cutoff mod, rq, and the LP/HP/BP/BR/dry mix
``post``     the post-filter: cutoff, rq, and the LP/HP/BP/BR/dry mix
``routing``  level, pan, enable, input level, buffer assignment, feedback matrix
``sync``     phase quantisation and offset, the voice-sync matrix, phase readout
``system``   reset, buffer clears, disk read/write, hello and quit
===========  ==================================================================

Ranges are linear because a TouchOSC argument partial scales linearly; filter
cutoff in particular is therefore linear in Hz rather than exponential. Pass
``--max-time`` to match the loop-point and position ranges to the material.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import py2tosc
from py2tosc import Value, ui
from py2tosc.enums import Conversion, TriggerCondition

# --- defaults ---------------------------------------------------------------

DEFAULT_VOICES = 6
DEFAULT_SIZE = (1024, 768)
DEFAULT_MAX_TIME = 32.0  # seconds; loop point, position and phase ranges
DEFAULT_RATE = 2.0  # rate faders span -RATE .. +RATE
DEFAULT_READ_FILE = "loop.wav"
DEFAULT_WRITE_FILE = "softcut-out.wav"

#: Cutoff spans up to each filter's own default, so a fader that has not been
#: touched sits where the engine actually is rather than below it.
PRE_FC = (10.0, 16000.0)  # Hz
POST_FC = (10.0, 12000.0)  # Hz, as in norns
FILTER_RQ = (0.1, 4.0)  # reciprocal Q
REC_OFFSET = (-0.01, 0.01)  # seconds
SLEW = (0.0, 1.0)  # seconds
PHASE_QUANT = (0.0, 4.0)  # seconds
PHASE_OFFSET = (-1.0, 1.0)  # seconds

#: What a freshly reset softcut voice holds, in the parameter's own units. A
#: control opens at the position that stands for this, so the surface agrees
#: with the engine before anything has been touched -- TouchOSC transmits
#: nothing on load, so these are a display, not a preset. Checked against a real
#: voice by tests/test_touchosc.py.
VOICE_DEFAULTS: dict[str, float] = {
    "/set/level/cut": 1.0,
    "/set/pan/cut": 0.0,
    "/set/level/in_cut": 1.0,
    "/set/param/cut/rate": 1.0,
    "/set/param/cut/loop_start": 0.0,
    "/set/param/cut/loop_end": 0.0,
    "/set/param/cut/position": 0.0,
    "/set/param/cut/fade_time": 0.01,
    "/set/param/cut/rate_slew_time": 0.001,
    "/set/param/cut/recpre_slew_time": 0.001,
    "/set/param/cut/rec_level": 0.0,
    "/set/param/cut/pre_level": 0.0,
    "/set/param/cut/rec_offset": -1.0 / 6000.0,
    "/set/param/cut/phase_quant": 0.0,
    "/set/param/cut/phase_offset": 0.0,
    "/set/param/cut/pre_filter_fc": 16000.0,
    "/set/param/cut/pre_filter_fc_mod": 1.0,
    "/set/param/cut/pre_filter_rq": 4.0,
    "/set/param/cut/pre_filter_lp": 1.0,
    "/set/param/cut/pre_filter_hp": 0.0,
    "/set/param/cut/pre_filter_bp": 0.0,
    "/set/param/cut/pre_filter_br": 0.0,
    "/set/param/cut/pre_filter_dry": 0.0,
    "/set/param/cut/post_filter_fc": 12000.0,
    "/set/param/cut/post_filter_rq": 4.0,
    "/set/param/cut/post_filter_lp": 0.0,
    "/set/param/cut/post_filter_hp": 0.0,
    "/set/param/cut/post_filter_bp": 0.0,
    "/set/param/cut/post_filter_br": 0.0,
    "/set/param/cut/post_filter_dry": 1.0,
}

# --- palette ----------------------------------------------------------------

INK = "#dcdcdc"
DIM = "#8d8d8d"
PANEL = "#151515"
SECTION = "#1d1d1d"

LEVEL = "#2a9d8f"
PAN = "#457b9d"
RATE = "#e9c46a"
REC = "#e63946"
PLAY = "#43aa8b"
LOOP = "#9d4edd"
SLEWC = "#577590"
PRE = "#00b4d8"
POST = "#0077b6"
ROUTE = "#f4a261"
SYNC = "#b5838d"
SYSTEM = "#5c677d"
DANGER = "#8d1c26"

#: Horizontal text alignment, as the format numbers it: 1 left, 2 centre, 3 right.
LEFT = 1
CENTRE = 2

#: One accent per voice, so a column can be found by colour as well as number.
ACCENT = ("#e76f51", "#e9c46a", "#2a9d8f", "#457b9d", "#9d4edd", "#f4a261")

#: TouchOSC's ten connection slots, with only the first enabled. Left at the
#: default every control would transmit on all ten, so adding a second OSC
#: destination later would silently send the whole surface there as well.
CONNECTION = "1" + "0" * 9

# --- message helpers --------------------------------------------------------


def index_arg(n: int) -> object:
    """A constant integer argument -- a voice, buffer or channel index."""
    return ui.const(str(n), conversion=Conversion.INTEGER)


def float_arg(x: float) -> object:
    """A constant float argument."""
    return ui.const(f"{x:g}", conversion=Conversion.FLOAT)


def string_arg(text: str) -> object:
    """A constant string argument, such as a file path."""
    return ui.const(text, conversion=Conversion.STRING)


def scaled_arg(lo: float, hi: float) -> object:
    """The control's own value, mapped from 0..1 onto ``lo..hi``."""
    return ui.value("x", conversion=Conversion.FLOAT, scale=(lo, hi))


def send(address: str, *args: object) -> object:
    """A send-only OSC binding firing on any change of the control's value.

    Receive is off: softcut never transmits on the control namespace, and a
    control that listens to the address it writes will chase an echo.
    """
    return ui.osc(address, args=list(args), receive=False, connections=CONNECTION)


def send_on_press(address: str, *args: object) -> object:
    """A send-only OSC binding firing once, on the press rather than the release."""
    return ui.osc(
        address,
        args=list(args),
        on=TriggerCondition.RISE,
        receive=False,
        connections=CONNECTION,
    )


def receive_only(address: str, *args: object) -> object:
    """A display binding: listen for the address, never transmit it.

    Constant arguments act as a filter on the incoming message, which is how one
    readout per voice picks its own voice out of a shared reply address.
    """
    return ui.osc(
        address, args=list(args), send=False, receive=True, connections=CONNECTION
    )


# --- control helpers --------------------------------------------------------


def caption(
    text: str, *, size: float = 13, color: str = INK, align: int = CENTRE
) -> py2tosc.Control:
    """A non-interactive text label with no background of its own."""
    return py2tosc.label(
        name=text or "spacer",
        background=False,
        outline=False,
        text_color=color,
        text_size=size,
        text_align_h=align,
        values=[Value("text", default=text), Value("touch", default=False)],
    )


def titled(
    control: py2tosc.Control,
    text: str,
    *,
    size: float = 11,
    sizes: tuple[float, float] = (1.0, 4.0),
) -> py2tosc.Control:
    """A control with its caption on a thin line above it."""
    return ui.column(caption(text, size=size, color=DIM), control, sizes=sizes, gap=2)


def position_of(address: str, span: tuple[float, float]) -> float:
    """Where a fader sits when its parameter is at the engine's default.

    Anything not in [`VOICE_DEFAULTS`][] opens at the bottom of its travel, and
    a default outside the fader's range is clamped rather than silently
    misdrawn.
    """
    default = VOICE_DEFAULTS.get(address)
    if default is None:
        return 0.0
    lo, hi = span
    return min(1.0, max(0.0, (default - lo) / (hi - lo)))


def _at(x: float) -> list[Value]:
    """The value list of a fader resting at ``x``."""
    return [Value("x", default=x), Value("touch", default=False)]


def hfader(
    name: str,
    color: str,
    messages: Sequence[object],
    *,
    steps: int = 11,
    interactive: bool = True,
    at: float = 0.0,
) -> py2tosc.Control:
    """A horizontal fader (orientation 1, as the TouchOSC editor writes it)."""
    return py2tosc.fader(
        name=name,
        color=color,
        orientation=1,
        outline=False,
        corner_radius=2.0,
        grid_steps=steps,
        interactive=interactive,
        values=_at(at),
        messages=list(messages),
    )


def vfader(
    name: str, color: str, messages: Sequence[object], *, at: float = 0.0
) -> py2tosc.Control:
    """A vertical fader."""
    return py2tosc.fader(
        name=name,
        color=color,
        orientation=0,
        outline=False,
        corner_radius=2.0,
        grid_steps=11,
        values=_at(at),
        messages=list(messages),
    )


def toggle(
    name: str, color: str, messages: Sequence[object], text: str | None = None
) -> py2tosc.Control:
    """A latching button, captioned with its own name unless told otherwise."""
    button = py2tosc.button(
        name=name,
        color=color,
        button_type=1,  # toggle on release
        outline=False,
        corner_radius=2.0,
        messages=list(messages),
    )
    return ui.labelled(button, text if text is not None else name, size=13)


def trigger(
    name: str, color: str, messages: Sequence[object], text: str | None = None
) -> py2tosc.Control:
    """A momentary button that fires once when pressed."""
    button = py2tosc.button(
        name=name,
        color=color,
        button_type=0,  # momentary
        outline=False,
        corner_radius=2.0,
        messages=list(messages),
    )
    return ui.labelled(button, text if text is not None else name, size=13)


# --- per-voice parameter tables ---------------------------------------------


@dataclass(frozen=True)
class Param:
    """One row of a parameter table: the same control, once per voice.

    Attributes:
        label: The row caption. The range is appended for faders.
        address: The OSC address, sent as ``<address> <voice:i> <value:f>``.
        color: The control colour.
        span: Low and high ends of the value range. Ignored by toggles.
        toggle: Whether the row holds latching buttons rather than faders.
        steps: Grid line count, odd for bipolar ranges so the centre shows.
        key: The stem of each control's name, defaulting to the label. The two
            filter pages set it, since `fc` on the pre-filter and `fc` on the
            post-filter are different parameters and should not share a name.
        note: A hint appended to the caption, for a row whose fader does not
            read the way it looks.
    """

    label: str
    address: str
    color: str
    span: tuple[float, float] = (0.0, 1.0)
    toggle: bool = False
    steps: int = 11
    key: str = ""
    note: str = ""

    def caption(self) -> str:
        text = self.label
        if not self.toggle:
            lo, hi = self.span
            text = f"{text}  {lo:g}..{hi:g}"
        return f"{text}   {self.note}" if self.note else text

    def control(self, voice: int) -> py2tosc.Control:
        name = f"{(self.key or self.label).replace(' ', '_')}{voice + 1}"
        message = send(self.address, index_arg(voice), scaled_arg(*self.span))
        if self.toggle:
            return toggle(name, self.color, [message], text="")
        return hfader(
            name,
            self.color,
            [message],
            steps=self.steps,
            at=position_of(self.address, self.span),
        )


def param_row(param: Param, voices: int, label_width: float) -> py2tosc.Control:
    """A caption plus one control per voice, laid out left to right."""
    return ui.row(
        caption(param.caption(), size=13, color=INK, align=LEFT),
        *(param.control(v) for v in range(voices)),
        sizes=[label_width] + [2.0] * voices,
        gap=5,
        name=param.label.replace(" ", "_"),
    )


def voice_header(voices: int, label_width: float, text: str = "") -> py2tosc.Control:
    """The numbered column header shared by every table on a page."""
    return ui.row(
        caption(text, size=13, color=DIM),
        *(
            caption(str(v + 1), size=16, color=ACCENT[v % len(ACCENT)])
            for v in range(voices)
        ),
        sizes=[label_width] + [2.0] * voices,
        gap=5,
        name="voices",
    )


def param_page(
    name: str, params: Sequence[Param], voices: int, *, label_width: float = 3.5
) -> py2tosc.Control:
    """A page of parameter rows, one column per voice."""
    return ui.column(
        voice_header(voices, label_width),
        *(param_row(p, voices, label_width) for p in params),
        sizes=[1.4] + [2.0] * len(params),
        gap=8,
        pad=(12, 10),
        name=name,
        color=PANEL,
    )


# --- pages ------------------------------------------------------------------


def mix_page(voices: int, rate: float) -> py2tosc.Control:
    """One vertical strip per voice: the page to perform from."""

    def strip(v: int) -> py2tosc.Control:
        accent = ACCENT[v % len(ACCENT)]
        level = vfader(
            f"level{v + 1}",
            LEVEL,
            [send("/set/level/cut", index_arg(v), scaled_arg(0.0, 1.0))],
            at=position_of("/set/level/cut", (0.0, 1.0)),
        )
        pan = hfader(
            f"pan{v + 1}",
            PAN,
            [send("/set/pan/cut", index_arg(v), scaled_arg(-1.0, 1.0))],
            at=position_of("/set/pan/cut", (-1.0, 1.0)),
        )
        rate_fader = hfader(
            f"rate{v + 1}",
            RATE,
            [send("/set/param/cut/rate", index_arg(v), scaled_arg(-rate, rate))],
            at=position_of("/set/param/cut/rate", (-rate, rate)),
        )
        flags = ui.row(
            toggle(
                f"play{v + 1}",
                PLAY,
                [send("/set/param/cut/play_flag", index_arg(v), scaled_arg(0.0, 1.0))],
                text="play",
            ),
            toggle(
                f"rec{v + 1}",
                REC,
                [send("/set/param/cut/rec_flag", index_arg(v), scaled_arg(0.0, 1.0))],
                text="rec",
            ),
            toggle(
                f"loop{v + 1}",
                LOOP,
                [send("/set/param/cut/loop_flag", index_arg(v), scaled_arg(0.0, 1.0))],
                text="loop",
            ),
            gap=4,
            name=f"flags{v + 1}",
        )
        return ui.column(
            caption(f"voice {v + 1}", size=17, color=accent),
            titled(level, "level", sizes=(1.0, 9.0)),
            titled(pan, "pan"),
            titled(rate_fader, f"rate  -{rate:g}..{rate:g}"),
            flags,
            sizes=(1.1, 9.0, 2.2, 2.2, 2.0),
            gap=8,
            pad=6,
            name=f"strip{v + 1}",
            color=SECTION,
        )

    return ui.row(
        *(strip(v) for v in range(voices)),
        gap=6,
        pad=(10, 8),
        name="mix",
        color=PANEL,
    )


def loop_page(voices: int, max_time: float, rate: float) -> py2tosc.Control:
    """Loop points, playback position and the rate, per voice."""
    return param_page(
        "loop",
        [
            Param("loop", "/set/param/cut/loop_flag", LOOP, toggle=True),
            Param("loop start", "/set/param/cut/loop_start", LOOP, (0.0, max_time)),
            Param("loop end", "/set/param/cut/loop_end", LOOP, (0.0, max_time)),
            Param("position", "/set/param/cut/position", RATE, (0.0, max_time)),
            Param("rate", "/set/param/cut/rate", RATE, (-rate, rate)),
            Param("rate slew", "/set/param/cut/rate_slew_time", SLEWC, SLEW),
            Param("fade time", "/set/param/cut/fade_time", SLEWC, (0.0, 1.0)),
        ],
        voices,
    )


def rec_page(voices: int) -> py2tosc.Control:
    """Record and playback flags, record and preserve levels."""
    return param_page(
        "rec",
        [
            Param("rec", "/set/param/cut/rec_flag", REC, toggle=True),
            Param("play", "/set/param/cut/play_flag", PLAY, toggle=True),
            Param("rec once", "/set/param/cut/rec_once", REC, toggle=True),
            Param("rec level", "/set/param/cut/rec_level", REC),
            Param("pre level", "/set/param/cut/pre_level", REC),
            Param("rec offset", "/set/param/cut/rec_offset", SLEWC, REC_OFFSET),
            Param("recpre slew", "/set/param/cut/recpre_slew_time", SLEWC, SLEW),
        ],
        voices,
    )


def filter_page(voices: int, which: str) -> py2tosc.Control:
    """The pre- or post-filter mix. The pre-filter alone has cutoff modulation."""
    color = PRE if which == "pre" else POST
    address = f"/set/param/cut/{which}_filter_"

    def band(
        label: str, span: tuple[float, float] = (0.0, 1.0), note: str = ""
    ) -> Param:
        return Param(
            label,
            address + label.replace(" ", "_"),
            color,
            span,
            key=f"{which}_{label}",
            note=note,
        )

    params = [band("fc", PRE_FC if which == "pre" else POST_FC)]
    if which == "pre":
        params.append(band("fc mod"))
    # rq is reciprocal Q -- it enters the coefficients as damping, so the fader
    # gets tamer as it rises and the resonance is at the bottom of its travel.
    params += [band("rq", FILTER_RQ, "low = resonant")] + [
        band(b) for b in ("lp", "hp", "bp", "br", "dry")
    ]
    return param_page(which, params, voices)


def matrix(
    voices: int,
    cell: Callable[[int, int], py2tosc.Control],
    *,
    name: str,
    label_width: float = 1.6,
) -> py2tosc.Control:
    """A voices-by-voices grid with numbered row and column headers."""
    rows = [
        ui.row(
            caption(str(src + 1), size=15, color=ACCENT[src % len(ACCENT)]),
            *(cell(src, dst) for dst in range(voices)),
            sizes=[label_width] + [2.0] * voices,
            gap=4,
            name=f"{name}_row{src + 1}",
        )
        for src in range(voices)
    ]
    return ui.column(
        voice_header(voices, label_width),
        *rows,
        sizes=[1.0] + [1.6] * voices,
        gap=4,
        name=name,
    )


def routing_page(voices: int) -> py2tosc.Control:
    """Output mix, input level, buffer assignment and the feedback matrix."""

    def mix_row(
        label: str, address: str, color: str, span: tuple[float, float]
    ) -> py2tosc.Control:
        return param_row(Param(label, address, color, span), voices, 3.0)

    enable_row = param_row(
        Param("enabled", "/set/enabled/cut", PLAY, toggle=True), voices, 3.0
    )

    # /set/level/in_cut takes (input channel, voice, level). The binding has one
    # scalar input gain per voice, so the channel index is fixed at 0.
    in_row = ui.row(
        caption("input -> cut  0..1", size=13, align=LEFT),
        *(
            hfader(
                f"in_cut{v + 1}",
                ROUTE,
                [
                    send(
                        "/set/level/in_cut",
                        index_arg(0),
                        index_arg(v),
                        scaled_arg(0.0, 1.0),
                    )
                ],
                at=position_of("/set/level/in_cut", (0.0, 1.0)),
            )
            for v in range(voices)
        ),
        sizes=[3.0] + [2.0] * voices,
        gap=5,
        name="in_cut",
    )

    # /set/param/cut/buffer takes (voice, buffer), both 0-based on the wire.
    buffer_row = ui.row(
        caption("buffer", size=13, align=LEFT),
        *(
            ui.row(
                *(
                    trigger(
                        f"buf{b + 1}_{v + 1}",
                        ROUTE,
                        [
                            send_on_press(
                                "/set/param/cut/buffer", index_arg(v), index_arg(b)
                            )
                        ],
                        text=str(b + 1),
                    )
                    for b in range(2)
                ),
                gap=4,
                name=f"buffer{v + 1}",
            )
            for v in range(voices)
        ),
        sizes=[3.0] + [2.0] * voices,
        gap=5,
        name="buffer",
    )

    def feedback_cell(src: int, dst: int) -> py2tosc.Control:
        return hfader(
            f"cut_cut{src + 1}_{dst + 1}",
            ROUTE,
            [
                send(
                    "/set/level/cut_cut",
                    index_arg(src),
                    index_arg(dst),
                    scaled_arg(0.0, 1.0),
                )
            ],
            steps=5,
        )

    return ui.column(
        voice_header(voices, 3.0),
        mix_row("level", "/set/level/cut", LEVEL, (0.0, 1.0)),
        mix_row("pan", "/set/pan/cut", PAN, (-1.0, 1.0)),
        enable_row,
        in_row,
        buffer_row,
        caption(
            "cut -> cut feedback   row = source, column = destination",
            size=13,
            align=LEFT,
        ),
        matrix(voices, feedback_cell, name="cut_cut"),
        sizes=(1.2, 1.6, 1.6, 1.6, 1.6, 1.6, 1.2, 9.0),
        gap=7,
        pad=(12, 10),
        name="routing",
        color=PANEL,
    )


def sync_page(voices: int, max_time: float) -> py2tosc.Control:
    """Phase quantisation and offset, the voice-sync matrix, and the readout."""
    quant = param_row(
        Param("phase quant", "/set/param/cut/phase_quant", SYNC, PHASE_QUANT),
        voices,
        3.0,
    )
    offset = param_row(
        Param("phase offset", "/set/param/cut/phase_offset", SYNC, PHASE_OFFSET),
        voices,
        3.0,
    )

    # Receive-only: the running phase poll replies to reply_host:reply_port with
    # /poll/softcut/phase <voice> <phase>, and the constant voice argument is
    # what picks one voice's reply out of the shared address.
    readout = ui.row(
        caption(f"phase  0..{max_time:g}", size=13, align=LEFT),
        *(
            hfader(
                f"phase{v + 1}",
                ACCENT[v % len(ACCENT)],
                [
                    receive_only(
                        "/poll/softcut/phase",
                        index_arg(v),
                        scaled_arg(0.0, max_time),
                    )
                ],
                interactive=False,
            )
            for v in range(voices)
        ),
        sizes=[3.0] + [2.0] * voices,
        gap=5,
        name="phase",
    )

    poll = ui.row(
        trigger(
            "poll_start", SYNC, [send_on_press("/poll/start/cut/phase")], text="poll on"
        ),
        trigger(
            "poll_stop",
            SYSTEM,
            [send_on_press("/poll/stop/cut/phase")],
            text="poll off",
        ),
        caption(
            "phase replies go to the server's reply host and port",
            size=12,
            color=DIM,
            align=LEFT,
        ),
        sizes=(2.0, 2.0, 7.0),
        gap=6,
        name="poll",
    )

    def sync_cell(dst: int, src: int) -> py2tosc.Control:
        # /set/param/cut/voice_sync takes (destination, source, offset seconds).
        # Every cell is captioned: a momentary button draws its colour only
        # while it is held, so an unlabelled matrix is 36 identical dark
        # rectangles. Syncing a voice to itself does nothing, so the diagonal
        # says so rather than pretending to be a control.
        if dst == src:
            return ui.stack(
                py2tosc.box(
                    name=f"sync{dst + 1}_{src + 1}", color=PANEL, outline=False
                ),
                caption("-", size=13, color=DIM),
            )
        return trigger(
            f"sync{dst + 1}_{src + 1}",
            SYNC,
            [
                send_on_press(
                    "/set/param/cut/voice_sync",
                    index_arg(dst),
                    index_arg(src),
                    float_arg(0.0),
                )
            ],
            text=f"{dst + 1} to {src + 1}",
        )

    return ui.column(
        voice_header(voices, 3.0),
        quant,
        offset,
        readout,
        poll,
        caption(
            "voice sync   row = voice moved, column = voice followed",
            size=13,
            align=LEFT,
        ),
        matrix(voices, sync_cell, name="voice_sync"),
        sizes=(1.2, 1.6, 1.6, 1.6, 1.4, 1.2, 9.0),
        gap=7,
        pad=(12, 10),
        name="sync",
        color=PANEL,
    )


def system_page(max_time: float, read_file: str, write_file: str) -> py2tosc.Control:
    """Reset, buffer clears, disk read and write, and the server lifecycle.

    The disk addresses carry their file path as a constant string argument,
    because a TouchOSC message can only read values from the control it belongs
    to. Pass ``--read-file`` and ``--write-file`` to bake in different ones.
    """
    buffers = ui.row(
        trigger(
            "clear", REC, [send_on_press("/softcut/buffer/clear")], text="clear both"
        ),
        trigger(
            "clear1",
            REC,
            [send_on_press("/softcut/buffer/clear_channel", index_arg(0))],
            text="clear 1",
        ),
        trigger(
            "clear2",
            REC,
            [send_on_press("/softcut/buffer/clear_channel", index_arg(1))],
            text="clear 2",
        ),
        trigger(
            "clear_region",
            REC,
            [
                send_on_press(
                    "/softcut/buffer/clear_region", float_arg(0.0), float_arg(max_time)
                )
            ],
            text=f"clear 0..{max_time:g}s",
        ),
        *(
            trigger(
                f"clear_region{b + 1}",
                REC,
                [
                    send_on_press(
                        "/softcut/buffer/clear_region_channel",
                        index_arg(b),
                        float_arg(0.0),
                        float_arg(max_time),
                    )
                ],
                text=f"clear {b + 1}: 0..{max_time:g}s",
            )
            for b in range(2)
        ),
        gap=8,
        name="clears",
    )

    disk = ui.row(
        trigger(
            "read_mono",
            ROUTE,
            [
                send_on_press(
                    "/softcut/buffer/read_mono",
                    string_arg(read_file),
                    float_arg(0.0),
                    float_arg(0.0),
                    float_arg(-1.0),
                    index_arg(0),
                    index_arg(0),
                )
            ],
            text="read mono",
        ),
        trigger(
            "read_stereo",
            ROUTE,
            [
                send_on_press(
                    "/softcut/buffer/read_stereo",
                    string_arg(read_file),
                    float_arg(0.0),
                    float_arg(0.0),
                    float_arg(-1.0),
                )
            ],
            text="read stereo",
        ),
        trigger(
            "write_mono",
            ROUTE,
            [
                send_on_press(
                    "/softcut/buffer/write_mono",
                    string_arg(write_file),
                    float_arg(0.0),
                    float_arg(-1.0),
                    index_arg(0),
                )
            ],
            text="write mono",
        ),
        trigger(
            "write_stereo",
            ROUTE,
            [
                send_on_press(
                    "/softcut/buffer/write_stereo",
                    string_arg(write_file),
                    float_arg(0.0),
                    float_arg(-1.0),
                )
            ],
            text="write stereo",
        ),
        gap=8,
        name="disk",
    )

    lifecycle = ui.row(
        trigger(
            "reset", SYSTEM, [send_on_press("/softcut/reset")], text="reset voices"
        ),
        trigger("hello", SYSTEM, [send_on_press("/hello")], text="hello"),
        trigger("quit", DANGER, [send_on_press("/quit")], text="quit server"),
        gap=8,
        name="lifecycle",
    )

    return ui.column(
        caption("buffers", size=15, color=DIM, align=LEFT),
        buffers,
        caption(
            f"disk    read {read_file}    write {write_file}",
            size=13,
            color=DIM,
            align=LEFT,
        ),
        disk,
        caption("server", size=15, color=DIM, align=LEFT),
        lifecycle,
        caption(
            "reset restores voice parameters and routing; buffer contents are kept",
            size=12,
            color=DIM,
            align=LEFT,
        ),
        sizes=(1.0, 2.4, 1.0, 2.4, 1.0, 2.4, 1.0),
        gap=10,
        pad=(16, 14),
        name="system",
        color=PANEL,
    )


# --- document ---------------------------------------------------------------


@dataclass
class Options:
    """Everything the layout is parameterised by."""

    voices: int = DEFAULT_VOICES
    size: tuple[int, int] = DEFAULT_SIZE
    max_time: float = DEFAULT_MAX_TIME
    rate: float = DEFAULT_RATE
    read_file: str = DEFAULT_READ_FILE
    write_file: str = DEFAULT_WRITE_FILE
    tabbar_size: float = 42.0


def build(options: Options | None = None) -> py2tosc.Document:
    """Build the whole surface and place it inside the canvas.

    Args:
        options: Voice count, canvas size and parameter ranges. Defaults match
            the OSC server's own: 6 voices on a 1024x768 canvas.

    Returns:
        A resolved document, ready to save or validate.

    Raises:
        ValueError: If the voice count is not between 1 and 6, the range the
            wire protocol and the norns host allow.
    """
    opts = options or Options()
    if not 1 <= opts.voices <= 6:
        raise ValueError(f"softcut has 1 to 6 voices, asked for {opts.voices}")

    width, height = opts.size
    pages = ui.pager(
        mix_page(opts.voices, opts.rate),
        loop_page(opts.voices, opts.max_time, opts.rate),
        rec_page(opts.voices),
        filter_page(opts.voices, "pre"),
        filter_page(opts.voices, "post"),
        routing_page(opts.voices),
        sync_page(opts.voices, opts.max_time),
        system_page(opts.max_time, opts.read_file, opts.write_file),
        name="pages",
        color=PANEL,
        tabbar_size=opts.tabbar_size,
        text_size_off=14,
        text_size_on=14,
    )

    # A PAGER at the root is drawn as the canvas rather than paged, so it goes
    # inside a group -- see the note in py2tosc's ui.pager.
    root = ui.stack(pages, frame=(0, 0, width, height), name="softcut", color=PANEL)
    document = py2tosc.Document(root=root)
    document.resolve()
    return document


def main(argv: Sequence[str] | None = None) -> int:
    """Write the layout to disk from the command line."""
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="build_layout.py",
        description="Generate a TouchOSC surface for the softcut OSC namespace.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=here / "softcut.tosc",
        help="output path; the extension picks the format (.tosc or .xml)",
    )
    parser.add_argument(
        "--no-xml",
        action="store_true",
        help="skip the readable .xml export written next to a .tosc output",
    )
    parser.add_argument("--voices", type=int, default=DEFAULT_VOICES)
    parser.add_argument(
        "--size",
        default=f"{DEFAULT_SIZE[0]}x{DEFAULT_SIZE[1]}",
        help="canvas size, as WIDTHxHEIGHT",
    )
    parser.add_argument(
        "--max-time",
        type=float,
        default=DEFAULT_MAX_TIME,
        help="seconds spanned by the loop point, position and phase controls",
    )
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="rate faders span -RATE..RATE"
    )
    parser.add_argument("--read-file", default=DEFAULT_READ_FILE)
    parser.add_argument("--write-file", default=DEFAULT_WRITE_FILE)
    args = parser.parse_args(argv)

    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
    except ValueError:
        parser.error(f"--size wants WIDTHxHEIGHT, got {args.size!r}")

    document = build(
        Options(
            voices=args.voices,
            size=(width, height),
            max_time=args.max_time,
            rate=args.rate,
            read_file=args.read_file,
            write_file=args.write_file,
        )
    )

    issues = document.validate()
    for issue in issues:
        print(issue)
    if any(issue.level == "error" for issue in issues):
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    document.save(args.output)
    written = [args.output]
    if args.output.suffix == ".tosc" and not args.no_xml:
        xml = args.output.with_suffix(".xml")
        document.save(xml)
        written.append(xml)

    controls = len(document.find_all())
    messages = sum(len(c.messages) for c in document.walk())
    print(
        f"{args.voices} voices, {len(document.root.children[0].children)} pages, "
        f"{controls} controls, {messages} messages -> "
        + ", ".join(str(p) for p in written)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
