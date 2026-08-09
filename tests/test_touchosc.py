"""The generated TouchOSC surface, checked against the OSC server it drives.

``clients/touchosc/build_layout.py`` writes a ``.tosc`` layout whose controls
send softcut's own OSC namespace. The risk in a generated control surface is
silent drift: an address that no longer exists, an argument in the wrong order,
a voice index off by one. So rather than inspect the XML, these tests pull every
binding out of the layout, synthesise the message TouchOSC would send, and push
it through the real dispatch table -- the layout is only correct if the server
it targets accepts every message in it.

Skipped without py2tosc (``pip install py2tosc``) or python-osc.
"""

import importlib.util
import socket
import sys
from pathlib import Path

import numpy as np
import pytest

py2tosc = pytest.importorskip("py2tosc")
pytest.importorskip("pythonosc.udp_client")

from softcut._wavio import write_wav  # noqa: E402
from softcut.norns import _FLOAT_PARAMS, NornsSoftcut  # noqa: E402
from softcut.osc import SoftcutOSC  # noqa: E402

SR = 48000.0
BUFFER_FRAMES = 2**21  # ~43 s at 48 kHz, so a 32 s clear region stays in range
BUILDER = (
    Path(__file__).resolve().parents[1] / "clients" / "touchosc" / "build_layout.py"
)


