# softcut

![CI](https://github.com/shakfu/softcut-py/actions/workflows/ci.yml/badge.svg)

Python bindings for [softcut-lib](https://github.com/monome/softcut-lib) — the per-voice DSP engine behind monome norns' softcut — with realtime audio I/O via [miniaudio](https://github.com/mackron/miniaudio). Built with [nanobind](https://github.com/wjakob/nanobind).

The primary API exposes softcut as idiomatic Python objects. An optional [norns-compatible layer](#norns-compatible-api) (`softcut.norns`) additionally mirrors the flat norns Lua `softcut` API for porting existing scripts.

## Concepts

- **`Voice`** wraps one `softcut::Voice`: a crossfading read/write head over an audio buffer, with rate, loop points, record/play, fades, and pre/post state-variable filters. Parameters are plain attributes; the buffer is a numpy `float32` array **you** own (softcut-lib never allocates buffer memory). Buffer length must be a power of two — use `softcut.next_power_of_two` or `Engine.allocate`, which rounds up for you. The same array can be shared by several voices.

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

No device; process a mono numpy block through the voices and get the mixed stereo output back. This is the deterministic path used by the tests:

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
softcut.buffer_read_mono("loop.wav", ch_dst=1)   # numpy + stdlib wave, no extra dep
softcut.loop(1, 1)
softcut.loop_start(1, 0.0)
softcut.loop_end(1, 4.0)
softcut.rate(1, 1.0)
softcut.level(1, 0.8)
softcut.play(1, 1)

softcut.start()                                  # open the audio device
```

- **Attribute passthrough** — `rate`, `level`, `pan`, `play`/`rec`/`loop`, loop points, `position`, the pre/post filters, slews, phase, `buffer`, `voice_sync`, `level_cut_cut`, `reset`.
- **Buffer/disk ops** — `buffer_read_*` / `buffer_write_*`, `buffer_copy_*`, `buffer_clear*`, in pure numpy plus the standard-library `wave` module (WAV only, no new dependency), with preserve/mix crossfade, edge `fade_time` and `reverse`. Operations write in place, so they are safe against the running audio thread; reads are non-resampling, matching norns.

`softcut.render` / `softcut.start` / `softcut.stop` drive audio (norns runs its audio continuously; here you render offline or open the device explicitly). Phase polling and per-sample level/pan slews are not yet implemented; see [`docs/dev/norns-api.md`](docs/dev/norns-api.md) for the full mapping and status. `demos/12_norns_api.py` is a narrated walkthrough built entirely on this layer.

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

- The vendored `softcut-lib` carries small host-portability fixes (uninitialized members that relied on embedded zero-init static storage, and an oversized debug buffer stubbed out); see the comments in `thirdparty/softcut-lib`.
