#!/usr/bin/env python3
"""Headless smoke test for the standalone softcut-osc binary.

Launches the binary on miniaudio's null backend (no hardware) and drives it
entirely over UDP -- Python is only the OSC *client* here; the server is pure
C++ with no interpreter. Verifies that parameter messages are accepted, that the
phase poll emits `/poll/softcut/phase` replies with an advancing phase, and that
`/quit` shuts the process down cleanly.

Run after building:  make build-standalone && python clients/softcut-osc/test_smoke.py
Exits nonzero on failure.
"""

import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
BINARY = os.path.join(HERE, "..", "..", "build", "softcut-osc", "softcut-osc")


def osc(addr, types="", *args):
    def pad(b):
        return b + b"\x00" * (4 - len(b) % 4)

    m = pad(addr.encode()) + pad(("," + types).encode())
    for t, a in zip(types, args):
        if t == "i":
            m += struct.pack(">i", a)
        elif t == "f":
            m += struct.pack(">f", a)
        elif t == "s":
            m += pad(a.encode())  # pad() already adds the null terminator + 4-align
    return m


def write_wav_mono(path, samples, sr):
    """Write float samples [-1,1] as a mono 16-bit PCM WAV."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(round(s * 32767)))))
            for s in samples
        )
        w.writeframes(frames)


def read_wav_mono(path):
    """Read a mono 16-bit PCM WAV to float samples [-1,1]."""
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    return [struct.unpack("<h", raw[i : i + 2])[0] / 32768.0 for i in range(0, len(raw), 2)]


def disk_roundtrip(send, tmpdir, sr):
    """Load a known WAV via OSC into buffer 1, write it back, compare.

    Uses buffer 1 (the default-routed playing voice uses buffer 0), so the test
    signal is not disturbed. Returns True on success.
    """
    n = 2400  # 0.05 s at 48 kHz
    original = [0.5 * math.sin(2 * math.pi * 300.0 * i / sr) for i in range(n)]
    in_path = os.path.join(tmpdir, "in.wav")
    out_path = os.path.join(tmpdir, "out.wav")
    write_wav_mono(in_path, original, sr)

    dur = n / sr
    # read whole file -> buffer 1 (ch_dst=1) at dst 0; file rate == engine rate.
    send("/softcut/buffer/read_mono", "sfffii", in_path, 0.0, 0.0, -1.0, 0, 1)
    time.sleep(0.2)
    # write buffer 1's first `dur` seconds back out (ch=1).
    send("/softcut/buffer/write_mono", "sffi", out_path, 0.0, dur, 1)
    time.sleep(0.2)

    if not os.path.exists(out_path):
        print("FAIL: disk write produced no file")
        return False
    got = read_wav_mono(out_path)
    if len(got) < n:
        print(f"FAIL: round-trip length {len(got)} < {n}")
        return False
    max_err = max(abs(got[i] - original[i]) for i in range(n))
    print(f"disk round-trip: {n} frames, max abs error {max_err:.2e}")
    if max_err > 2e-3:  # 16-bit quantization + 32767/32768 scaling
        print("FAIL: round-trip signal differs beyond 16-bit tolerance")
        return False
    return True


def spawn(reply_port, extra=()):
    """Launch the server on an ephemeral port; return (proc, port)."""
    proc = subprocess.Popen(
        [BINARY, "--null", "--listen-host", "127.0.0.1", "--listen-port", "0",
         "--reply-host", "127.0.0.1", "--reply-port", str(reply_port),
         "--voices", "4", "--block-size", "128", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        print("server:", line.rstrip())
        if "listening on" in line:
            return proc, int(line.rsplit(":", 1)[1])
    proc.kill()
    return proc, None


def crossfade_check():
    """With the default crossfade, a constant DC block loaded over a zeroed
    buffer must be flat in the interior but ramped (faded) at the edges."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    reply_port = rx.getsockname()[1]
    rx.close()
    proc, port = spawn(reply_port, extra=["--crossfade-ms", "2"])
    if port is None:
        print("FAIL: crossfade server did not start")
        return False
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = ("127.0.0.1", port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            n = 2400  # 0.05 s; crossfade of 2 ms = 96 frames at each edge
            in_path = os.path.join(tmpdir, "dc.wav")
            out_path = os.path.join(tmpdir, "dc_out.wav")
            write_wav_mono(in_path, [0.5] * n, 48000)
            tx.sendto(osc("/softcut/buffer/read_mono", "sfffii",
                          in_path, 0.0, 0.0, -1.0, 0, 1), dst)
            time.sleep(0.2)
            tx.sendto(osc("/softcut/buffer/write_mono", "sffi",
                          out_path, 0.0, n / 48000, 1), dst)
            time.sleep(0.2)
            got = read_wav_mono(out_path)
            tx.sendto(osc("/quit"), dst)
            proc.wait(timeout=3)
            interior = got[300] if len(got) > 300 else 0.0
            print(f"crossfade: got[0]={got[0]:.3f} got[48]={got[48]:.3f} "
                  f"interior={interior:.3f}")
            if abs(interior - 0.5) > 2e-3:
                print("FAIL: interior not preserved through crossfaded load")
                return False
            if abs(got[0]) > 0.05:  # first frame should be faded ~to zero
                print("FAIL: edge not faded (crossfade not applied)")
                return False
            if not (abs(got[0]) < abs(got[48]) < abs(interior)):
                print("FAIL: edge is not a monotone ramp into the interior")
                return False
            print("PASS (crossfade)")
            return True
    finally:
        if proc.poll() is None:
            proc.kill()
        tx.close()


def preserve_mix_check(send, tmpdir, sr):
    """Load DC 0.5, then overlay DC 1.0 with preserve=0.5, mix=0.5. The interior
    must blend to 0.5*0.5 + 1.0*0.5 = 0.75 (run on a crossfade-0 server)."""
    n = 2400
    a_path = os.path.join(tmpdir, "a.wav")
    b_path = os.path.join(tmpdir, "b.wav")
    out_path = os.path.join(tmpdir, "pm_out.wav")
    write_wav_mono(a_path, [0.5] * n, sr)
    write_wav_mono(b_path, [1.0] * n, sr)
    dur = n / sr
    send("/softcut/buffer/read_mono", "sfffii", a_path, 0.0, 0.0, -1.0, 0, 1)
    time.sleep(0.15)
    # 8 args: ...ch_src, ch_dst, preserve, mix
    send("/softcut/buffer/read_mono", "sfffiiff", b_path, 0.0, 0.0, -1.0, 0, 1, 0.5, 0.5)
    time.sleep(0.15)
    send("/softcut/buffer/write_mono", "sffi", out_path, 0.0, dur, 1)
    time.sleep(0.2)
    got = read_wav_mono(out_path)
    if len(got) < n:
        print(f"FAIL: preserve/mix round-trip length {len(got)} < {n}")
        return False
    interior = got[n // 2]
    print(f"preserve/mix: interior={interior:.3f} (expect 0.75)")
    if abs(interior - 0.75) > 3e-3:
        print("FAIL: preserve/mix blend incorrect")
        return False
    return True


def resample_check():
    """With --resample-on-read, a 24 kHz file loaded into a 48 kHz engine must
    occupy ~2x its source frame count (frame-for-frame would keep it at 1x)."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    reply_port = rx.getsockname()[1]
    rx.close()
    # crossfade 0 so the loaded DC region has clean square edges to count.
    proc, port = spawn(reply_port, extra=["--resample-on-read", "--crossfade-ms", "0"])
    if port is None:
        print("FAIL: resample server did not start")
        return False
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = ("127.0.0.1", port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            n_src = 1200  # 0.05 s at 24 kHz
            in_path = os.path.join(tmpdir, "half.wav")
            out_path = os.path.join(tmpdir, "half_out.wav")
            write_wav_mono(in_path, [0.5] * n_src, 24000)  # half the engine rate
            tx.sendto(osc("/softcut/buffer/read_mono", "sfffii",
                          in_path, 0.0, 0.0, -1.0, 0, 1), dst)
            time.sleep(0.2)
            tx.sendto(osc("/softcut/buffer/write_mono", "sffi",
                          out_path, 0.0, 0.1, 1), dst)  # write 0.1 s (4800 frames)
            time.sleep(0.2)
            got = read_wav_mono(out_path)
            tx.sendto(osc("/quit"), dst)
            proc.wait(timeout=3)
            loaded = sum(1 for v in got if v > 0.25)
            print(f"resample: loaded frames={loaded} (expect ~2400, not ~1200)")
            if not (2300 <= loaded <= 2500):
                print("FAIL: resampled length not ~2x source (resampler not applied)")
                return False
            print("PASS (resample)")
            return True
    finally:
        if proc.poll() is None:
            proc.kill()
        tx.close()


def parse_phase(d):
    if not d.startswith(b"/poll/softcut/phase"):
        return None
    # address(24 padded) + ",if\0" (4) + int(4) + float(4)
    body = d[d.index(b",") :]
    args = body[4:]  # skip ",if\0"
    voice = struct.unpack(">i", args[0:4])[0]
    phase = struct.unpack(">f", args[4:8])[0]
    return voice, phase


def main():
    if not os.path.exists(BINARY):
        print(f"FAIL: binary not built at {BINARY} (run: make build-standalone)")
        return 1

    # A UDP socket to receive phase replies.
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    reply_port = rx.getsockname()[1]

    # crossfade 0 here so the disk round-trip is an exact (edge-inclusive) check;
    # the crossfade itself is verified separately by crossfade_check().
    proc, port = spawn(reply_port, extra=["--crossfade-ms", "0"])
    if port is None:
        print("FAIL: server did not report a listening port")
        return 1

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = ("127.0.0.1", port)

    def send(addr, types="", *args):
        tx.sendto(osc(addr, types, *args), dst)

    try:
        # Set up voice 0 to loop and play, so its quantized phase advances.
        send("/set/param/cut/loop_start", "if", 0, 0.0)
        send("/set/param/cut/loop_end", "if", 0, 2.0)
        send("/set/param/cut/loop_flag", "if", 0, 1)
        send("/set/param/cut/rate", "if", 0, 1.0)
        send("/set/param/cut/phase_quant", "if", 0, 0.05)
        send("/set/param/cut/play_flag", "if", 0, 1)
        send("/set/param/cut/position", "if", 0, 0.0)
        time.sleep(0.1)
        send("/poll/start/cut/phase", "")

        phases = []
        end = time.time() + 2.0
        while time.time() < end and len(phases) < 6:
            try:
                d, _ = rx.recvfrom(128)
            except socket.timeout:
                break
            r = parse_phase(d)
            if r is not None and r[0] == 0:
                phases.append(r[1])

        print("phases for voice 0:", phases)
        if len(phases) < 2:
            print("FAIL: expected multiple advancing phase replies")
            return 1
        if len(set(phases)) < 2:
            print("FAIL: phase did not advance (poll not emitting on change)")
            return 1

        # Disk I/O round-trip (dr_wav): file -> buffer -> file, then a
        # preserve/mix blend check (both on this crossfade-0 server).
        with tempfile.TemporaryDirectory() as tmpdir:
            if not disk_roundtrip(send, tmpdir, 48000):
                return 1
            if not preserve_mix_check(send, tmpdir, 48000):
                return 1

        # Clean shutdown via /quit.
        send("/quit", "")
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("FAIL: server did not exit on /quit")
            return 1
        print(f"server exited cleanly (code {proc.returncode}) on /quit")
    finally:
        if proc.poll() is None:
            proc.kill()
        rx.close()
        tx.close()

    # Separate server (default crossfade) to verify the edge fade.
    if not crossfade_check():
        return 1
    # Separate server (--resample-on-read) to verify opt-in resampling.
    if not resample_check():
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
