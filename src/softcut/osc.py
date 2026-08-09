"""OSC server exposing softcut over the monome softcut wire protocol.

This mirrors the OSC namespace of the reference ``softcut_jack_osc`` client so
that existing norns/Lua scripts, SuperCollider, Max, or any OSC controller can
drive softcut-py as a drop-in engine. It is a thin dispatch layer over the
:class:`softcut.norns.NornsSoftcut` host: every address maps to a host method,
with the one translation that the wire protocol is **0-based** (voices 0-5,
buffers 0-1) while the norns host is 1-based, so indices gain ``+1`` here.

Two transports are available and share one dispatch table:

- ``python-osc`` (the ``osc`` extra: ``pip install softcut-py[osc]``): the
  default pure-Python transport; the core package stays numpy-only.
- ``native`` (**experimental**): a dependency-free UDP transport built on the
  vendored tinyosc codec, compiled in with the CMake option
  ``SOFTCUT_ENABLE_TINYOSC`` (reported by :data:`softcut._core.HAVE_TINYOSC`). It
  is not built into the published wheels -- opt in with a source build.
  Receiving and parsing happen in C, but dispatch runs under the GIL because the
  DSP command queue is single-producer; it is a dependency-free transport, not a
  GIL-free fast path. IPv4-only and less battle-tested than python-osc.

``backend="auto"`` prefers native when built, else python-osc. Defaults match
the reference: listen on UDP 9999, reply (phase poll) to 127.0.0.1:57120. Run as
a server with ``python -m softcut.osc``.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Callable, Optional

from . import _core
from .norns import NornsSoftcut

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient

    HAVE_PYTHONOSC = True
except ImportError:  # pragma: no cover - exercised only without the extra
    HAVE_PYTHONOSC = False

#: True when the optional native OSC transport (tinyosc) was compiled into _core.
NATIVE_OSC_AVAILABLE = bool(getattr(_core, "HAVE_TINYOSC", False))

DEFAULT_LISTEN_PORT = 9999
DEFAULT_REPLY_HOST = "127.0.0.1"
DEFAULT_REPLY_PORT = 57120  # SuperCollider's sclang default, as in the reference

_log = logging.getLogger("softcut.osc")

Handler = Callable[..., None]

# /set/param/cut/<suffix> -> NornsSoftcut method name. Each is an ``if`` message
# (voice index, float value). Suffixes match the reference OSC namespace; the
# flag suffixes (loop_flag/rec_flag/play_flag) map to the boolean host setters.
_PARAM_MAP: dict[str, str] = {
    "rate": "rate",
    "loop_start": "loop_start",
    "loop_end": "loop_end",
    "loop_flag": "loop",
    "fade_time": "fade_time",
    "rec_level": "rec_level",
    "pre_level": "pre_level",
    "rec_flag": "rec",
    "rec_once": "rec_once",
    "play_flag": "play",
    "rec_offset": "rec_offset",
    "position": "position",
    "recpre_slew_time": "recpre_slew_time",
    "rate_slew_time": "rate_slew_time",
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


class _PhasePoll:
    """Background phase reporter, the analogue of the reference's phase poll.

    While running, it scans each voice's quantized phase every ``period``
    seconds and sends ``/poll/softcut/phase <voice:int> <phase:float>`` (voice
    0-based) to the reply client whenever a voice's quantized phase changes.
    """

    ADDRESS = "/poll/softcut/phase"

    def __init__(self, host: NornsSoftcut, client, period: float = 0.01) -> None:
        self._host = host
        self._client = client
        self._period = float(period)
        self._n = len(host.engine)
        self._last: list[Optional[float]] = [None] * self._n
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._errors = 0

    def poll_once(self) -> None:
        """Scan all voices once, emitting a message for each changed phase."""
        eng = self._host.engine
        for i in range(self._n):
            phase = float(eng[i].quant_phase)
            if phase != self._last[i]:
                # Recorded only once the send has gone out, so a scan that
                # fails retries that voice rather than dropping the update it
                # never managed to report.
                self._client.send_message(self.ADDRESS, [i, phase])
                self._last[i] = phase

    def reset(self) -> None:
        """Clear change detection so the next poll re-emits every voice."""
        self._last = [None] * self._n

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._last = [None] * self._n
        self._errors = 0
        self._thread = threading.Thread(
            target=self._run, name="softcut-osc-phase", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def errors(self) -> int:
        """How many scans have failed since the poll was last started."""
        return self._errors

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # Nothing restarts this thread, so an exception escaping here
                # would end phase reporting for the life of the process -- and
                # a transient one (an unroutable reply address, a socket
                # hiccup) is not worth that. The first is logged with its
                # traceback and the rest are counted, so a persistent fault
                # stays visible through ``errors`` without flooding the log.
                # ``poll_once`` itself still raises, for callers driving it
                # directly.
                self._errors += 1
                if self._errors == 1:
                    _log.exception("phase poll failed; further errors suppressed")
            self._stop.wait(self._period)


class SoftcutOSC:
    """An OSC server driving a :class:`NornsSoftcut` host over the softcut protocol.

    Construct it with an optional pre-built host (else a fresh 6-voice host is
    created) and a ``backend`` ("auto", "python-osc", or "native"), then either
    :meth:`serve_forever` / :meth:`wait_for_quit` (blocking) or :meth:`start` /
    :meth:`shutdown` (background, used by tests). The server never opens the
    audio device itself; call ``host.start()`` (or use ``python -m``) to go live.
    """

    def __init__(
        self,
        host: Optional[NornsSoftcut] = None,
        *,
        backend: str = "auto",
        listen_host: str = "0.0.0.0",
        listen_port: int = DEFAULT_LISTEN_PORT,
        reply_host: str = DEFAULT_REPLY_HOST,
        reply_port: int = DEFAULT_REPLY_PORT,
        phase_period: float = 0.01,
    ) -> None:
        self.backend = self._resolve_backend(backend)
        self.host = host if host is not None else NornsSoftcut()
        self._listen_host = listen_host
        self._quit = threading.Event()
        self._handlers = self._build_handlers()

        if self.backend == "native":
            # Native outbound: a C phase-poll thread reads quant_phase and sends
            # the reply entirely in C, so no periodic GIL holder remains. It owns
            # its own reply socket, so no separate _OscSender is needed.
            self._sender = None
            self._phase = _core._OscPhasePoll(
                self.host.engine._core, reply_host, reply_port, phase_period
            )
        else:
            self._sender = SimpleUDPClient(reply_host, reply_port)
            self._phase = _PhasePoll(self.host, self._sender, period=phase_period)

        if self.backend == "native":
            # A weakref breaks the SoftcutOSC <-> receiver <-> callback cycle
            # (nanobind objects do not participate in Python's cyclic GC).
            weak = weakref.ref(self)

            def native_cb(address: str, args: object, _weak=weak) -> None:
                s = _weak()
                if s is not None:
                    s._dispatch(address, list(args))  # type: ignore[arg-type]

            # Hand the low-level engine to the receiver so per-voice
            # /set/param/cut/* messages dispatch in C without the GIL; the
            # callback handles every other address.
            self._receiver = _core._OscReceiver(
                listen_host, listen_port, native_cb, self.host.engine._core
            )
            self._server = None
            self._thread = None
        else:
            dispatcher = Dispatcher()
            dispatcher.set_default_handler(
                lambda address, *a: self._dispatch(address, list(a))
            )
            self._server = ThreadingOSCUDPServer((listen_host, listen_port), dispatcher)
            self._receiver = None
            self._thread = None

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend == "auto":
            if NATIVE_OSC_AVAILABLE:
                return "native"
            if HAVE_PYTHONOSC:
                return "python-osc"
            raise ImportError(
                "no OSC transport available: build with SOFTCUT_ENABLE_TINYOSC "
                "or install softcut-py[osc]."
            )
        if backend == "native":
            if not NATIVE_OSC_AVAILABLE:
                raise RuntimeError(
                    "native OSC transport not built (SOFTCUT_ENABLE_TINYOSC=ON)."
                )
            return "native"
        if backend == "python-osc":
            if not HAVE_PYTHONOSC:
                raise ImportError(
                    "python-osc not installed; `pip install softcut-py[osc]`."
                )
            return "python-osc"
        raise ValueError(f"unknown OSC backend {backend!r}")

    # --- introspection ---------------------------------------------------

    @property
    def server_address(self) -> tuple[str, int]:
        """The bound (host, port) the server is listening on."""
        if self.backend == "native":
            return (self._listen_host, self._receiver.port)
        return self._server.server_address

    @property
    def phase_poll(self) -> _PhasePoll:
        return self._phase

    @property
    def quit_requested(self) -> bool:
        return self._quit.is_set()

    # --- lifecycle -------------------------------------------------------

    def serve_forever(self) -> None:
        """Block serving OSC (python-osc backend) until :meth:`shutdown`.

        The native backend receives on its own C++ thread, so use
        :meth:`start` + :meth:`wait_for_quit` there.
        """
        if self.backend == "native":
            self.start()
            self.wait_for_quit()
        else:
            self._server.serve_forever()

    def wait_for_quit(self, poll: float = 0.25) -> None:
        """Block until a ``/quit`` (or ``/goodbye``) message is received."""
        while not self._quit.wait(poll):
            pass

    def start(self) -> "SoftcutOSC":
        """Serve OSC on a background thread and return immediately."""
        if self.backend == "native":
            self._receiver.start()
            return self
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="softcut-osc", daemon=True
        )
        self._thread.start()
        return self

    def shutdown(self) -> "SoftcutOSC":
        """Stop the phase poll and the server, releasing the socket."""
        self._phase.stop()
        if self.backend == "native":
            self._receiver.stop()
            return self
        # python-osc: BaseServer.shutdown() blocks until serve_forever
        # acknowledges, so only call it when the serve loop is running.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=1.0)
            self._thread = None
        self._server.server_close()
        return self

    def __enter__(self) -> "SoftcutOSC":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # --- dispatch --------------------------------------------------------

    def _dispatch(self, address: str, args: list) -> None:
        """Route one parsed OSC message to its handler, isolating errors so a
        malformed message can never take down the receive thread."""
        fn = self._handlers.get(address)
        if fn is None:
            _log.debug("no handler for OSC address: %s %r", address, args)
            return
        try:
            fn(address, *args)
        except Exception:
            _log.exception("OSC handler error: %s %r", address, args)

    def _build_handlers(self) -> dict[str, Handler]:
        host = self.host
        h: dict[str, Handler] = {}

        # lifecycle
        h["/hello"] = lambda a, *x: _log.info("hello")
        h["/goodbye"] = lambda a, *x: self._quit.set()
        h["/quit"] = lambda a, *x: self._quit.set()

        # softcut phase poll control (VU poll is unsupported: no meter, and it is
        # dead code in the reference too)
        h["/poll/start/cut/phase"] = lambda a, *x: self._phase.start()
        h["/poll/stop/cut/phase"] = lambda a, *x: self._phase.stop()
        h["/poll/start/vu"] = self._unsupported("/poll/start/vu")
        h["/poll/stop/vu"] = lambda a, *x: None

        # routing / mixer
        h["/set/level/cut"] = self._v_f(host.level)
        h["/set/pan/cut"] = self._v_f(host.pan)
        h["/set/enabled/cut"] = self._v_f(self._set_enabled)
        h["/set/level/cut_cut"] = self._ii_f(host.level_cut_cut)
        h["/set/level/in_cut"] = self._on_in_cut

        # per-voice params (all ``if``)
        for suffix, method in _PARAM_MAP.items():
            h[f"/set/param/cut/{suffix}"] = self._v_f(getattr(host, method))

        # multi-arg / special params
        h["/set/param/cut/voice_sync"] = self._ii_f(host.voice_sync)
        h["/set/param/cut/buffer"] = self._on_buffer
        # level/pan slew are per-sample DSP slews softcut-lib supports but the
        # binding does not yet expose; accept and ignore rather than error.
        h["/set/param/cut/level_slew_time"] = self._unsupported("level_slew_time")
        h["/set/param/cut/pan_slew_time"] = self._unsupported("pan_slew_time")

        # buffer / disk operations
        h["/softcut/buffer/read_mono"] = self._on_read_mono
        h["/softcut/buffer/read_stereo"] = self._on_read_stereo
        h["/softcut/buffer/write_mono"] = self._on_write_mono
        h["/softcut/buffer/write_stereo"] = self._on_write_stereo
        h["/softcut/buffer/clear"] = lambda a, *x: host.buffer_clear()
        h["/softcut/buffer/clear_channel"] = self._on_clear_channel
        h["/softcut/buffer/clear_region"] = self._on_clear_region
        h["/softcut/buffer/clear_region_channel"] = self._on_clear_region_channel
        h["/softcut/reset"] = lambda a, *x: host.reset()

        return h

    # --- handler factories -----------------------------------------------

    @staticmethod
    def _v_f(method: Callable[[int, float], None]) -> Handler:
        """``(voice, float)`` -> ``method(voice + 1, float)`` (0-based wire)."""

        def h(address: str, *args: object) -> None:
            v, x = args[0], args[1]
            method(int(v) + 1, float(x))  # type: ignore[arg-type]

        return h

    @staticmethod
    def _ii_f(method: Callable[[int, int, float], None]) -> Handler:
        """``(i0, i1, float)`` -> ``method(i0 + 1, i1 + 1, float)``."""

        def h(address: str, *args: object) -> None:
            a, b, x = args[0], args[1], args[2]
            method(int(a) + 1, int(b) + 1, float(x))  # type: ignore[arg-type]

        return h

    @staticmethod
    def _unsupported(name: str) -> Handler:
        def h(address: str, *args: object) -> None:
            _log.debug("unsupported OSC message ignored: %s %r", name, args)

        return h

    # --- individual handlers ---------------------------------------------

    def _set_enabled(self, i: int, state: float) -> None:
        # No per-voice idle-disable in the core; approximate enable with play.
        self.host.play(i, bool(state))

    def _on_in_cut(self, address: str, *args: object) -> None:
        # Reference: (in_channel, voice, level). The core has only a scalar
        # per-voice input gain (mono duplex), not a per-channel ADC matrix, so
        # the input channel is ignored.
        _in_ch, v, level = args[0], args[1], args[2]
        self.host.engine[int(v)].input_gain = float(level)  # type: ignore[arg-type]

    def _on_buffer(self, address: str, *args: object) -> None:
        v, b = args[0], args[1]
        self.host.buffer(int(v) + 1, int(b) + 1)  # type: ignore[arg-type]

    def _on_read_mono(self, address: str, *args: object) -> None:
        path = str(args[0])
        start_src = float(args[1]) if len(args) > 1 else 0.0
        start_dst = float(args[2]) if len(args) > 2 else 0.0
        dur = float(args[3]) if len(args) > 3 else -1.0
        ch_src = int(args[4]) if len(args) > 4 else 0  # type: ignore[arg-type]
        ch_dst = int(args[5]) if len(args) > 5 else 0  # type: ignore[arg-type]
        self.host.buffer_read_mono(
            path, start_src, start_dst, dur, ch_src + 1, ch_dst + 1
        )

    def _on_read_stereo(self, address: str, *args: object) -> None:
        path = str(args[0])
        start_src = float(args[1]) if len(args) > 1 else 0.0
        start_dst = float(args[2]) if len(args) > 2 else 0.0
        dur = float(args[3]) if len(args) > 3 else -1.0
        self.host.buffer_read_stereo(path, start_src, start_dst, dur)

    def _on_write_mono(self, address: str, *args: object) -> None:
        path = str(args[0])
        start = float(args[1]) if len(args) > 1 else 0.0
        dur = float(args[2]) if len(args) > 2 else -1.0
        ch = int(args[3]) if len(args) > 3 else 0  # type: ignore[arg-type]
        self.host.buffer_write_mono(path, start, dur, ch + 1)

    def _on_write_stereo(self, address: str, *args: object) -> None:
        path = str(args[0])
        start = float(args[1]) if len(args) > 1 else 0.0
        dur = float(args[2]) if len(args) > 2 else -1.0
        self.host.buffer_write_stereo(path, start, dur)

    def _on_clear_channel(self, address: str, *args: object) -> None:
        self.host.buffer_clear_channel(int(args[0]) + 1)  # type: ignore[arg-type]

    def _on_clear_region(self, address: str, *args: object) -> None:
        start, dur = float(args[0]), float(args[1])
        self.host.buffer_clear_region(start, dur)

    def _on_clear_region_channel(self, address: str, *args: object) -> None:
        ch, start, dur = int(args[0]), float(args[1]), float(args[2])  # type: ignore[arg-type]
        self.host.buffer_clear_region_channel(ch + 1, start, dur)


def main(argv: Optional[list[str]] = None) -> int:
    """Run a softcut OSC server from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m softcut.osc",
        description="Serve softcut over the monome softcut OSC protocol.",
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--reply-host", default=DEFAULT_REPLY_HOST)
    parser.add_argument("--reply-port", type=int, default=DEFAULT_REPLY_PORT)
    parser.add_argument(
        "--backend", choices=["auto", "python-osc", "native"], default="auto"
    )
    parser.add_argument("--voices", type=int, default=6)
    parser.add_argument("--sample-rate", type=float, default=48000.0)
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Do not open the audio device (offline: buffer ops only).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s",
    )

    host = NornsSoftcut(sample_rate=args.sample_rate, voices=args.voices)
    server = SoftcutOSC(
        host,
        backend=args.backend,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        reply_host=args.reply_host,
        reply_port=args.reply_port,
    )
    if not args.no_audio:
        host.start()

    server.start()
    bound_host, bound_port = server.server_address
    _log.info("softcut OSC server listening on %s:%d", bound_host, bound_port)
    _log.info("phase replies to %s:%d", args.reply_host, args.reply_port)
    _log.info("transport: %s", server.backend)
    try:
        server.wait_for_quit()  # until /quit, /goodbye, or Ctrl-C
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if not args.no_audio:
            host.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
