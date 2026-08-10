"""The whole stack end to end: TouchOSC layout -> UDP -> OSC server -> DSP.

Every other demo calls softcut directly. This one drives it the way a controller
does, exercising the layers no other demo touches:

    clients/touchosc/softcut.tosc   the control surface (py2tosc document)
      -> OSC datagrams on the wire  (encoded here, byte for byte as TouchOSC does)
      -> softcut.osc.SoftcutOSC     the server (native tinyosc, or python-osc)
      -> softcut.norns.NornsSoftcut the 1-based host, 0-based on the wire
      -> softcut.Engine / Voice     the DSP

Nothing is simulated except the finger. Each step below picks a real control out
of the layout by name, reads the binding the generator gave it, works out the
message that control sends at a given position, and puts that on a real socket.
If the layout, the server and the host ever stop agreeing, this stops making
sound -- which is the point of it.

Audio is rendered offline so the result is deterministic and the demo can check
each parameter landed before moving on. The OSC path is real either way.

Two modes:

    uv run python demos/13_osc_surface.py            # drive it, render a wav
    uv run python demos/13_osc_surface.py --serve     # open the device and wait
                                                      # for the real TouchOSC app

``--serve`` is the half this cannot automate: it starts the server live and
prints what to put in TouchOSC's connection settings, including the reply port
the ``sync`` page's phase readout needs.

Run:  uv run python demos/13_osc_surface.py [--play] [--serve] [--backend auto]
Out:  build/out/13_osc_surface.wav
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "touchosc"))
from _util import DATA, OUT, write_wav  # noqa: E402

SR = 48000.0
SECTION = 3.0  # seconds rendered per step
LOOP = 2.5


# --- the wire ---------------------------------------------------------------


def osc_datagram(address: str, args: list) -> bytes:
    """Encode one OSC 1.0 message, which is all TouchOSC puts on the wire.

    Written out rather than imported so the demo shows the actual bytes: an
    address and a type tag string, each padded to a 4-byte boundary, then the
    arguments big-endian in order.
    """

    def pad(raw: bytes) -> bytes:
        return raw + b"\0" * (4 - len(raw) % 4)

    tags, body = ",", b""
    for arg in args:
        if isinstance(arg, bool):  # bool before int: bool *is* an int
            raise TypeError("softcut's namespace has no boolean arguments")
        if isinstance(arg, int):
            tags += "i"
            body += struct.pack(">i", arg)
        elif isinstance(arg, float):
            tags += "f"
            body += struct.pack(">f", arg)
        else:
            tags += "s"
            body += pad(str(arg).encode())
    return pad(address.encode()) + pad(tags.encode()) + body


# --- reading the surface ----------------------------------------------------


def binding(document, name: str):
    """The (address, message) a named control sends, straight from the layout."""
    control = document.find(name)
    if control is None:
        raise LookupError(f"no control named {name!r} in the layout")
    for message in control.messages:
        if getattr(message, "send", False):
            address = "".join(str(p.value) for p in message.path)
            return address, message
    raise LookupError(f"control {name!r} sends nothing")


def arguments(message, x: float) -> list:
    """What that control sends with its value at ``x`` (0..1 of its travel).

    Constant partials are the voice/buffer indices and file paths the generator
    baked in; the one VALUE partial is the fader position, scaled into the
    parameter's range exactly as TouchOSC scales it.
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
        else:
            span = partial.scale_max - partial.scale_min
            out.append(float(partial.scale_min + x * span))
    return out


class Surface:
    """A finger on the layout: sends what a control sends, over a real socket."""

    def __init__(self, document, port: int, host: str = "127.0.0.1") -> None:
        self._doc = document
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def touch(self, name: str, x: float = 1.0, *, until=None, label: str = "") -> None:
        """Move control ``name`` to ``x`` and wait for the host to show it.

        UDP can drop on loopback, so the message is resent until ``until`` holds
        rather than sent once and hoped for -- every softcut set is idempotent.
        Without ``until`` it is sent once, which is right for the fire-and-forget
        buttons (reset, disk reads) whose effect is not a parameter.
        """
        address, message = binding(self._doc, name)
        args = arguments(message, x)
        shown = " ".join(f"{a:g}" if isinstance(a, float) else str(a) for a in args)
        print(f"    {label or name:<22} {address} {shown}")

        deadline = time.time() + 2.0
        while True:
            self._sock.sendto(osc_datagram(address, args), self._addr)
            if until is None:
                return
            if until():
                return
            if time.time() > deadline:
                raise TimeoutError(f"{name} -> {address} never reached the host")
            time.sleep(0.02)

    def close(self) -> None:
        self._sock.close()


