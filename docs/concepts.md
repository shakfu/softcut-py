# Concepts

## Voice

A [`Voice`](api.md#voice) wraps one `softcut::Voice`: a crossfading read/write head over an audio buffer. It is the unit of sound. Parameters are plain attributes — setting one takes effect on the next processed block:

```python
v = softcut.Voice(sample_rate=48000)
v.rate = 2.0          # octave up, half loop time
v.loop_region = (0, 4)
v.rec = v.play = True
```

A voice is mono. Stereo output comes from the engine mixing several voices via their `level` and `pan`.

## Buffers

softcut-lib **owns no buffer memory** — a voice's buffer is a numpy `float32` array that *you* own and assign:

```python
import numpy as np
v.buffer = np.zeros(2**16, dtype=np.float32)
```

Two constraints follow from the DSP:

!!! warning "Buffer length must be a power of two" The read/write head wraps its index with a bitmask, so the frame count must be a positive power of two. Use [`next_power_of_two`](api.md#functions) or [`Engine.allocate`](api.md#engine), which rounds up for you. A non-power-of-two length raises `ValueError`.

The same array can be **shared** between voices — assign it to several voices and they read and record into common memory (norns' model).

## The record model

When a voice has both `rec` and `play` on, each sample it writes is:

```text
buffer = buffer * pre_level + input * rec_level
```

That single rule covers a lot of looping technique:

| pre_level | rec_level | result |
| --- | --- | --- |
| 1 | 1 | **overdub** (sum new on top of old) |
| 0 | 1 | **replace** (erase old, write new) |
| < 1 | 1 | **feedback decay** (old material dissolves while new layers in) |

Automating `pre_level`/`rec_level` with a slew gives partial replaces that fade in and out. See the [Frippertronics demo](demos.md).

!!! note "Recording the first loop is silent" A voice's output is the buffer *read*, which runs slightly ahead of the write head. So the first pass while recording a fresh buffer is near-silent; you hear the material on the next loop. To make a loop audible immediately, pre-load the array (`v.buffer[:n] = samples`) instead of recording it.

## Engine

An [`Engine`](api.md#engine) hosts a fixed set of voices over a miniaudio device. It is:

- a **context manager** — entering starts the device, exiting stops it;

- a **sequence** of its voices — `len(eng)`, `eng[i]`, `for v in eng`;

- a **mixer** — each voice's mono output is panned and summed to stereo.

It runs in two ways:

- **Live** (`start()`/`stop()` or `with`) — audio runs on a background thread; `start()` returns immediately and the REPL stays responsive. See [Live looping](guide/live-looping.md).

- **Offline** (`render()`) — process a numpy block synchronously and get the mixed stereo output back. Deterministic; used by the tests. See [Offline rendering](guide/offline-rendering.md).

`mode` is `"duplex"` (live mic input feeds recording voices) or `"playback"` (speakers only). Voices can also feed each other — see [Routing & feedback](guide/routing.md).
