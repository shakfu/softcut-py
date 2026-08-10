# softcut

![CI](https://github.com/shakfu/softcut-py/actions/workflows/ci.yml/badge.svg)

Python bindings for [softcut-lib](https://github.com/monome/softcut-lib) — the per-voice DSP engine behind monome norns' softcut — with realtime audio I/O via [miniaudio](https://github.com/mackron/miniaudio). Built with [nanobind](https://github.com/wjakob/nanobind).

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

out = eng.render(np.random.randn(48000).astype(np.float32))   # (48000, 2) float32
```

Load/save audio with whatever you like (e.g. `soundfile`) and assign the array to `voice.buffer`.

## Dependencies

The package has **no required runtime dependencies**. Buffers are `array.array("f")` and every entry point takes any C-contiguous float32 buffer, so numpy arrays work everywhere they did before — they are simply no longer necessary. The sample-level buffer arithmetic and the WAV sample-format conversion run in C++ (shared with the standalone server), and `wave` from the standard library parses the container.

numpy is needed for exactly one thing: `Engine.render` and `Voice.process` allocate their result as an `(n, out_channels)` ndarray. Pass `out=` — a flat buffer of `n * out_channels` float32 samples, written in place and returned — and nothing imports numpy at all.

```console
$ pip install softcut-py            # no dependencies
$ pip install softcut-py[numpy]     # + the ndarray return from render/process
$ pip install softcut-py[osc]       # + the pure-Python OSC transport
```

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

- **python-osc** — the default pure-Python transport. Install the optional extra: `pip install softcut-py[osc]`.

- **native** (**experimental**) — a dependency-free UDP transport built on the vendored [tinyosc](https://github.com/mhroth/tinyosc) codec. It is **not** compiled into the published wheels; opt in with a source build (`SKBUILD_CMAKE_DEFINE="SOFTCUT_ENABLE_TINYOSC=ON" pip install .` or `make build-tinyosc`; reported by `softcut._core.HAVE_TINYOSC`). Per-voice `/set/param/cut/*` messages are parsed **and dispatched entirely in C without the GIL** — via a second single-producer command queue drained on the audio thread plus atomic parameter mirrors — and the phase poll runs in C too, so a busy Python interpreter can neither delay nor be delayed by the native control path (other addresses fall back to a Python handler). It is IPv4-only and less battle-tested than python-osc; prefer python-osc unless you specifically need zero-dependency or GIL-free OSC control.

A few addresses are partial, mirroring the norns layer's gaps: `enabled` maps to play, `in_cut` uses the scalar per-voice input gain (there is no per-channel ADC matrix), and level/pan slew and the VU poll are accepted-and-ignored. See [`docs/guide/osc.md`](docs/guide/osc.md) for the complete address table.

### Standalone server (no Python)

For a headless, interpreter-free deployment, [`clients/softcut-osc`](clients/softcut-osc/) builds a standalone native binary (softcut-lib + tinyosc + miniaudio) that speaks the same softcut OSC protocol with **no CPython at all** — so nothing on its control path can touch a GIL. It is the pure-C++ counterpart to `softcut.osc`: identical DSP and wire protocol, no library or numpy. It covers the full namespace plus WAV disk I/O (via the vendored [dr_wav](https://github.com/mackron/dr_libs), on a disk-worker thread with click-avoidance crossfades), `preserve`/`mix` blending, opt-in `--resample-on-read`, and device selection.

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

CI runs QA and a Linux/macOS/Windows build smoke on every push and pull request. Pushing a `v*` tag builds wheels for CPython 3.10-3.14 across Linux (x86_64/aarch64), macOS (x86_64/arm64) and Windows with [cibuildwheel](https://cibuildwheel.pypa.io), plus the sdist, and publishes them to PyPI via trusted publishing. `make release` bumps the version and creates the tag; pushing it triggers the release. (TestPyPI is available via the workflow's manual `workflow_dispatch`.)

## Notes

- Realtime parameter updates are safe: while the device is running, voice DSP parameter changes from Python are enqueued and applied on the audio thread via a lock-free queue rather than racing it. (The mix scalars `level`/`pan`/ `input_gain` and the feedback matrix are plain aligned writes.)

- The vendored `softcut-lib` carries small host-portability fixes (uninitialized members that relied on embedded zero-init static storage — including the phase quantum and the two phase mirrors the poll reports from — and an oversized debug buffer stubbed out); see the comments in `thirdparty/softcut-lib`.
