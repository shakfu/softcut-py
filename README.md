# softcut

Python bindings for [softcut-lib](https://github.com/monome/softcut-lib) — the
per-voice DSP engine behind monome norns' softcut — with realtime audio I/O via
[miniaudio](https://github.com/mackron/miniaudio). Built with
[nanobind](https://github.com/wjakob/nanobind).

This is not a port of the norns Lua API; it exposes softcut as Python objects.

## Concepts

- **`Voice`** wraps one `softcut::Voice`: a crossfading read/write head over an
  audio buffer, with rate, loop points, record/play, fades, and pre/post
  state-variable filters. Parameters are plain attributes; the buffer is a numpy
  `float32` array **you** own (softcut-lib never allocates buffer memory). Buffer
  length must be a power of two — use `softcut.next_power_of_two` or
  `Engine.allocate`, which rounds up for you. The same array can be shared by
  several voices.
- **`Engine`** is the multi-voice host: it owns a set of voices and a miniaudio
  device, and runs them either live (realtime mic/speaker I/O on a background
  audio thread) or offline via `Engine.render`. It is a context manager and a
  sequence of voices.

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

`eng.start()` returns immediately and audio runs on a background thread, so the
REPL stays live — set a parameter and you hear the change on the next block.
`record()` is the non-blocking context-manager gesture; `record_for(seconds)`
blocks the calling thread for a fixed capture.

## Offline rendering

No device; process a mono numpy block through the voices and get the mixed
stereo output back. This is the deterministic path used by the tests:

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

Load/save audio with whatever you like (e.g. `soundfile`) and assign the array
to `voice.buffer`.

## Build and test

```bash
make sync     # set up the environment
make test     # run the test suite
make qa       # test + lint + typecheck + format
```

Set `SOFTCUT_TEST_AUDIO=1` to additionally exercise a real audio device in the
test suite. Use `make help` for more targets (wheel, sdist, clean, etc.).

## Notes

- Realtime parameter updates from Python are best-effort: softcut parameters are
  single aligned scalars, so a concurrent read on the audio thread is at worst
  stale by one block. A lock-free command queue is a possible future hardening.
- The vendored `softcut-lib` carries small host-portability fixes (uninitialized
  members that relied on embedded zero-init static storage, and an oversized
  debug buffer stubbed out); see the comments in `thirdparty/softcut-lib`.
