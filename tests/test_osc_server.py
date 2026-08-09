"""End-to-end tests for the softcut OSC server (softcut.osc).

Each test sends real OSC datagrams over UDP to a server bound to an ephemeral
port and asserts the resulting host state, exercising the wire dispatch and the
0-based -> 1-based index translation. Skipped entirely without python-osc.

UDP is lossy, so the control-path tests resend their (idempotent) messages via
``drive`` until the host state converges, rather than sending once and hoping
the datagram arrived. This keeps them deterministic under full-suite load.
"""

import threading
import time

import numpy as np
import pytest

osc = pytest.importorskip("softcut.osc")
pythonosc_server = pytest.importorskip("pythonosc.osc_server")
pythonosc_dispatcher = pytest.importorskip("pythonosc.dispatcher")
pythonosc_client = pytest.importorskip("pythonosc.udp_client")

from softcut.norns import NornsSoftcut  # noqa: E402
from softcut.osc import SoftcutOSC  # noqa: E402

SR = 48000.0


def drive(client, messages, pred, timeout=2.0, interval=0.02):
    """Resend ``messages`` (list of ``(address, args)``) until ``pred`` holds.

    All softcut ``set`` operations are idempotent, so resending is safe and
    makes the test robust against a dropped loopback datagram.
    """
    end = time.time() + timeout
    while time.time() < end:
        for address, args in messages:
            client.send_message(address, args)
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.fixture(params=["python-osc", "native"])
def backend(request):
    """Run each dispatch test against both transports (native when built)."""
    if request.param == "native" and not osc.NATIVE_OSC_AVAILABLE:
        pytest.skip("native OSC transport not built (SOFTCUT_ENABLE_TINYOSC)")
    return request.param


@pytest.fixture
def server(backend):
    """A running server on an ephemeral port with a small offline host."""
    host = NornsSoftcut(sample_rate=SR, buffer_frames=2**16, mode="playback")
    srv = SoftcutOSC(
        host,
        backend=backend,
        listen_host="127.0.0.1",
        listen_port=0,
        reply_host="127.0.0.1",
        reply_port=0,
    )
    srv.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def client(server):
    _host, port = server.server_address
    return pythonosc_client.SimpleUDPClient("127.0.0.1", port)


def test_set_param_rate_and_voice_offset(server, client):
    # Wire voice 0 -> host voice 1 -> engine[0]; voice 1 -> engine[1].
    eng = server.host.engine
    assert drive(
        client,
        [("/set/param/cut/rate", [0, 1.5]), ("/set/param/cut/rate", [1, -0.25])],
        lambda: eng[0].rate == 1.5 and eng[1].rate == -0.25,
    )


def test_set_level_and_pan(server, client):
    eng = server.host.engine
    assert drive(
        client,
        [("/set/level/cut", [2, 0.75]), ("/set/pan/cut", [2, -0.5])],
        lambda: eng[2].level == 0.75 and eng[2].pan == -0.5,
    )


def test_bool_flag_params(server, client):
    eng = server.host.engine
    msgs = [
        ("/set/param/cut/play_flag", [0, 1]),
        ("/set/param/cut/loop_flag", [0, 1]),
        ("/set/param/cut/rec_flag", [0, 0]),
    ]
    assert drive(
        client,
        msgs,
        lambda: eng[0].play is True and eng[0].loop is True and eng[0].rec is False,
    )


def test_buffer_assignment_offset(server, client):
    # Wire buffer 1 -> host buffer 2.
    host = server.host
    assert drive(
        client,
        [("/set/param/cut/buffer", [0, 1])],
        lambda: host.engine[0].buffer is host.buffers[2],
    )


def test_level_cut_cut_feedback(server, client):
    # (src=0, dst=1, level) -> feedback voice 0 into voice 1.
    eng = server.host.engine
    assert drive(
        client,
        [("/set/level/cut_cut", [0, 1, 0.5])],
        lambda: eng.feedback(0, 1) == 0.5,
    )


def test_in_cut_sets_input_gain(server, client):
    # (in_channel, voice, level); input channel ignored (scalar input gain).
    eng = server.host.engine
    assert drive(
        client,
        [("/set/level/in_cut", [0, 3, 0.25])],
        lambda: eng[3].input_gain == 0.25,
    )


