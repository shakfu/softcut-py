# Design proposal: a norns-compatible API layer

Status: proposal (not yet implemented)

## Goal

Offer an optional API that mirrors the norns [softcut Lua API](https://monome.org/docs/norns/api/modules/softcut.html) on top of softcut-py's object-oriented core, so that norns-style scripts (and the muscle memory that goes with them) port with minimal change. The native `Engine`/`Voice` API stays the primary, idiomatic interface; this is an additive compatibility layer.

## Why it is feasible

The norns `softcut` module is a flat functional namespace over a fixed global engine: every call is `softcut.thing(voice, value)`, where `voice` is a 1-based integer index into 6 hardware voices and 2 fixed global mono buffers. It is a thin Lua veneer over OSC messages to the `crone`/softcut audio process.

softcut-py's core is already the same shape underneath. `softcut::Voice` holds a single 1-D mono float buffer (`BufferArray = ndarray<float, ndim<1>>` in `_core.cpp`), and an `Engine(voices=6)` gives the fixed voice set. The compatibility layer is therefore a translation shim, not a re-implementation of the DSP.

Notably, the buffer and disk operations become *simpler* in Python than on norns. norns implements them in C on a background disk thread (`BufDiskWork`); here they reduce to numpy slicing plus the standard-library `wave` module, because numpy already is the buffer vocabulary.

## Architecture: a singleton facade

norns' `softcut` is a global singleton with an implicit layout (6 voices, 2 global mono buffers, 1-based indexing). The shim reproduces that:

```python
# softcut/norns.py  -- import as: from softcut import norns as softcut
class _NornsSoftcut:
    def __init__(self, sr=48000.0):
        self._eng = Engine(voices=6, sample_rate=sr, mode="duplex")
        # two global mono buffers, norns-style; index 1 and 2
        self._buf = {1: np.zeros(2**24, np.float32), 2: np.zeros(2**24, np.float32)}
        self._assign = {}                       # voice(1-based) -> buffer id
    def rate(self, i, r):   self._eng[i-1].rate = r          # flat passthrough
    def level(self, i, a):  self._eng[i-1].level = a
    def buffer(self, i, b): self._eng[i-1].buffer = self._buf[b]; self._assign[i]=b
    ...
softcut = _NornsSoftcut()                        # module-level singleton
```

The 1-based, flat-function norns idiom lives entirely in this module and drives the object API internally. Existing norns Lua scripts port almost verbatim.

## Mapping tiers

The API divides into four tiers by implementation cost.

### Tier A: trivial attribute passthrough (~40 functions)

One-liners that set a `Voice` attribute or call an existing method: `play`, `rate`, `loop`, `loop_start`/`loop_end`, `level`, `pan`, `rec`, `rec_level`, `pre_level`, `position` (to `cut_to`), `fade_time`, all `pre_filter_*` / `post_filter_*`, `phase_quant`, `phase_offset`, `voice_sync` (to `Engine.sync`), `rate_slew_time`, `recpre_slew_time`.

### Tier B: buffer/disk operations in pure Python (the main win)

norns implements these in C on a disk thread; here they are numpy plus the Python standard library `wave` module, so Tier B adds no new dependency (the project ships numpy only). The demos already carry a proven WAV codec in `demos/_util.py` (`load_wav_mono`/`write_wav`, handling 8/16/24/32-bit PCM decode, mono-summing, and 16-bit write); the shim promotes these helpers into the package (e.g. `softcut/_wavio.py`) so demos and the norns layer share one tested implementation. WAV is the only format norns' `buffer_read_*` needs in practice, so this is a deliberate scope choice, not a limitation to work around.

- `buffer_read_mono`/`buffer_read_stereo`: decode the WAV to a float32 array, then write into the buffer slice, honoring `start_src`/`start_dst`/`dur` offsets and the `preserve`/`mix` crossfade (`dst = dst*preserve + src*mix` over the region). Match norns' non-resampling semantics: write samples at the file's rate (a sample-rate mismatch shifts pitch, as on norns) rather than resampling to the engine rate.

- `buffer_write_mono`/`buffer_write_stereo`: slice, then write a 16-bit PCM WAV.

- `buffer_copy_mono`/`buffer_copy_stereo`: numpy slice assignment, with optional `reverse` (`[::-1]`) and a `fade_time` ramp.

- `buffer_clear*`: `region[:] = 0`, or a windowed fade for `clear_region`'s `fade_time`/`preserve`.

These are simpler than the norns originals because the buffer is already a numpy array.

### Tier C: phase polling via a helper thread (~6 functions)

norns' `event_phase(func)` plus `poll_start_phase()` is asynchronous because it crosses an OSC boundary. The shim runs a small daemon thread that, every `phase_quant` seconds, reads each voice's `position` property and dispatches registered callbacks, reproducing `poll_start_phase`/`poll_stop_phase`/`event_phase`/`query_position`/`event_position`. `render_buffer`/`event_render` (waveform snapshot) is trivial: downsample the numpy buffer and call the callback.

### Tier D: needs core C++ work (the only real gaps)

1. `level_slew_time` / `pan_slew_time`: softcut-lib's `Voice` supports these slews but the binding does not expose them (only `rate` and `recpre` slew are in `_core.pyi`). True slew is per-sample inside the DSP and cannot be approximated in Python. Fix: add two `def_prop_rw` lines to `_core.cpp`. Small.

2. `level_input_cut(ch, voice, amp)`: norns' full ADC-to-voice routing matrix. The core has a scalar `input_gain` per voice and mono duplex input, not a per-channel grid. Partial without core changes.

3. `enable(voice, state)`: no per-voice idle-disable exists; minor, can stub as a no-op or map to `stop()`.

4. `defaults()` / `params()`: norns-menu controlspec tables, meaningless off-device. Stub or omit.

## The caveat that matters: thread safety

norns reads buffers on a disk thread with fades, so it never races the audio thread. In softcut-py the audio thread reads the numpy buffer live. Writing samples in place (`buf[a:b] = ...`) is safe: the allocation is unchanged and float stores do not tear audibly. Reassigning the buffer (`v.buffer = new_array`, which calls `set_buffer` and swaps the pointer) while running is not safe.

The shim's rule is therefore: disk/copy/clear operations write into the existing array in place and never reallocate while `eng.running`. This matches norns semantics. A `buffer_read` of a file larger than the current buffer is the one case that needs a stop-reallocate-restart or a documented error.

## Scope options

- Tiers A+B+C: pure Python, zero core changes, zero new dependencies (numpy plus the stdlib `wave` module). Gives a norns-script-compatible API on its own.

- Tier D: about four lines of C++ to expose the two missing slews (worth doing); the input routing matrix is the only genuine feature gap.

## Function reference: Lua to Python mapping

| norns Lua | softcut-py native | Tier |
|---|---|---|
| `rate(i, r)` | `eng[i].rate = r` | A |
| `level(i, a)` | `eng[i].level = a` | A |
| `pan(i, p)` | `eng[i].pan = p` | A |
| `play(i, s)` | `eng[i].play = s` | A |
| `rec(i, s)` | `eng[i].rec = s` | A |
| `rec_level(i, a)` | `eng[i].rec_level = a` | A |
| `pre_level(i, a)` | `eng[i].pre_level = a` | A |
| `loop(i, s)` | `eng[i].loop = s` | A |
| `loop_start(i, p)` / `loop_end(i, p)` | `eng[i].loop_region = (p0, p1)` | A |
| `position(i, p)` | `eng[i].cut_to(p)` | A |
| `fade_time(i, t)` | `eng[i].fade_time = t` | A |
| `rate_slew_time(i, t)` | `eng[i].rate_slew_time = t` | A |
| `recpre_slew_time(i, t)` | `eng[i].rec_pre_slew_time = t` | A |
| `pre_filter_*` / `post_filter_*` | same-named attributes | A |
| `phase_quant(i, q)` / `phase_offset(i, o)` | same-named attributes | A |
| `voice_sync(dst, src, off)` | `Engine.sync(dst, src, off)` | A |
| `buffer(i, b)` | assign global buffer array to `eng[i].buffer` | A |
| `buffer_read_mono/stereo` | stdlib `wave` decode + in-place slice write | B |
| `buffer_write_mono/stereo` | slice + stdlib `wave` 16-bit PCM write | B |
| `buffer_copy_mono/stereo` | numpy slice assignment | B |
| `buffer_clear*` | numpy region zero/fade | B |
| `poll_start_phase` / `poll_stop_phase` | helper thread start/stop | C |
| `event_phase(func)` / `event_position(func)` | callback registry polled by thread | C |
| `query_position(i)` | read `eng[i].position` | C |
| `render_buffer` / `event_render` | downsample numpy buffer + callback | C |
| `level_slew_time` / `pan_slew_time` | not exposed; needs `_core.cpp` addition | D |
| `level_input_cut(ch, i, a)` | partial; core has scalar `input_gain` only | D |
| `level_cut_cut(src, dst, a)` | `Engine.feedback(src, dst, a)` | A |
| `enable(i, s)` | no-op or `stop()`; no idle-disable | D |
| `reset()` | per-voice `reset()` loop | A |
| `defaults()` / `params()` | norns-menu specific; stub or omit | D |
