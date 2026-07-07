"""Benchmark: control-latency jitter of the OSC backends under a busy interpreter.

This is the measurement the design note (docs/dev/tinypsc-opt.md) said was
missing: the native (tinyosc) backend's claimed win is not throughput or mean
latency -- both are transport-bound / block-quantized and unchanged -- but
*robustness under GIL contention*. python-osc dispatches each message on a
Python thread that must hold the GIL to run its handler, so when another Python
thread is busy the dispatch is delayed. The native fast path parses and applies
`/set/param/cut/*` in C without the GIL, so a busy interpreter cannot delay it.

It reports two latencies per message, so the observer confound is measured, not
just caveated:
  - dispatch-only: send -> the apply instant recorded *in C* by an apply-latency
    probe (`_core._bench_last_apply_ns`), stamped on whichever thread applied the
    value. This excludes the Python observer and is the true control-to-audio
    proxy.
  - end-to-end: send -> when the Python observer *sees* the value by polling the
    probe (`_core._bench_probe_applied`). The observer's poll needs the GIL, so
    this carries its own GIL wait.
Both timestamps come from one steady_clock (`_core._steady_clock_ns` for the send
side), so end-to-end minus dispatch-only is the observer's contribution.

Method (per backend, with and without a background GIL hog):
  - Start the OSC server on an ephemeral port (offline: no audio device, so a
    param is applied the moment it is dispatched -- this isolates the dispatch
    component; a running engine would add ~one block period equally to both
    backends).
  - Repeatedly: send `/set/param/cut/rate 0 <unique>` (the value is exactly
    representable in float32 so the probe's value match is exact), poll the probe
    until it reports the value applied, then read both the observe time and the C
    apply time. The probe publishes the value with release and the poll acquires
    it, so the timestamp read always belongs to this apply (no stale/negative).
  - Under contention a background thread runs pure-Python work, holding the GIL
    except at the interpreter's switch interval.
  - Report each distribution (min / median / p95 / p99 / max) -- "jitter" is the
    tail, not the average.

What to read: under 'gil-hog', dispatch-only native stays ~flat while python-osc
inflates -- that gap is the GIL-free win, with the observer removed. The
end-to-end table shows what a Python app polling state would feel (both backends
pay the observer wait there).

Requires a build with the apply-latency probe (SOFTCUT_ENABLE_BENCH_PROBE) and,
for the `native` rows, the tinyosc transport (SOFTCUT_ENABLE_TINYOSC). The probe
is compile-time gated and absent from normal builds:
  make build-bench
  # or SKBUILD_CMAKE_DEFINE='SOFTCUT_ENABLE_TINYOSC=ON;SOFTCUT_ENABLE_BENCH_PROBE=ON'

Run: uv run python benchmarks/osc_jitter.py [--samples N] [--switch-interval S]

The default --switch-interval (5e-4 s) forces frequent GIL handoffs so the
distribution is stable and legible. At CPython's stock 5e-3 s the effect is far
more dramatic but less measurable: python-osc can drop control messages outright
under the hog, while native keeps applying -- pass --switch-interval 5e-3 to see
that failure mode.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time

from softcut import _core
from softcut import osc as osc_mod
from softcut.norns import NornsSoftcut
from softcut.osc import SoftcutOSC

# rate values are exactly float32-representable (1 + k/1024), so the probe's
# value match (float bits, parsed from the wire) is exact and never misses.
STEP = 1.0 / 1024.0
RESEND = 0.005      # resend an unacknowledged message after this long (UDP loss)
PER_TIMEOUT = 0.5   # give up on a single sample after this long (counts as drop)


def rate_message(voice: int, value: float) -> bytes:
    """Encode `/set/param/cut/rate <voice:int> <value:float>` as an OSC packet."""
    def pad(b: bytes) -> bytes:
        return b + b"\x00" * (4 - len(b) % 4)

    return (
        pad(b"/set/param/cut/rate")
        + pad(b",if")
        + struct.pack(">i", voice)
        + struct.pack(">f", value)
    )


class GilHog:
    """A background thread running pure-Python work to contend for the GIL."""

    def __init__(self) -> None:
        self._stop = False
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "GilHog":
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        x = 0
        while not self._stop:
            x = (x + 1) & 0xFFFFFFFF  # pure-Python bytecode; holds the GIL


def measure(server: SoftcutOSC, samples: int) -> tuple[list[float], list[float], int]:
    """Return (end_to_end_ms, dispatch_only_ms, drops) for `samples` sends.

    Two latencies per message, both from one steady_clock so they subtract:
      - end_to_end: send -> the Python observer *sees* the value (its own poll
        must acquire the GIL, so this carries the observer's GIL wait);
      - dispatch_only: send -> the apply instant recorded in C by the probe
        (`_bench_last_apply_ns`), which excludes the observer entirely.
    Their difference is the observer confound, made visible rather than assumed.
    """
    _host, port = server.server_address
    dst = ("127.0.0.1", port)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    applied = _core._bench_probe_applied  # value-correlated, acquire-loaded

    def send_once(value: float) -> tuple[float, float] | None:
        msg = rate_message(0, value)
        t_send = _core._steady_clock_ns()
        tx.sendto(msg, dst)
        t0 = time.perf_counter()
        next_resend = t0 + RESEND
        deadline = t0 + PER_TIMEOUT
        while not applied(value):
            now = time.perf_counter()
            if now >= deadline:
                return None
            if now >= next_resend:
                tx.sendto(msg, dst)
                next_resend = now + RESEND
            time.sleep(0)  # yield the GIL so the receiver (and hog) can run
        t_obs = _core._steady_clock_ns()
        t_apply = _core._bench_last_apply_ns()  # C apply instant, same clock
        return (t_obs - t_send) / 1e6, (t_apply - t_send) / 1e6

    for i in range(64):  # warm the receiver thread and socket path
        send_once(1.0 + (i + 1) * STEP)

    e2e: list[float] = []
    iso: list[float] = []
    drops = 0
    for i in range(samples):
        value = 2.0 + (i + 1) * STEP
        r = send_once(value)
        if r is None:
            drops += 1
        else:
            e2e.append(r[0])
            iso.append(r[1])
    tx.close()
    return e2e, iso, drops


def pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 100]) of an already-sorted list."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(q / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def summarize(lat: list[float]) -> dict:
    lat = sorted(lat)
    return {
        "min": lat[0] if lat else float("nan"),
        "median": pct(lat, 50),
        "p95": pct(lat, 95),
        "p99": pct(lat, 99),
        "max": lat[-1] if lat else float("nan"),
    }


def run_case(backend: str, hog: bool, samples: int) -> dict:
    host = NornsSoftcut(buffer_frames=2**16, mode="playback")
    server = SoftcutOSC(
        host, backend=backend, listen_host="127.0.0.1", listen_port=0
    )
    server.start()
    try:
        if hog:
            with GilHog():
                e2e, iso, drops = measure(server, samples)
        else:
            e2e, iso, drops = measure(server, samples)
    finally:
        server.shutdown()
    return {
        "backend": backend,
        "load": "gil-hog" if hog else "idle",
        "e2e": summarize(e2e),
        "iso": summarize(iso),
        "drops": drops,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument(
        "--switch-interval",
        type=float,
        default=5e-4,
        help="sys.setswitchinterval for the run (seconds); smaller = more "
        "frequent GIL handoffs. Default 5e-4 keeps the measurement stable. "
        "Pass 5e-3 (CPython's stock interval) to see python-osc degrade far "
        "harder -- coarse handoffs let it drop control messages outright.",
    )
    args = parser.parse_args()

    if not getattr(_core, "HAVE_BENCH_PROBE", False):
        raise SystemExit(
            "this benchmark needs the compile-time apply-latency probe.\n"
            "Rebuild with it (and tinyosc, for the native rows):\n"
            "  make build-bench\n"
            "  # or SKBUILD_CMAKE_DEFINE="
            "'SOFTCUT_ENABLE_TINYOSC=ON;SOFTCUT_ENABLE_BENCH_PROBE=ON' "
            "pip install ."
        )

    backends = []
    if osc_mod.HAVE_PYTHONOSC:
        backends.append("python-osc")
    if osc_mod.NATIVE_OSC_AVAILABLE:
        backends.append("native")
    if not backends:
        raise SystemExit(
            "no OSC transport available: build with SOFTCUT_ENABLE_TINYOSC "
            "or install softcut-py[osc]."
        )
    if "native" not in backends:
        print("note: native transport not built; only python-osc will run "
              "(make build-tinyosc to compare).")

    saved_interval = sys.getswitchinterval()
    sys.setswitchinterval(args.switch_interval)

    rows = []
    try:
        for backend in backends:
            for hog in (False, True):
                rows.append(run_case(backend, hog, args.samples))
    finally:
        sys.setswitchinterval(saved_interval)

    header = (f"{'backend':<12}{'load':<9}{'min':>8}{'median':>8}"
              f"{'p95':>8}{'p99':>8}{'max':>9}{'drops':>7}")

    def print_table(title: str, key: str) -> None:
        print(f"\n{title}  (samples={args.samples}, "
              f"switch_interval={args.switch_interval * 1e3:.2f} ms)")
        print(header)
        print("-" * len(header))
        for r in rows:
            s = r[key]
            print(f"{r['backend']:<12}{r['load']:<9}"
                  f"{s['min']:>8.3f}{s['median']:>8.3f}{s['p95']:>8.3f}"
                  f"{s['p99']:>8.3f}{s['max']:>9.3f}{r['drops']:>7}")
        print("-" * len(header))

    print_table("dispatch-only latency, ms  (C apply timestamp; observer excluded)",
                "iso")
    print_table("end-to-end latency, ms  (Python observer; includes its GIL wait)",
                "e2e")
    print("\nDispatch-only is the true control-to-audio proxy: under 'gil-hog' "
          "native stays ~flat\nwhile python-osc's tail inflates -- that gap is "
          "the GIL-free win, now with the observer\nconfound removed. "
          "(end-to-end minus dispatch-only ~= the observer's own GIL wait.)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