@pytest.fixture(scope="module")
def builder():
    """The generator script, imported by path (clients/ is not a package)."""
    if not BUILDER.exists():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"{BUILDER} not present")
    spec = importlib.util.spec_from_file_location("softcut_touchosc_builder", BUILDER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audio_file(tmp_path_factory):
    """A short stereo WAV for the layout's disk-read buttons to point at."""
    path = tmp_path_factory.mktemp("touchosc") / "loop.wav"
    write_wav(path, np.zeros((int(0.1 * SR), 2), dtype=np.float32), int(SR))
    return path


@pytest.fixture(scope="module")
def document(builder, audio_file):
    """The shipped layout, with its disk paths pointed at the temporary files."""
    return builder.build(
        builder.Options(
            read_file=str(audio_file),
            write_file=str(audio_file.parent / "out.wav"),
        )
    )


@pytest.fixture
def server():
    """A server on an ephemeral port over a small offline host.

    The phase-poll reply goes to a real bound socket rather than port 0, because
    one of the layout's buttons starts the poll for real.
    """
    reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply.bind(("127.0.0.1", 0))
    host = NornsSoftcut(sample_rate=SR, buffer_frames=BUFFER_FRAMES, mode="playback")
    srv = SoftcutOSC(
        host,
        backend="python-osc",
        listen_host="127.0.0.1",
        listen_port=0,
        reply_host="127.0.0.1",
        reply_port=reply.getsockname()[1],
    )
    try:
        yield srv
    finally:
        srv.shutdown()
        reply.close()


# --- reading the layout back ------------------------------------------------


def bindings(document):
    """Every OSC binding in the layout, as ``(control, message)`` pairs."""
    for control in document.walk():
        for message in control.messages:
            if isinstance(message, py2tosc.OscMessage):
                yield control, message


def address_of(message):
    """The address the binding sends to. Every partial in it is a constant."""
    assert all(str(p.type) == "CONSTANT" for p in message.path)
    return "".join(str(p.value) for p in message.path)


def arguments_of(message, x=1.0):
    """The arguments TouchOSC would send with the control's value at ``x``.

    Constants are converted the way their partial says; the one VALUE partial is
    the control's own position, mapped from 0..1 onto the partial's scale.
    """
    out = []
    for partial in message.arguments:
        kind, conversion = str(partial.type), str(partial.conversion)
        if kind == "CONSTANT":
            if conversion == "INTEGER":
                out.append(int(partial.value))
            elif conversion == "FLOAT":
                out.append(float(partial.value))
            else:
                out.append(str(partial.value))
        elif kind == "VALUE":
            span = partial.scale_max - partial.scale_min
            out.append(float(partial.scale_min + x * span))
        else:  # pragma: no cover - the layout uses no other partial type
            raise AssertionError(f"unexpected argument partial {kind}")
    return out


def sent_by(document, name):
    """The (address, message) pairs of every control with this name."""
    return [
        (address_of(m), m)
        for c, m in bindings(document)
        if c.get("name") == name and m.send
    ]


# --- the layout itself ------------------------------------------------------


def test_layout_validates_clean(document):
    errors = [i for i in document.validate() if i.level == "error"]
    assert errors == []


def test_layout_round_trips_through_a_file(document, tmp_path):
    path = tmp_path / "softcut.tosc"
    document.save(path)
    reloaded = py2tosc.load(path)
    assert len(reloaded.find_all()) == len(document.find_all())
    assert len(list(bindings(reloaded))) == len(list(bindings(document)))


def test_every_page_is_present(document):
    pager = document.root.children[0]
    assert pager.control_type.value == "PAGER"
    assert [p.get("name") for p in pager.children] == [
        "mix",
        "loop",
        "rec",
        "pre",
        "post",
        "routing",
        "sync",
        "system",
    ]


def test_voice_count_is_honoured(builder):
    two = builder.build(builder.Options(voices=2))
    assert two.find("strip3") is None
    assert two.find("strip2") is not None
    with pytest.raises(ValueError):
        builder.build(builder.Options(voices=7))


# --- the layout against the server ------------------------------------------


def test_every_address_is_served(document, server):
    """No control sends to an address the dispatch table does not know."""
    unknown = {
        address_of(m)
        for _c, m in bindings(document)
        if m.send and address_of(m) not in server._handlers
    }
    assert unknown == set()


def test_the_surface_covers_the_namespace(document, server):
    """Every served address has a control, bar the ones deliberately left off.

    This is the drift guard in the other direction: an address added to the
    server needs either a control or a line in this set.
    """
    unmapped = {
        "/goodbye",  # a synonym for /quit, which is on the surface
        "/poll/start/vu",  # no VU meter in the binding
        "/poll/stop/vu",
        "/set/param/cut/level_slew_time",  # accepted-and-ignored by the server
        "/set/param/cut/pan_slew_time",
    }
    sent = {address_of(m) for _c, m in bindings(document) if m.send}
    assert set(server._handlers) - sent == unmapped


def test_every_binding_uses_only_the_first_connection(document):
    """The surface talks to connection 1 and nothing else.

    TouchOSC has ten connection slots and enables all of them by default, so a
    second OSC destination added later would receive the whole surface too.
    """
    masks = {m.connections for _c, m in bindings(document)}
    assert masks == {"1000000000"}


def test_only_the_poll_reply_is_listened_for(document):
    """The one address the layout receives on is the one softcut transmits."""
    listening = {address_of(m) for _c, m in bindings(document) if m.receive}
    assert listening == {"/poll/softcut/phase"}
    assert not any(m.send and m.receive for _c, m in bindings(document))


@pytest.mark.parametrize("x", [0.0, 1.0])
def test_every_message_dispatches(document, server, x):
    """Every binding, at both ends of its travel, is accepted by its handler.

    Handlers are called directly rather than over UDP: ``_dispatch`` swallows
    handler exceptions by design, which is right for a live server and useless
    for a test.
    """
    for control, message in bindings(document):
        if not message.send:
            continue
        address = address_of(message)
        handler = server._handlers[address]
        try:
            handler(address, *arguments_of(message, x))
        except Exception as exc:  # pragma: no cover - only on a real failure
            raise AssertionError(
                f"{control.get('name')} -> {address} "
                f"{arguments_of(message, x)!r} raised {exc!r}"
            ) from exc


def test_voice_arguments_are_zero_based_and_complete(document):
    """The per-voice rows address voices 0-5, once each, as the wire expects."""
    for address in ("/set/level/cut", "/set/pan/cut", "/set/param/cut/loop_start"):
        voices = [
            arguments_of(m)[0]
            for _c, m in bindings(document)
            if address_of(m) == address and m.send
        ]
        assert sorted(set(voices)) == list(range(6))


def test_fader_travel_reaches_the_host(document, server):
    """A fader at each end of its travel produces the value the caption claims."""
    engine = server.host.engine
    cases = [
        ("level3", 1.0, lambda: engine[2].level),
        ("pan3", -1.0, lambda: engine[2].pan),
        ("rate3", 2.0, lambda: engine[2].rate),
        ("loop_start2", 32.0, lambda: engine[1].loop_start),
        ("post_fc1", 12000.0, lambda: engine[0].post_filter_fc),
    ]
    for name, expected, read in cases:
        address, message = sent_by(document, name)[0]
        x = 1.0 if expected >= 0 else 0.0
        server._handlers[address](address, *arguments_of(message, x))
        assert read() == pytest.approx(expected, rel=1e-5)


def test_faders_open_where_the_engine_starts(document, server):
    """An untouched fader stands for the value a freshly reset voice holds.

    Checked by sending it: replaying every fader at the position it opens at
    must leave a fresh voice exactly as it was. That catches a stale default and
    a range the default cannot be expressed in, which a table comparison would
    not.
    """
    watched = sorted(set(_FLOAT_PARAMS.values()) | {"level", "pan", "input_gain"})
    voice = server.host.engine[0]
    before = {name: getattr(voice, name) for name in watched}

    replayed = 0
    for control, message in bindings(document):
        if control.control_type.value != "FADER" or not message.send:
            continue
        if arguments_of(message)[0] != 0:  # voice 0's column only
            continue
        address = address_of(message)
        (resting,) = [v.default for v in control.values if v.key == "x"]
        server._handlers[address](address, *arguments_of(message, float(resting)))
        replayed += 1

    assert replayed >= 25  # the whole of voice 0, across every page
    after = {name: getattr(voice, name) for name in watched}
    for name, was in before.items():
        assert after[name] == pytest.approx(was, rel=1e-5, abs=1e-7), name


def test_the_sync_diagonal_is_not_a_button(document):
    """Syncing a voice to itself does nothing, so no message is bound to it."""
    for v in range(6):
        assert document.find(f"sync{v + 1}_{v + 1}").control_type.value == "BOX"
        assert document.find(f"sync{v + 1}_{v + 1}").messages == []
    assert document.find("sync2_5").control_type.value == "BUTTON"


def test_toggles_carry_both_states(document, server):
    """A latching button sends 1 when it goes on and 0 when it goes off."""
    engine = server.host.engine
    address, message = sent_by(document, "play2")[0]
    assert address == "/set/param/cut/play_flag"

    server._handlers[address](address, *arguments_of(message, 1.0))
    assert engine[1].play is True
    server._handlers[address](address, *arguments_of(message, 0.0))
    assert engine[1].play is False


def test_buffer_buttons_assign_both_buffers(document, server):
    """The buffer row assigns voice 1 to either global buffer, 0-based on the wire."""
    host = server.host
    for button, buffer in (("buf1_1", 1), ("buf2_1", 2)):
        address, message = sent_by(document, button)[0]
        assert address == "/set/param/cut/buffer"
        server._handlers[address](address, *arguments_of(message))
        assert host.engine[0].buffer is host.buffers[buffer]


def test_feedback_matrix_is_source_by_destination(document, server):
    """Row 2, column 5 of the matrix routes voice 2 into voice 5, not the reverse."""
    address, message = sent_by(document, "cut_cut2_5")[0]
    assert address == "/set/level/cut_cut"
    assert arguments_of(message)[:2] == [1, 4]  # 0-based on the wire


def test_phase_readout_only_listens(document):
    """The phase faders display the poll reply; they must not transmit or move."""
    for voice in range(6):
        control = document.find(f"phase{voice + 1}")
        assert control.get("interactive") is False
        (message,) = control.messages
        assert address_of(message) == "/poll/softcut/phase"
        assert message.receive is True and message.send is False
        assert arguments_of(message)[0] == voice


def test_disk_buttons_carry_a_path_and_read_into_the_buffer(
    document, server, audio_file
):
    """The read button's constant path argument reaches the host's WAV reader."""
    host = server.host
    host.buffers[1][:] = 1.0
    address, message = sent_by(document, "read_mono")[0]
    args = arguments_of(message)
    assert args[0] == str(audio_file)
    server._handlers[address](address, *args)
    assert host.buffers[1][: int(0.05 * SR)].max() == 0.0  # the silent file landed