# --- the performance --------------------------------------------------------


def build_layout(read_file: Path):
    """The shipped surface, with the disk button pointed at the demo audio.

    `clients/touchosc/softcut.tosc` is this same layout built with its default
    path; only that constant differs, so the `read_mono` button here does what it
    would do on a tablet with the file present.
    """
    import build_layout as generator

    return generator.build(generator.Options(read_file=str(read_file), max_time=8.0))


def perform(surface: Surface, host, sections: list) -> None:
    """Drive the surface, rendering between steps so each is audible."""
    engine = host.engine
    voice = engine[0]

    def render(seconds: float) -> np.ndarray:
        flat = host.render(np.zeros(int(seconds * SR), dtype=np.float32))
        return np.asarray(flat).reshape(-1, engine.out_channels)

    print("\n  1. load the buffer from disk (system page)")
    surface.touch("read_mono", label="read mono")
    time.sleep(0.3)  # the read runs on the server thread
    assert np.abs(np.asarray(host.buffers[1])).sum() > 0.0, "buffer never filled"
    print("       buffer 1 filled")

    print("\n  2. set a loop and play it (loop + mix pages)")
    surface.touch("loop_start1", 0.0, until=lambda: voice.loop_start == 0.0)
    surface.touch(
        "loop_end1", LOOP / 8.0, until=lambda: abs(voice.loop_end - LOOP) < 1e-3
    )
    surface.touch("loop1", 1.0, until=lambda: voice.loop)
    surface.touch("level1", 0.8, until=lambda: abs(voice.level - 0.8) < 1e-3)
    surface.touch("play1", 1.0, until=lambda: voice.play)
    # Setting loop points does not put the head inside them: softcut only starts
    # obeying a loop once a head has been *cut* to a position, so a surface that
    # sets the region and presses play free-runs past loop_end and off the end of
    # the material. Pressing `position` is the gesture that closes the loop.
    #
    # The half second before it is deliberate: it lets the head move, so cutting
    # it back to zero is something the demo can *watch* happen. Waiting on an
    # observable change is the only way to synchronise with a control surface --
    # OSC is fire-and-forget, and a render started before the datagram was
    # dispatched would quietly produce the wrong audio.
    sections.append(render(0.5))
    surface.touch(
        "position1", 0.0, until=lambda: voice.position < 0.1, label="position (cut in)"
    )
    sections.append(render(SECTION))
    assert voice.position < LOOP, (
        f"head free-ran to {voice.position:.2f}s past a {LOOP}s loop"
    )
    print(f"       looping: head at {voice.position:.2f}s of {LOOP:g}s")

    print("\n  3. half speed (mix page rate fader)")
    surface.touch("rate1", 0.625, until=lambda: abs(voice.rate - 0.5) < 1e-3)
    sections.append(render(SECTION))

    print("\n  4. close the post filter (post page)")
    surface.touch("post_dry1", 0.0, until=lambda: voice.post_filter_dry == 0.0)
    surface.touch("post_lp1", 1.0, until=lambda: voice.post_filter_lp == 1.0)
    surface.touch("post_fc1", 0.05, until=lambda: voice.post_filter_fc < 1000.0)
    sections.append(render(SECTION))

    print("\n  5. pan hard left, then back (mix page)")
    surface.touch("rate1", 0.75, until=lambda: abs(voice.rate - 1.0) < 1e-3)
    surface.touch("post_dry1", 1.0, until=lambda: voice.post_filter_dry == 1.0)
    surface.touch("pan1", 0.0, until=lambda: abs(voice.pan + 1.0) < 1e-3)
    sections.append(render(SECTION / 2))
    surface.touch("pan1", 1.0, until=lambda: abs(voice.pan - 1.0) < 1e-3)
    sections.append(render(SECTION / 2))

    print("\n  6. reset every voice (system page)")
    surface.touch("reset", label="reset voices")
    deadline = time.time() + 2.0
    while voice.play and time.time() < deadline:
        time.sleep(0.02)
    assert not voice.play, "reset never reached the host"
    print("       voices back to defaults; silence follows")
    sections.append(render(SECTION / 2))


