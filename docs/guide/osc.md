# OSC server

`softcut.osc` exposes softcut over the same OSC wire protocol as the reference
[`softcut_jack_osc`](https://github.com/monome/softcut-lib) client. Existing
norns/Lua scripts, SuperCollider, Max, or any OSC controller can drive
softcut-py as a drop-in engine over the network. It is a thin dispatch layer
over the [norns host](../dev/norns-api.md): each address maps to a host
method, with the one translation that the wire protocol is **0-based** (voices
0-5, buffers 0-1) while the host is **1-based**.

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

For background use (tests, embedding), `start()` runs the server on a background
thread and returns immediately; `shutdown()` stops it. `SoftcutOSC` is also a
context manager.

```python
with SoftcutOSC(host) as server:      # start() on enter, shutdown() on exit
    ...
```

The server never opens the audio device itself — call `host.start()` (or use the
command line without `--no-audio`) to go live.

## Transports

Two transports share one dispatch table, selected by `backend=`:

| backend | requires | notes |
|---|---|---|
| `"auto"` (default) | — | native if built, else python-osc |
| `"python-osc"` | `pip install softcut-py[osc]` | pure-Python; core stays numpy-only |
| `"native"` (experimental) | source build with `SOFTCUT_ENABLE_TINYOSC` | dependency-free UDP + vendored tinyosc |

!!! warning "The native transport is experimental"
    It is **not** compiled into the published wheels and must be enabled in a
    source build. It is IPv4-only and far less battle-tested than python-osc,
    and the benchmark ([`benchmarks/osc_dispatch.py`](https://github.com/shakfu/softcut-py/blob/main/benchmarks/osc_dispatch.py))
    shows no meaningful speed advantage (OSC is transport-bound). Use it only
    when you specifically need zero-dependency OSC; otherwise prefer python-osc.

The native transport is compiled in with the CMake option (source build only):

```bash
SKBUILD_CMAKE_DEFINE="SOFTCUT_ENABLE_TINYOSC=ON" pip install .
# or, in this repo:
make build-tinyosc
```

Whether it was built is reported by `softcut._core.HAVE_TINYOSC` (and
`softcut.osc.NATIVE_OSC_AVAILABLE`). Receiving and parsing run in C, but dispatch
runs under the GIL: the DSP command queue is single-producer (the Python control
thread), so parameter writes must serialize with it via the GIL rather than
posting from a second thread. The native path is therefore a **dependency-free
transport, not a GIL-free fast path**.

## Address namespace

Voice and buffer/channel indices are 0-based on the wire. Types: `i` int32,
`f` float32, `s` string.

### Per-voice parameters — `/set/param/cut/<name> i f`

`rate`, `loop_start`, `loop_end`, `loop_flag`, `fade_time`, `rec_level`,
`pre_level`, `rec_flag`, `rec_once`, `play_flag`, `rec_offset`, `position`,
`recpre_slew_time`, `rate_slew_time`, `phase_quant`, `phase_offset`, and every
`pre_filter_*` / `post_filter_*` (`fc`, `fc_mod`, `rq`, `lp`, `hp`, `bp`, `br`,
`dry`).

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

Trailing arguments on the buffer read/write messages are optional and fall back
to their defaults, matching the reference.

### Lifecycle and polls

| address | types | meaning |
|---|---|---|
| `/hello` | | log a hello |
| `/goodbye`, `/quit` | | stop the server |
| `/poll/start/cut/phase` | | start the phase poll |
| `/poll/stop/cut/phase` | | stop the phase poll |
| `/poll/start/vu`, `/poll/stop/vu` | | accepted-and-ignored (no meter) |

## Phase poll

While the phase poll is running, a background thread scans each voice's quantized
phase and sends, whenever it changes:

```
/poll/softcut/phase <voice:i> <phase:f>      # to reply_host:reply_port
```

Set `phase_quant` (and optionally `phase_offset`) per voice to control the
quantization. Replies go to `reply_host:reply_port` (default `127.0.0.1:57120`,
SuperCollider's `sclang` port, as in the reference).

## Known partials

These mirror the norns layer's gaps rather than the OSC layer inventing new ones:

- `/set/enabled/cut` — the core has no per-voice idle-disable; it is approximated
  with the play flag.
- `/set/level/in_cut` — the core has a single scalar input gain per voice (mono
  duplex), not a per-channel ADC matrix, so the input channel index is ignored.
- `level_slew_time` / `pan_slew_time` — per-sample DSP slews softcut-lib supports
  but the binding does not yet expose; accepted-and-ignored.
- `/poll/*/vu` — no VU meter (also dead code in the reference client).
