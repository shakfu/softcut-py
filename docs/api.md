# API reference

```python
import softcut
from softcut import Voice, Engine, next_power_of_two, list_devices
```

## Voice

```python
Voice(sample_rate: float = 48000.0)
```

A single softcut DSP voice over a caller-owned `float32` buffer. Parameters are plain attributes; reading one returns the last value set.

### Buffer

| Attribute | Description |
| --- | --- |
| `sample_rate` | Sample rate in Hz. |
| `buffer` | The voice's audio buffer, a 1-D C-contiguous `float32` buffer you own (`array.array`, `memoryview` or ndarray). The length must be a positive power of two (else `ValueError`). The voice reads from and records into this memory in place; the same array may be shared by several voices. |

### Transport and loop

| Attribute | Description |
| --- | --- |
| `rate` | Playback speed multiplier (`1.0` normal, `2.0` octave up, negative = reverse). |
| `loop_start`, `loop_end` | Loop region bounds, in seconds. |
| `loop_region` | `(start, end)` tuple; setting it also enables looping. |
| `loop` | Loop flag (bool). |
| `fade_time` | Crossfade time at loop/cut boundaries, in seconds. |

### Record and play

| Attribute | Description |
| --- | --- |
| `play` | Play (read) flag. |
| `rec` | Record (write) flag. |
| `rec_once` | Record exactly one loop, then recording stops automatically. |
| `rec_level` | Gain applied to new input written into the buffer. |
| `pre_level` | Gain applied to existing buffer content when recording (`1` overdub, `0` replace, `<1` feedback decay). |
| `rec_offset` | Write-head offset relative to the read head, in seconds. |

### Slew

| Attribute | Description |
| --- | --- |
| `rate_slew_time` | Glide time for `rate` changes, in seconds (tape-stop, portamento). |
| `rec_pre_slew_time` | Slew time for `rec_level`/`pre_level` changes, in seconds. |

### Phase

| Attribute | Description |
| --- | --- |
| `phase_quant` | Quantization grid for `quant_phase`, in seconds. |
| `phase_offset` | Offset applied to the reported phase, in seconds. |

### Filters

The voice has a state-variable filter on the record path (`pre_filter_*`) and one on the output (`post_filter_*`). Each has a cutoff `fc`, reciprocal-Q `rq`, a `dry` mix, and per-mode mix levels.

| Pre filter | Post filter | Description |
| --- | --- | --- |
| `pre_filter_fc` | `post_filter_fc` | Cutoff frequency in Hz. |
| `pre_filter_rq` | `post_filter_rq` | Reciprocal of Q (resonance). |
| `pre_filter_lp` | `post_filter_lp` | Lowpass mix. |
| `pre_filter_hp` | `post_filter_hp` | Highpass mix. |
| `pre_filter_bp` | `post_filter_bp` | Bandpass mix. |
| `pre_filter_br` | `post_filter_br` | Band-reject (notch) mix. |
| `pre_filter_dry` | `post_filter_dry` | Dry (unfiltered) mix. |
| `pre_filter_fc_mod` | — | Amount the pre cutoff tracks `rate`. |

### Engine mix

These are read by an `Engine`; a standalone `Voice.process()` ignores them.

| Attribute | Description |
| --- | --- |
| `level` | Output level (linear gain). |
| `pan` | Stereo pan, `-1` (left) to `+1` (right), equal-power. |
| `input_gain` | Gain applied to the engine's external input fed into this voice. |

### Read-only state

| Attribute | Description |
| --- | --- |
| `position` | Current head position in seconds (audio-thread view). |
| `saved_position` | Head position updated once per block; safe to read from any thread. |
| `quant_phase` | Quantized phase, in units of `phase_quant`. |

### Methods

| Method | Description |
| --- | --- |
| `process(input)` | Process one mono block. Takes a 1-D `float32` array; returns a new `float32` array of the same length. |
| `cut_to(sec)` | Jump the head to `sec` seconds (with a crossfade); also starts a head. |
| `stop()` | Immediately stop both subheads. |
| `reset()` | Reset the voice's DSP state to defaults. |

### Convenience

| Method | Description |
| --- | --- |
| `configure(**params)` | Set several attributes at once; returns the voice (chainable). |
| `record(at=None)` | Context manager: on enter optionally `cut_to(at)` then turn play+rec on; on exit turn rec off. Non-blocking. |
| `record_for(seconds, at=None)` | Blocking capture: rec on, wait `seconds`, rec off. Returns the voice. |

## Engine

```python
Engine(
    voices: int = 2,
    sample_rate: float = 48000.0,
    mode: str = "duplex",        # "duplex" or "playback"
    block_size: int = 512,
    out_channels: int = 2,
    output_device: int = -1,     # index from list_devices(); -1 = default
    input_device: int = -1,
)
```

A multi-voice host owning its voices and an audio device. It is a context manager (enter starts the device, exit stops it) and a sequence of its voices.

### Sequence and access

| Member | Description |
| --- | --- |
| `len(eng)`, `eng[i]`, `iter(eng)` | Voice count, indexing, iteration. |
| `voices` | The list of voices. |
| `voice(i)` | The voice at index `i`. |

### Properties

| Property | Description |
| --- | --- |
| `sample_rate` | Sample rate in Hz. |
| `mode` | `"duplex"` or `"playback"`. |
| `block_size` | Processing block size in frames. |
| `running` | `True` while the device is started. |

### Methods

| Method | Description |
| --- | --- |
| `allocate(seconds=None, frames=None, shared=True)` | Allocate and assign zeroed `float32` buffer(s), rounded up to a power of two. Provide exactly one of `seconds`/`frames`. `shared=True` gives all voices one buffer; `False` gives each its own. Returns the buffer or the list of buffers. |
| `sync(follow, lead, offset=0.0)` | Cut the `follow` voice to the `lead` voice's position + `offset`. |
| `feedback(src, dst, amount=None)` | Get (omit `amount`) or set the feedback gain from voice `src`'s output into voice `dst`'s input. `src == dst` is self-feedback. Returns the engine when setting. |
| `render(input=None, out=None, *, seconds=None)` | Offline: process a 1-D `float32` mono buffer through all voices, or `seconds` of silence -- exactly one of the two. Writes interleaved frames into `out`, or into a fresh `array.array("f")` of `n * out_channels` samples, and returns it. Raises if the device is running. |
| `render_to(path, input=None, out=None, *, seconds=None)` | `render` followed by `write_wav`, taking the sample rate and channel count from the engine. Returns the path written. |
| `start()` / `stop()` | Start/stop the device (non-blocking). Return the engine. |

## Functions

| Function | Description |
| --- | --- |
| `next_power_of_two(n)` | Smallest power of two `>= n` (and `>= 1`). |
| `list_devices()` | List the system audio devices as dicts with keys `index`, `name`, `type` (`"playback"`/`"capture"`), `is_default`. |
| `write_wav(path, data, sr, channels=None)` | Write float32 samples as 16-bit PCM. Takes any C-contiguous float32 buffer; a 2-D shape supplies the channel count, otherwise pass `channels`. Returns the path. |
| `read_wav(path)` | Decode a WAV to interleaved float32. Returns `(data, channels, sample_rate)`, with `data` an `array.array("f")`. Handles 8/16/24/32-bit integer PCM. |
| `read_wav_mono(path)` | The same, with channels averaged. Returns `(data, sample_rate)`. |

## Softcut

!!! warning "Deprecated" `softcut.Softcut` is a deprecated alias for `Engine` (defaulting to playback mode), kept for the pre-1.0 transition. Use `Engine`.