def main(play: bool = False, serve: bool = False, backend: str = "auto") -> None:
    try:
        import py2tosc  # noqa: F401
    except ImportError:
        print(
            "this demo reads the TouchOSC layout; install it with: pip install py2tosc"
        )
        return

    from softcut.norns import NornsSoftcut
    from softcut.osc import SoftcutOSC

    source = DATA / "m04.wav"
    if not source.exists():
        print(f"missing demo audio: {source}")
        return

    if serve:
        serve_forever(backend)
        return

    document = build_layout(source)
    host = NornsSoftcut(sample_rate=SR, buffer_frames=2**20, mode="playback")
    server = SoftcutOSC(
        host,
        backend=backend,
        listen_host="127.0.0.1",
        listen_port=0,
        reply_host="127.0.0.1",
        reply_port=0,
    )
    server.start()
    _bound, port = server.server_address
    print(f"  server on 127.0.0.1:{port} via the {server.backend} transport")
    print(f"  surface: {len(document.find_all())} controls, driving voice 1\n")

    surface = Surface(document, port)
    sections: list[np.ndarray] = []
    try:
        perform(surface, host, sections)
    finally:
        surface.close()
        server.shutdown()

    out = np.concatenate(sections)
    path = write_wav(OUT / "13_osc_surface.wav", out, int(SR))
    print(f"\nwrote {path}  ({len(out) / SR:.1f}s)")

    if play:
        import softcut
        from _util import play as play_live
        from _util import to_buffer

        mono = out.mean(axis=1)
        engine = softcut.Engine(voices=1, sample_rate=int(SR), mode="playback")
        engine[0].buffer = to_buffer(mono)
        engine[0].configure(loop_region=(0, len(mono) / SR), rate=1.0)
        engine[0].play = True
        engine[0].cut_to(0.0)
        play_live(engine, len(mono) / SR)


def serve_forever(backend: str) -> None:
    """Run the server live so the real TouchOSC app can drive it.

    This is the part no script can stand in for: whether TouchOSC itself sends
    and receives what the layout says it should. The phase readout in particular
    is unverified until someone watches it move.
    """
    from softcut.norns import NornsSoftcut
    from softcut.osc import DEFAULT_LISTEN_PORT, SoftcutOSC

    host = NornsSoftcut(sample_rate=SR)
    server = SoftcutOSC(host, backend=backend, listen_port=DEFAULT_LISTEN_PORT)
    addresses = local_addresses()

    print("  open clients/touchosc/softcut.tosc in TouchOSC, then set")
    print("  connection 1 (the only slot the surface uses) to:\n")
    for address in addresses:
        print(f"      host {address}   send port {DEFAULT_LISTEN_PORT}")
    print("\n  for the phase readout on the sync page, note TouchOSC's own")
    print("  receive port and restart this with:")
    print(
        "      python -m softcut.osc --reply-host <tablet ip> --reply-port <that port>"
    )
    print("  then press 'poll on'. Six bars tracking separately means the")
    print("  per-voice filtering works; all six moving together means it does not.\n")
    print(f"  transport: {server.backend}.  Ctrl-C, or the surface's 'quit', to stop.")

    try:
        host.start()
    except RuntimeError as exc:
        print(f"  (no audio device: {exc} -- buffer ops still work)")

    server.start()
    try:
        server.wait_for_quit()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        host.stop()
    print("\n  stopped.")


def local_addresses() -> list[str]:
    """Best-effort list of addresses a tablet could reach this machine on."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))  # no packet is sent; picks the route's source
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return found or ["<this machine's LAN address>"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--play", action="store_true", help="also play the result live")
    ap.add_argument(
        "--serve",
        action="store_true",
        help="run the server live for the real TouchOSC app instead",
    )
    ap.add_argument(
        "--backend", default="auto", choices=["auto", "native", "python-osc"]
    )
    main(**vars(ap.parse_args()))
