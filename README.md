# softcut

![CI](https://github.com/shakfu/softcut-py/actions/workflows/ci.yml/badge.svg)

Python bindings for [softcut-lib](https://github.com/monome/softcut-lib) — the per-voice DSP engine behind monome norns' softcut — with realtime audio I/O via [miniaudio](https://github.com/mackron/miniaudio). Built with [nanobind](https://github.com/wjakob/nanobind), and with **no dependencies**: buffers are plain `array.array("f")`, and audio, WAV I/O and the OSC server all work on a bare install.

The primary API exposes softcut as idiomatic Python objects. An optional [norns-compatible layer](#norns-compatible-api) (`softcut.norns`) additionally mirrors the flat norns Lua `softcut` API for porting existing scripts.

## Concepts

- **`Voice`** wraps one `softcut::Voice`: a crossfading read/write head over an audio buffer, with rate, loop points, record/play, fades, and pre/post state-variable filters. Parameters are plain attributes; the buffer is a `float32` buffer **you** own (softcut-lib never allocates buffer memory) — anything C-contiguous, so an `array.array("f")`, a `memoryview`, or a numpy array. Buffer length must be a power of two — use `softcut.next_power_of_two` or `Engine.allocate`, which rounds up for you. The same array can be shared by several voices.

- **`Engine`** is the multi-voice host: it owns a set of voices and a miniaudio device, and runs them either live (realtime mic/speaker I/O on a background audio thread) or offline via `Engine.render`. It is a context manager and a sequence of voices.

## Live looping

```python
import softcut, time

with softcut.Engine(voices=2) as eng:          # opens the audio device
    eng.allocate(seconds=8)                    # shared power-of-two buffer
    eng[0].configure(loop_region=(0, 4), rate=1.0, level=0.8, pan=-0.3)

    with eng[0].record(at=0):                  # rec + play on; head cut to 0s
        time.sleep(4)                          # capture 4s of mic input
    # on exit: rec off — the voice keeps looping what it captured

    eng[1].configure(loop_region=(0, 4), rate=-0.5, level=0.6, pan=0.3)
    eng[1].record_for(4, at=0)                 # blocking variant: record 4s, then stop

    time.sleep(8)                              # listen to both loops
# device closed automatically
```

`eng.start()` returns immediately and audio runs on a background thread, so the REPL stays live — set a parameter and you hear the change on the next block. `record()` is the non-blocking context-manager gesture; `record_for(seconds)` blocks the calling thread for a fixed capture.

## Offline rendering

No device; process a mono block through the voices and get the mixed stereo output back. This is the deterministic path used by the tests:

```python
import numpy as np, softcut

eng = softcut.Engine(voices=1, mode="playback")
v = eng[0]
v.buffer = np.zeros(2**16, dtype=np.float32)
v.configure(loop_region=(0, 1), rate=1.0)
v.rec = v.play = True
v.cut_to(0)

out = eng.render(np.random.randn(48000).astype(np.float32))   # flat, interleaved
frames = np.asarray(out).reshape(-1, eng.out_channels)        # (48000, 2), no copy
```

`render` returns interleaved frames in an `array.array("f")`; wrapping it in numpy costs nothing. Pass your own buffer as `out` — of either kind — to fill it in place and skip the allocation:

```python
mono = np.random.randn(48000).astype(np.float32)
buf = np.empty(48000 * eng.out_channels, dtype=np.float32)
eng.render(mono, buf)          # fills and returns buf
```

Load/save audio with whatever you like (e.g. `soundfile`) and assign the array to `voice.buffer`.

## Dependencies

softcut-py has **no dependencies**, numpy included. Buffers are `array.array("f")` and every entry point takes any C-contiguous float32 buffer, so numpy arrays work wherever you care to use them — as a voice's buffer, as render input, as an `out` buffer — and `numpy.asarray` wraps what softcut returns without copying. The extension allocates no results and imports nothing.

The sample-level buffer arithmetic and the WAV sample-format conversion run in C++ (shared with the standalone server), and `wave` from the standard library parses the container.

```console
$ pip install softcut-py         # no dependencies
$ pip install softcut-py[osc]    # + the pure-Python OSC transport
```

One consequence worth knowing: a float64 array is now **refused** rather than silently converted. Cast it with `.astype("float32")`.

## Routing and devices

Voices mix to stereo via each voice's `level` and `pan`. `Engine.feedback(src, dst, amount)` routes one voice's output into another's input (one block delayed; `src == dst` is a self-feedback delay line), and each voice's `input_gain` scales the engine's external (mic) input into it:

```python
eng.feedback(0, 1, 0.4)     # voice 0 -> voice 1 input
eng[1].input_gain = 0.0     # voice 1 ignores the mic
```

Pick a specific device by index from `softcut.list_devices()`:

```python
softcut.list_devices()                      # [{'index':0,'name':...,'type':'playback',...}, ...]
eng = softcut.Engine(output_device=1, input_device=0)
```

## norns-compatible API

For porting norns scripts (and the muscle memory that goes with them), `softcut.norns` mirrors the flat, 1-based, singleton norns [softcut Lua API](https://monome.org/docs/norns/api/modules/softcut.html): 6 voices indexed from 1 and 2 global mono buffers numbered 1/2. Import it under the name norns scripts expect and call the functions verbatim:

```python
from softcut import norns as softcut

softcut.buffer_clear()
softcut.buffer_read_mono("loop.wav", ch_dst=1)   # stdlib wave, no extra dep
softcut.loop(1, 1)
softcut.loop_start(1, 0.0)
softcut.loop_end(1, 4.0)
softcut.rate(1, 1.0)
softcut.level(1, 0.8)
softcut.play(1, 1)

softcut.start()                                  # open the audio device
```

- **Attribute passthrough** — `rate`, `level`, `pan`, `play`/`rec`/`loop`, loop points, `position`, the pre/post filters, slews, phase, `buffer`, `voice_sync`, `level_cut_cut`, `reset`.

- **Buffer/disk ops** — `buffer_read_*` / `buffer_write_*`, `buffer_copy_*`, `buffer_clear*`, on the shared C++ buffer primitives plus the standard-library `wave` module (WAV only, no new dependency), with preserve/mix crossfade, edge `fade_time` and `reverse`. Operations write in place, so they are safe against the running audio thread; reads are non-resampling, matching norns.

`softcut.render` / `softcut.start` / `softcut.stop` drive audio (norns runs its audio continuously; here you render offline or open the device explicitly). Phase polling and per-sample level/pan slews are not yet implemented; see [`docs/dev/norns-api.md`](docs/dev/norns-api.md) for the full mapping and status. `demos/12_norns_api.py` is a narrated walkthrough built entirely on this layer.

## OSC server

`softcut.osc` exposes softcut over the same OSC wire protocol as the reference [`softcut_jack_osc`](https://github.com/monome/softcut-lib) client, so existing norns/Lua scripts, SuperCollider, Max, or any OSC controller can drive softcut-py as a drop-in engine over the network. It is a thin dispatch layer over the norns host: each address maps to a host method, with the one translation that the wire protocol is 0-based (voices 0-5, buffers 0-1) while the host is 1-based.

```python
from softcut.osc import SoftcutOSC
from softcut import norns

host = norns.NornsSoftcut()
host.start()                                   # open the audio device
server = SoftcutOSC(host)                       # listen on UDP 9999
server.serve_forever()                          # blocks until a /quit message
```

Or run it straight from the command line:

```bash
python -m softcut.osc                            # device + OSC server
python -m softcut.osc --no-audio                 # offline: buffer ops only
```

Then drive it from any OSC client (voice/buffer indices 0-based):

```
/set/param/cut/rate      0 1.0        # voice 0 rate = 1.0
/set/param/cut/loop_start 0 0.0
/set/param/cut/loop_end  0 4.0
/set/param/cut/loop_flag 0 1
/set/level/cut           0 0.8
/set/param/cut/play_flag 0 1
/softcut/buffer/read_mono "loop.wav"  0.0 0.0 -1  0 0
/poll/start/cut/phase                 # -> /poll/softcut/phase <voice> <phase>
```

The full namespace is mirrored: all `/set/param/cut/*` params, routing (`/set/level|pan/cut`, `cut_cut`, `in_cut`), the `/softcut/buffer/*` disk ops, `/softcut/reset`, and the phase poll. Defaults match the reference: listen on UDP 9999, reply (phase poll) to `127.0.0.1:57120`.

**Two transports**, selected by `backend=` ("auto" by default):

- **native** — a dependency-free UDP transport on the vendored [tinyosc](https://github.com/mhroth/tinyosc) codec, **compiled in by default**, so `pip install softcut-py` serves OSC with nothing else installed. Per-voice `/set/param/cut/*` messages are parsed and dispatched entirely in C without the GIL. IPv4-only. Disable with a source build (`SKBUILD_CMAKE_DEFINE="SOFTCUT_ENABLE_TINYOSC=OFF" pip install .`).

- **python-osc** — the pure-Python transport, longer-established and the fallback when the native one is not built. `pip install softcut-py[osc]`.

`"auto"` takes native when built and python-osc otherwise; either can be named explicitly.

Building the transport in grants the *ability* to serve OSC, never a running server: importing `softcut.osc` opens no socket and starts no thread. A server exists when you construct `SoftcutOSC` and listens when you start it, or when you run `python -m softcut.osc`.

### Standalone server (no Python)

For a headless, interpreter-free deployment, [`clients/softcut-osc`](clients/softcut-osc/) builds a standalone native binary (softcut-lib + tinyosc + miniaudio) that speaks the same softcut OSC protocol with **no CPython at all** — so nothing on its control path can touch a GIL. It is the pure-C++ counterpart to `softcut.osc`: identical DSP and wire protocol, with no interpreter to schedule at all — where `softcut.osc` merely keeps its control path off the GIL, this has no GIL to keep off. It covers the full namespace plus WAV disk I/O (via the vendored [dr_wav](https://github.com/mackron/dr_libs), on a disk-worker thread with click-avoidance crossfades), `preserve`/`mix` blending, opt-in `--resample-on-read`, and device selection.

```bash
make build-standalone                    # -> build/softcut-osc/softcut-osc
./build/softcut-osc/softcut-osc --help
./build/softcut-osc/softcut-osc          # listen UDP 9999, reply 127.0.0.1:57120
```

The Python extension and this binary share their Python-free C++ core (command queue, mixer, device, sockets) under `src/shared`. See [`clients/softcut-osc/README.md`](clients/softcut-osc/README.md).

### TouchOSC surface

[`clients/touchosc`](clients/touchosc/) holds a TouchOSC layout, `softcut.tosc`, that plays either server over the wire protocol: a mixer strip per voice, tabular pages for the loop, record and filter parameters, the feedback and voice-sync matrices, buffer and disk operations, and a receive-only phase readout fed by the phase poll. It is generated from Python with [py2tosc](https://pypi.org/project/py2tosc/) rather than drawn by hand, so `make touchosc` rebuilds it for a different canvas, voice count or parameter range, and the test suite pushes every binding in it through the server's own dispatch table.

## Build and test

```bash
make sync     # set up the environment
make test     # run the test suite
make qa       # test + lint + typecheck + format
```

Set `SOFTCUT_TEST_AUDIO=1` to additionally exercise a real audio device in the test suite. Use `make help` for more targets (wheel, sdist, clean, etc.).

## Releasing

CI runs QA and a Linux/macOS/Windows build smoke on every push and pull request. Pushing a `v*` tag builds wheels for CPython 3.10-3.14 across Linux (x86_64/aarch64), macOS (x86_64/arm64) and Windows with [cibuildwheel](https://cibuildwheel.pypa.io), plus the sdist, and publishes them to PyPI via trusted publishing. To cut one, set the version in **both** `pyproject.toml` and `src/softcut/__init__.py` — they are separate copies, and `test_version_matches_the_packaging_metadata` fails if they disagree — then commit, tag `vX.Y.Z`, and push the tag. (TestPyPI is available via the workflow's manual `workflow_dispatch`.)

## Notes

- Realtime parameter updates are safe: while the device is running, voice DSP parameter changes from Python are enqueued and applied on the audio thread via a lock-free queue rather than racing it. (The mix scalars `level`/`pan`/ `input_gain` and the feedback matrix are plain aligned writes.)

- The vendored `softcut-lib` carries small host-portability fixes (uninitialized members that relied on embedded zero-init static storage — including the phase quantum and the two phase mirrors the poll reports from — and an oversized debug buffer stubbed out); see the comments in `thirdparty/softcut-lib`.