def test_buffer_clear_region(server, client):
    host = server.host
    for b in host.buffers.values():
        b[:] = 1.0
    n = int(0.1 * SR)
    # clear is idempotent; resend until both channels' region reads zero.
    assert drive(
        client,
        [("/softcut/buffer/clear_region", [0.0, 0.1])],
        lambda: (
            float(host.buffers[1][: n - 1].max()) == 0.0
            and float(host.buffers[2][: n - 1].max()) == 0.0
        ),
    )
    assert host.buffers[1][n + 10] == 1.0  # outside the cleared region


def test_reset_restores_buffer_routing(server, client):
    host = server.host
    host.buffer(1, 2)  # route voice 1 to buffer 2
    assert host.engine[0].buffer is host.buffers[2]
    assert drive(
        client,
        [("/softcut/reset", [])],
        lambda: host.engine[0].buffer is host.buffers[1],
    )


def test_unsupported_messages_do_not_break_server(server, client):
    # Slew/VU are accepted-and-ignored; the server must keep serving after them.
    eng = server.host.engine
    msgs = [
        ("/set/param/cut/level_slew_time", [0, 0.1]),
        ("/set/param/cut/pan_slew_time", [0, 0.1]),
        ("/poll/start/vu", []),
        ("/set/param/cut/rate", [0, 0.5]),
    ]
    assert drive(client, msgs, lambda: eng[0].rate == 0.5)


def test_malformed_message_is_isolated(server, client):
    # A message missing its value argument must not kill the server thread.
    eng = server.host.engine
    msgs = [
        ("/set/param/cut/rate", [0]),  # missing float -> handler error, isolated
        ("/set/param/cut/rate", [0, 0.75]),
    ]
    assert drive(client, msgs, lambda: eng[0].rate == 0.75)


def test_phase_poll_emits_on_change(backend):
    received = []
    disp = pythonosc_dispatcher.Dispatcher()
    disp.map("/poll/softcut/phase", lambda a, *args: received.append(tuple(args)))
    receiver = pythonosc_server.ThreadingOSCUDPServer(("127.0.0.1", 0), disp)
    _rh, rp = receiver.server_address
    threading.Thread(target=receiver.serve_forever, daemon=True).start()

    host = NornsSoftcut(sample_rate=SR, buffer_frames=2**16, mode="playback")
    v = host.engine[0]
    v.buffer = np.random.randn(2**16).astype(np.float32)
    v.loop_start, v.loop_end, v.loop = 0.0, 1.0, True
    v.rate, v.play, v.phase_quant = 1.0, True, 0.05
    v.cut_to(0.0)

    srv = SoftcutOSC(
        host,
        backend=backend,
        listen_host="127.0.0.1",
        listen_port=0,
        reply_host="127.0.0.1",
        reply_port=rp,
    )
    try:
        host.render(np.zeros(int(0.5 * SR), np.float32))  # advance voice 0 to ~0.45

        def emitted_voice0():
            # Force a re-emit each attempt (robust to a dropped datagram);
            # poll_once only sends when the quantized phase changed. reset() is
            # backend-agnostic (Python _PhasePoll and the native _OscPhasePoll).
            srv.phase_poll.reset()
            srv.phase_poll.poll_once()
            return any(a[0] == 0 for a in received)

        assert wait_until_pred(emitted_voice0)
        v0 = [a for a in received if a[0] == 0]
        assert abs(v0[-1][1] - 0.45) < 1e-3
    finally:
        srv.shutdown()
        receiver.shutdown()
        receiver.server_close()


def test_phase_poll_survives_a_failing_send():
    """A send that raises must not take the poll thread down with it.

    Nothing restarts the thread, so an exception escaping the loop would end
    phase reporting for the life of the process. The failure is counted and the
    scan keeps running.
    """

    class Failing:
        def __init__(self):
            self.attempts = 0

        def send_message(self, address, args):
            self.attempts += 1
            raise OverflowError("float too large to pack with f format")

    host = NornsSoftcut(sample_rate=SR, buffer_frames=2**16, mode="playback")
    client = Failing()
    poll = osc._PhasePoll(host, client, period=0.001)
    poll.start()
    try:
        assert wait_until_pred(lambda: poll.errors >= 3)
        assert poll.running  # still scanning after repeated failures
        assert client.attempts >= 3
    finally:
        poll.stop()

    # poll_once itself still raises, for callers driving it by hand.
    poll.reset()
    with pytest.raises(OverflowError):
        poll.poll_once()


def wait_until_pred(pred, timeout=2.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()
