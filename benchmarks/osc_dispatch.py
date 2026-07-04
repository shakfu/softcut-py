"""Benchmark: is a native GIL-free OSC fast path worth it?

Compares, per message:
  1. python-osc dispatch  - the Python handler path both current backends use
                            (dict lookup + handler + setattr + queue push).
  2. native dispatch      - the proposed C fast path: tinyosc parse + address
                            match + post to the command queue, GIL released.
  3. UDP loopback floor   - the transport cost (sendto+recvfrom) every message
                            pays regardless of how it is dispatched.

The takeaway drives the design: a message is transport-bound (a socket syscall),
not dispatch-bound, so dispatching in C rather than Python saves a rounding
error per message. The current native backend therefore dispatches under the
GIL (keeping the command queue single-producer), and no GIL-free fast path is
built.

Requires a build with the native OSC transport:
  make build-tinyosc      # or SKBUILD_CMAKE_DEFINE=SOFTCUT_ENABLE_TINYOSC=ON

Run: uv run python benchmarks/osc_dispatch.py
"""

import socket
import time

from softcut import _core
from softcut import norns
from softcut.osc import SoftcutOSC

N_DISPATCH = 500_000
N_UDP = 100_000
TRIALS = 5


def best(fn, trials=TRIALS):
    """Best (min) elapsed seconds over several trials, to cut scheduler noise."""
    return min(fn() for _ in range(trials))


def bench_python_dispatch(n):
    host = norns.NornsSoftcut(buffer_frames=2**16, mode="playback")
    server = SoftcutOSC(
        host, backend="python-osc", listen_host="127.0.0.1", listen_port=0
    )
    dispatch = server._dispatch
    args = [0, 1.0]
    # warmup
    for _ in range(1000):
        dispatch("/set/param/cut/rate", args)

    def run():
        t0 = time.perf_counter()
        for _ in range(n):
            dispatch("/set/param/cut/rate", args)
        return time.perf_counter() - t0

    dt = best(run)
    server.shutdown()
    return dt


def bench_native_dispatch(n):
    host = norns.NornsSoftcut(buffer_frames=2**16, mode="playback")
    v = host.engine[0]
    _core._bench_native_dispatch(v, 1000)  # warmup
    return best(lambda: _core._bench_native_dispatch(v, n))


def bench_udp_floor(n):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    addr = rx.getsockname()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"/set/param/cut/rate\x00,if\x00\x00\x00\x00\x00?\x80\x00\x00"  # ~28 bytes

    def run():
        t0 = time.perf_counter()
        for _ in range(n):
            tx.sendto(msg, addr)
            rx.recvfrom(64)
        return time.perf_counter() - t0

    dt = best(run, trials=3)
    rx.close()
    tx.close()
    return dt


def per_msg_us(seconds, n):
    return seconds / n * 1e6


def rate_m(seconds, n):
    return n / seconds / 1e6


def main():
    if not _core.HAVE_TINYOSC:
        raise SystemExit("build with SOFTCUT_ENABLE_TINYOSC=ON (make build-tinyosc)")

    py = bench_python_dispatch(N_DISPATCH)
    nat = bench_native_dispatch(N_DISPATCH)
    udp = bench_udp_floor(N_UDP)

    print(f"\n{'path':<34}{'us/msg':>10}{'M msg/s':>12}")
    print("-" * 56)
    print(f"{'1. python-osc dispatch':<34}"
          f"{per_msg_us(py, N_DISPATCH):>10.3f}{rate_m(py, N_DISPATCH):>12.2f}")
    print(f"{'2. native dispatch (C, GIL-free)':<34}"
          f"{per_msg_us(nat, N_DISPATCH):>10.3f}{rate_m(nat, N_DISPATCH):>12.2f}")
    print(f"{'3. UDP loopback round-trip':<34}"
          f"{per_msg_us(udp, N_UDP):>10.3f}{rate_m(udp, N_UDP):>12.2f}")
    print("-" * 56)

    speedup = py / nat
    udp_us = per_msg_us(udp, N_UDP)
    save_us = per_msg_us(py, N_DISPATCH) - per_msg_us(nat, N_DISPATCH)
    print(f"\nnative dispatch speedup vs python-osc dispatch: {speedup:.1f}x")
    print(f"dispatch time saved per message: ~{save_us:.2f} us")
    print(f"transport floor (server recvfrom ~= half round-trip): "
          f"~{udp_us / 2:.2f} us/msg")
    print(f"dispatch saving as a share of one recvfrom: "
          f"~{save_us / (udp_us / 2) * 100:.0f}%\n")


if __name__ == "__main__":
    main()
