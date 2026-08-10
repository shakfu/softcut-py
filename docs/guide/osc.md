# OSC server

`softcut.osc` exposes softcut over the same OSC wire protocol as the reference [`softcut_jack_osc`](https://github.com/monome/softcut-lib) client. Existing norns/Lua scripts, SuperCollider, Max, or any OSC controller can drive softcut-py as a drop-in engine over the network. It is a thin dispatch layer over the [norns host](../dev/norns-api.md): each address maps to a host method, with the one translation that the wire protocol is **0-based** (voices 0-5, buffers 0-1) while the host is **1-based**.

## Running a server

```python
from softcut.osc import SoftcutOSC
from softcut import norns

host = norns.NornsSoftcut()      # 6 voices, 2 global buffers
host.start()                      # open the audio device
server = SoftcutOSC(host)         # listen on UDP 9999
server.serve_forever()            # blocks until a /quit message
```

Or from the command line:

```bash
python -m softcut.osc                 # audio device + OSC server
python -m softcut.osc --no-audio      # offline: buffer ops only, no device
python -m softcut.osc --listen-port 9999 --reply-port 57120 --backend auto
```

For background use (tests, embedding), `start()` runs the server on a background thread and returns immediately; `shutdown()` stops it. `SoftcutOSC` is also a context manager.

```python
with SoftcutOSC(host) as server:      # start() on enter, shutdown() on exit
    ...
```

The server never opens the audio device itself — call `host.start()` (or use the command line without `--no-audio`) to go live.

## Transports

Two transports share one dispatch table, selected by `backend=`:

| backend | requires | notes |
|---|---|---|
| `"auto"` (default) | — | native if built, else python-osc |
| `"native"` | nothing — built in by default | dependency-free UDP + vendored tinyosc; IPv4-only |
| `"python-osc"` | `pip install softcut-py[osc]` | pure-Python; longer-established |

tinyosc is compiled into the extension by default, including in the published
wheels, so `pip install softcut-py` serves OSC with nothing else installed and
`auto` resolves to `native`. That also means the transport is exercised by
ordinary use rather than by a CI leg alone. Turn it off with:

```bash
SKBUILD_CMAKE_DEFINE="SOFTCUT_ENABLE_TINYOSC=OFF" pip install .
# or, in this repo:
make build-no-tinyosc
```

!!! note "The server is never implicit" Importing `softcut.osc` opens no socket
    and starts no thread. A server exists when you construct `SoftcutOSC` and
    listens when you start it, or when you run `python -m softcut.osc`. Building
    the transport in grants the *ability* to serve, never a running server.

Whether it was built is reported by `softcut._core.HAVE_TINYOSC` (and `softcut.osc.NATIVE_OSC_AVAILABLE`). Per-voice `/set/param/cut/*` messages are parsed **and dispatched entirely in C, without the GIL** — via a second single-producer command queue drained on the audio thread plus atomic parameter mirrors — and the phase poll runs in C too, so a busy Python interpreter can neither delay nor be delayed by the native control path. Other addresses (buffer ops, lifecycle) fall back to a Python handler.

OSC is transport-bound (a socket syscall dominates each message), so this is about **latency-jitter robustness under a busy interpreter**, not raw throughput. [`benchmarks/osc_jitter.py`](https://github.com/shakfu/softcut-py/blob/main/benchmarks/osc_jitter.py) measures it: under GIL contention the native dispatch tail stays roughly flat (p99 ~0.08 ms) while python-osc's blows out (~7.6 ms).

## Address namespace

Voice and buffer/channel indices are 0-based on the wire. Types: `i` int32, `f` float32, `s` string.

### Per-voice parameters — `/set/param/cut/<name> i f`

`rate`, `loop_start`, `loop_end`, `loop_flag`, `fade_time`, `rec_level`, `pre_level`, `rec_flag`, `rec_once`, `play_flag`, `rec_offset`, `position`, `recpre_slew_time`, `rate_slew_time`, `phase_quant`, `phase_offset`, and every `pre_filter_*` / `post_filter_*` (`fc`, `fc_mod`, `rq`, `lp`, `hp`, `bp`, `br`, `dry`).

Two multi-argument params:

| address | types | meaning |
|---|---|---|
| `/set/param/cut/voice_sync` | `i i f` | sync one voice to another + offset |
| `/set/param/cut/buffer` | `i i` | assign buffer (0/1) to a voice |

### Routing / mixer

| address | types | meaning |
|---|---|---|
| `/set/level/cut` | `i f` | voice output level |
| `/set/pan/cut` | `i f` | voice pan |
| `/set/level/cut_cut` | `i i f` | voice -> voice feedback |
| `/set/level/in_cut` | `i i f` | input -> voice level (partial) |
| `/set/enabled/cut` | `i f` | enable voice (partial: maps to play) |

### Buffer / disk

| address | types | meaning |
|---|---|---|
| `/softcut/buffer/read_mono` | `s f f f i i` | path, startSrc, startDst, dur, chSrc, chDst |
| `/softcut/buffer/read_stereo` | `s f f f` | path, startSrc, startDst, dur |
| `/softcut/buffer/write_mono` | `s f f i` | path, start, dur, ch |
| `/softcut/buffer/write_stereo` | `s f f` | path, start, dur |
| `/softcut/buffer/clear` | | zero both buffers |
| `/softcut/buffer/clear_channel` | `i` | zero one buffer |
| `/softcut/buffer/clear_region` | `f f` | start, dur (both buffers) |
| `/softcut/buffer/clear_region_channel` | `i f f` | ch, start, dur |
| `/softcut/reset` | | reset voices + restore routing |

Trailing arguments on the buffer read/write messages are optional and fall back to their defaults, matching the reference.

### Lifecycle and polls

| address | types | meaning |
|---|---|---|
| `/hello` | | log a hello |
| `/goodbye`, `/quit` | | stop the server |
| `/poll/start/cut/phase` | | start the phase poll |
| `/poll/stop/cut/phase` | | stop the phase poll |
| `/poll/start/vu`, `/poll/stop/vu` | | accepted-and-ignored (no meter) |

## Phase poll

While the phase poll is running, a background thread scans each voice's quantized phase and sends, whenever it changes:

```
/poll/softcut/phase <voice:i> <phase:f>      # to reply_host:reply_port
```

Set `phase_quant` (and optionally `phase_offset`) per voice to control the quantization. Replies go to `reply_host:reply_port` (default `127.0.0.1:57120`, SuperCollider's `sclang` port, as in the reference). With the native backend the poll loop also runs in C (`_core._OscPhasePoll`), so it holds no GIL.

## Standalone server (no Python)

For a headless, interpreter-free deployment, the repo also builds a standalone native binary (`clients/softcut-osc`, `make build-standalone`) that speaks this same protocol with no CPython at all — the pure-C++ counterpart to `softcut.osc`, sharing its Python-free core (mixer, device, sockets) under `src/shared`. It adds WAV disk I/O via the vendored dr_wav on a disk-worker thread with crossfades, `preserve`/`mix` blending, opt-in `--resample-on-read`, and device selection. See [`clients/softcut-osc/README.md`](https://github.com/shakfu/softcut-py/blob/main/clients/softcut-osc/README.md).

## Known partials

These mirror the norns layer's gaps rather than the OSC layer inventing new ones:

- `/set/enabled/cut` — the core has no per-voice idle-disable; it is approximated with the play flag.

- `/set/level/in_cut` — the core has a single scalar input gain per voice (mono duplex), not a per-channel ADC matrix, so the input channel index is ignored.

- `level_slew_time` / `pan_slew_time` — per-sample DSP slews softcut-lib supports but the binding does not yet expose; accepted-and-ignored.

- `/poll/*/vu` — no VU meter (also dead code in the reference client).
