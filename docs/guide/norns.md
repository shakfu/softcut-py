# The norns-compatible layer

`softcut.norns` mirrors the flat [softcut Lua API](https://monome.org/docs/norns/api/modules/softcut.html) that norns scripts are written against, on top of the same DSP. It exists so that a norns script — and the muscle memory that goes with it — ports with minimal change.

The [`Engine`/`Voice` API](../concepts.md) remains the primary interface. This is additive: a translation shim over the same engine, not a second implementation.

## Importing it

Import it under the name norns scripts expect, and the calls read verbatim:

```python
from softcut import norns as softcut

softcut.buffer_clear()
softcut.buffer_read_mono("loop.wav", ch_dst=1)
softcut.loop_start(1, 0.0)
softcut.loop_end(1, 4.0)
softcut.loop(1, 1)
softcut.rate(1, 1.0)
softcut.level(1, 0.8)
softcut.play(1, 1)
softcut.position(1, 0.0)     # cut the head into the loop -- see below

softcut.start()              # open the audio device
```

Every name resolves to a process-wide singleton, as on norns: **6 voices indexed from 1**, and **2 global mono buffers numbered 1 and 2**. Flags take `0`/`1` the way Lua passes them, and `True`/`False` work too.

For tests or for more than one engine, construct the class directly instead:

```python
from softcut.norns import NornsSoftcut

host = NornsSoftcut(sample_rate=48000.0, buffer_frames=2**16, mode="playback")
host.rate(1, 0.5)
```

## Parameters

Each of these is `softcut.<name>(voice, value)`, with `voice` in 1-6:

| group | functions |
| --- | --- |
| transport | `rate`, `play`, `rec`, `loop`, `loop_start`, `loop_end`, `position` |
| levels | `level`, `pan`, `rec_level`, `pre_level` |
| record | `rec_once`, `rec_offset`, `fade_time` |
| slews | `rate_slew_time`, `recpre_slew_time` |
| phase | `phase_quant`, `phase_offset` |
| pre filter | `pre_filter_fc`, `pre_filter_fc_mod`, `pre_filter_rq`, `pre_filter_lp`, `pre_filter_hp`, `pre_filter_bp`, `pre_filter_br`, `pre_filter_dry` |
| post filter | `post_filter_fc`, `post_filter_rq`, `post_filter_lp`, `post_filter_hp`, `post_filter_bp`, `post_filter_br`, `post_filter_dry` |

Plus the ones that take more than a voice and a value:

| | |
| --- | --- |
| `buffer(voice, b)` | assign global buffer 1 or 2 to a voice |
| `voice_sync(dst, src, offset=0.0)` | cut `dst` to `src`'s position plus an offset |
| `level_cut_cut(src, dst, amount)` | route one voice's output into another's input |
| `reset()` | every voice back to defaults, and the default buffer routing |

!!! warning "A loop does not loop until a head is cut into it" Setting `loop_start`/`loop_end`/`loop` describes a region; it does not move the play head. Until `position()` cuts the head somewhere, it free-runs straight past `loop_end` and off the end of your material — which sounds like the loop simply stopping. This is softcut's behaviour rather than this layer's, and it is why norns scripts habitually call `softcut.position(i, 0)` after setting a region.

## Buffers and disk

The two global buffers are reachable as `softcut.buffers`, a dict keyed `1` and `2`:

```python
buf = softcut.buffers[1]            # array.array("f"), the real memory
len(buf)                            # frames

import numpy as np
view = np.asarray(buf)              # a writable view, not a copy
```

Disk and buffer operations mirror norns':

| | |
| --- | --- |
| `buffer_read_mono(file, start_src=0, start_dst=0, dur=-1, ch_src=1, ch_dst=1, preserve=0, mix=1)` | one file channel into one buffer |
| `buffer_read_stereo(file, start_src=0, start_dst=0, dur=-1, preserve=0, mix=1)` | a file's two channels into buffers 1 and 2 |
| `buffer_write_mono(file, start=0, dur=-1, ch=1)` | one buffer out as a mono WAV |
| `buffer_write_stereo(file, start=0, dur=-1)` | both buffers out as an interleaved WAV |
| `buffer_copy_mono(src_ch, dst_ch, ..., fade_time=0, preserve=0, reverse=0)` | copy a region, optionally reversed |
| `buffer_copy_stereo(...)` | the same for both buffers |
| `buffer_clear()`, `buffer_clear_channel(ch)` | zero everything, or one buffer |
| `buffer_clear_region(start, dur, fade_time=0, preserve=0)` | zero a region of both |
| `buffer_clear_region_channel(ch, start, dur, ...)` | zero a region of one |

Three things are worth knowing about them:

- **Reads do not resample.** A file at a different sample rate plays back pitch-shifted, exactly as on norns. Resample beforehand if you do not want that.
- **`preserve` and `mix` blend rather than overwrite.** `dst = dst*preserve + src*mix` — the same rule as the record model — so a read can fade into what is already there, and `fade_time` puts a ramp on each edge to avoid a click.
- **They write in place and never reallocate**, so they are safe to call while audio is running. Assigning a whole new array to a voice is not; see [Concepts](../concepts.md#buffers).

WAV is the only format, handled by the standard library's `wave` module plus a C conversion — no dependency, and no other container.

## Driving audio

This is the one place the layer deliberately departs from norns. On norns the audio engine is always running, so its API has nothing to start. Here you say when:

```python
softcut.start()                     # open the device; audio runs on its own thread
softcut.stop()                      # close it
out = softcut.render(seconds=4)     # or process a block offline, no device at all
```

`render` returns interleaved frames — see [Offline rendering](offline-rendering.md). The underlying `Engine` is available as `softcut.engine` if you want to drop back to the object API for something the flat one does not cover.

## What is not here

- **Phase polling.** norns' `poll_start_phase`/`event_phase` is not in this layer. The [OSC server](osc.md) provides it instead, reporting `/poll/softcut/phase`, which is also how a controller gets it. Per-voice `phase_quant` and `phase_offset` are here; the reporting is there.
- **`level_slew_time` and `pan_slew_time`.** softcut-lib supports these per-sample slews; the binding does not expose them yet. Over OSC they are accepted and ignored.
- **The input routing matrix.** norns' `level_in_cut` addresses a per-channel ADC matrix; this engine has one scalar input gain per voice (`softcut.engine[i].input_gain`), so the input channel index has nothing to select.
- **`enable`** maps to the play flag, as the core has no per-voice idle-disable.

[`demos/12_norns_api.py`](../demos.md) is a narrated walkthrough built entirely on this layer, and [the design note](../dev/norns-api.md) records why the mapping is shaped the way it is.
