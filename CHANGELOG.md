# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `clients/touchosc`: a TouchOSC control surface (`softcut.tosc`) covering the OSC namespace across eight pages — a mixer strip per voice, tabular loop/record/pre-filter/post-filter tables, the cut-to-cut feedback and voice-sync matrices, buffer and disk operations, and a receive-only phase readout driven by the phase poll. Every control sends a real address, with the voice index as a constant integer argument and the control's value scaled into the parameter's range; indices are 0-based on the wire and 1-based in the captions, as the protocol and norns respectively have them. Persistent state gets a fader or a latching toggle, one-shot commands (voice sync, buffer assignment, reset, disk I/O) get a momentary button, and the sync diagonal is a blank because syncing a voice to itself does nothing.

  Controls open at the values a freshly reset voice actually holds — level at unity, pan centred, rate at 1, the pre-filter low-passed and the post-filter dry — so the surface agrees with the engine before anything is touched; TouchOSC transmits nothing on load, so these are a display and not a preset. Each filter's cutoff fader spans up to that filter's own default (16 kHz pre, 12 kHz post), and the `rq` rows are captioned `low = resonant` since rq is reciprocal Q and tames the filter as it rises. Bindings are on TouchOSC connection 1 alone rather than all ten slots, so a second OSC destination added later receives nothing from this surface.

  Calibrated to softcut-py's conventions, which the standalone `clients/softcut-osc` binary shares but softcut-lib's own `softcut_jack_osc` demo client does not: its pan is `0..1` rather than `-1..1`, and it resets output level to `0` and `phase_quant` to `1`.

- `make touchosc`: the surface is generated rather than drawn, by `clients/touchosc/build_layout.py` with [py2tosc](https://pypi.org/project/py2tosc/) (a new dev dependency), so it can be reshaped for a different canvas, voice count, time range or disk paths by rerunning it. It also writes `softcut.xml`, TouchOSC's readable export, for inspecting what a control carries; that one is git-ignored at ~2 MB, and `--no-xml` skips it. `tests/test_touchosc.py` pulls every binding out of the layout, synthesises the message TouchOSC would send at both ends of each control's travel, and pushes it through the OSC server's own dispatch table — catching address drift in either direction, and checking that replaying a fader at its resting position leaves a fresh voice untouched.

- `src/shared/buffer_ops.hpp`: the sample-level buffer arithmetic every buffer operation reduces to — the blended in-place write (`apply_to_buffer`), its edge-fade envelope, channel de-interleaving and 16-bit quantization — promoted out of the standalone server's `buffer_io.hpp` into the Python-free shared core, and bound into the extension as `_core._buffer_apply` and `_core._buffer_extract_channel`. Read, copy and clear are all the one primitive, and it had been written twice: in numpy in `softcut.norns` and in C++ for the standalone. Both hosts now compile the same source, and `tests/test_buffer_ops.py` pins the binding to the numpy implementation sample for sample. The bindings take any C-contiguous float32 buffer (`ndarray`, `array.array`, `memoryview`) and release the GIL while looping, so a long buffer edit does not starve the interpreter. The disk layer above them stays with the standalone, which needs dr_wav and its own `Engine` type.

- `softcut.osc._PhasePoll.errors`: how many scans have failed since the poll was last started.

### Changed

- `softcut.norns`'s buffer operations now run on the shared C++ primitive rather than on numpy. `_apply` delegates to `_core._buffer_apply` and the numpy edge-envelope helper is gone, so read, copy and clear all execute the same code the standalone server does — the duplication that motivated `buffer_ops.hpp` is now actually gone rather than merely shareable. Clears no longer allocate a zero-filled array the size of the region to pass in, and the arithmetic releases the GIL while it runs. Behaviour is unchanged: the numpy implementation was moved into `tests/test_buffer_ops.py` as an independent reference the binding is checked against, since a `softcut.norns` that delegates can no longer serve as one.

- **numpy is now optional.** `dependencies` is empty; `pip install softcut-py[numpy]` adds it. The library imports and runs without it: buffers are `array.array("f")`, and every entry point takes any C-contiguous float32 buffer, so numpy arrays keep working exactly where they did. numpy is needed for one thing only — `Engine.render` and `Voice.process` allocating their `(n, out_channels)` ndarray result — and both now take `out=`, a flat buffer of `n * out_channels` samples written in place and returned, which avoids it entirely. Calling `render()` with neither says so rather than failing in nanobind.

  Two visible consequences: `Engine.allocate` and `NornsSoftcut.buffers` hand back `array.array("f")` rather than ndarrays. `numpy.asarray` wraps one without copying, which is how the tests and `demos/_util.py` keep their numpy idioms.

- `softcut._wavio` no longer uses numpy. The stdlib `wave` module still parses the container; the per-sample conversion moved to C (`_core._pcm_decode` for 8/16/24/32-bit integer PCM, `_core._pcm_encode_s16` for writing), which is the only part of WAV I/O that is per-sample work. No WAV decoder is vendored into the extension — dr_wav stays with the standalone server, which already has it. `read_wav` now returns `(data, channels, sample_rate)` with the samples interleaved rather than a `(frames, channels)` array, since there is no 2-D without numpy; callers pull a channel out with `_core._buffer_extract_channel`, which is what the standalone does with the same audio. `write_wav` takes any C-contiguous float32 buffer and still infers the channel count from a 2-D shape, so numpy callers are unaffected, and it accepts an explicit `channels` for a flat interleaved buffer. `read_wav`/`read_wav_mono` hand back `array.array("f")`; `numpy.asarray` wraps one without copying.

- `scsh::apply_to_buffer` takes a separate unfaded path. With no edge crossfade the envelope is 1 throughout, which collapses the blend to the target, so the branchy per-sample envelope is lifted out of the loop and a plain clear becomes a `std::fill`. Whole-buffer clears went from ~17 ms to ~1.9 ms for `2**24` frames (numpy's memset is ~2.4 ms); reads and copies without a fade benefit equally. Both hosts get it.

### Fixed

- A further vendored `softcut-lib` host-portability fix, of the same family as 0.2.0's: uninitialized phase state on `Voice`. `Voice::phaseQuant`, `Voice::rawPhase` and `Voice::quantPhase` had no initializer and `Voice::reset()` did not set them — softcut assumes zero-initialized static storage, which a heap-allocated voice on a host does not get. A fresh voice therefore reported whatever the recycled allocation held, and `updateQuantPhase()` took its quantizing branch and divided by a garbage quantum. Reaching a host, that is a nonsense playhead position from `Voice.quant_phase` and `/poll/softcut/phase`, and a value beyond float range kills the python-osc phase-poll thread outright (`OverflowError` in `struct.pack`). `reset()` now restores the phase state too, so a reset voice reports where it is rather than where it was, and the defaults the extension advertises for `phase_quant`/`phase_offset` are the ones `reset()` actually establishes. Affects the standalone `clients/softcut-osc` binary equally; rebuild it with `make build-standalone` to pick the fix up.

- The Python phase poll (`softcut.osc._PhasePoll`) no longer dies on a failed scan. Nothing restarts its thread, so one exception escaping the loop ended phase reporting for the life of the process; failures are now logged once with a traceback, counted on `errors`, and the scan continues. A voice's last-reported phase is recorded only after its send succeeds, so a failed scan retries rather than dropping the update it never managed to report. `poll_once` still raises, for callers driving it directly. (The native backend's poll runs in C and is unaffected.)

## [0.3.0]

### Added

- GIL-free native OSC fast path. Per-voice `/set/param/cut/*` messages are now parsed and applied entirely in C on the native receiver's thread — no GIL, no Python callback — by posting to a second per-producer SPSC command queue that the audio thread drains alongside the Python one (each queue stays strictly single-producer). The 26 float + 4 bool `Voice` mirror fields became `std::atomic` so Python getters stay coherent with natively-dispatched writes; non-param addresses still fall back to the Python callback. This reverses the 0.2.0 "dispatch under the GIL" design for the hot path.

- GIL-free native phase poll (`_core._OscPhasePoll`): the outbound `/poll/softcut/phase` reporter runs its scan-and-send loop in C, replacing the Python `_PhasePoll` thread for the native backend. With inbound dispatch and the outbound poll both in C, nothing on the native control path touches the GIL, so a busy interpreter can neither delay it nor be delayed by it.

- `benchmarks/osc_jitter.py`: measures p99 control-latency jitter under GIL contention, with a compile-time apply-latency probe (`SOFTCUT_ENABLE_BENCH_PROBE`, `make build-bench`) that timestamps the apply instant in C to isolate dispatch latency from the GIL-gated Python observer. Under load, native's dispatch tail stays roughly flat (p99 ~0.08 ms) while python-osc's blows out (~7.6 ms).

- `clients/softcut-osc`: a standalone, no-Python softcut OSC server binary (softcut-lib + tinyosc + miniaudio) that speaks the full softcut wire protocol with no interpreter, so nothing on its control path touches a GIL. Params, mix, feedback, routing, `voice_sync`, buffer clear, phase poll and lifecycle dispatch in C++; WAV disk I/O is backed by the vendored dr_wav on a dedicated disk-worker thread with click-avoidance edge crossfades (`--crossfade-ms`), `preserve`/`mix` blending, and opt-in `--resample-on-read` (miniaudio's `ma_resampler`; off by default so reads stay frame-for-frame like softcut/norns). Adds device selection (`--output-device`/`--input-device`/ `--list-devices`) and a `--null` headless backend. Build with `make build-standalone`, exercise with `make test-standalone`.

- `src/shared`: a Python-free C++ core (namespace `scsh`) shared by the extension and the standalone server — the SPSC `CommandQueue`, UDP socket helpers, the multi-voice mixer (`VoiceMix` + `process_block`), and miniaudio device configuration — so the mixer/device/socket code is written once.

- `thirdparty/dr_wav`: vendored dr_wav (public domain / MIT-0), used only by the standalone server for WAV read/write (the vendored miniaudio is trimmed with no codec). The Python extension keeps its numpy + stdlib `wave` path.

- `docs/dev/tinypsc-opt.md`: design note on the native OSC performance levers (transport-bound vs dispatch-bound, block quantization, GIL decoupling), recording the implemented fast path and its measured payoff.

### Changed

- The native OSC transport is now a GIL-free fast path for `/set/param/cut/*` dispatch rather than dispatching under the GIL as in 0.2.0; `benchmarks/osc_jitter.py` supersedes `benchmarks/osc_dispatch.py`'s hot-path conclusion for those messages.

- `_core._OscReceiver` takes the low-level engine as an optional final argument (for the C fast path); the phase-poll test uses a backend-agnostic `reset()` so it drives either the Python `_PhasePoll` or the native `_OscPhasePoll`.

## [0.2.0]

### Added

- OSC server (`softcut.osc`) exposing softcut over the monome softcut wire protocol, so norns/Lua scripts, SuperCollider, Max, or any OSC controller can drive softcut-py as a drop-in engine. It is a thin dispatch layer over the `softcut.norns` host: the full reference address namespace (`/set/param/cut/*`, routing, `/softcut/buffer/*`, `/softcut/reset`) maps to host methods, with the 0-based wire protocol translated to the host's 1-based API. A background phase
  poll (`/poll/start|stop/cut/phase`) reports quantized playhead position back as
  `/poll/softcut/phase <voice> <phase>`. Run with `python -m softcut.osc` (defaults: listen UDP 9999, reply 127.0.0.1:57120, matching the reference). Partial/known gaps mirror the norns layer: `enabled` maps to play, `in_cut` uses the scalar input gain (no per-channel ADC matrix), level/pan slew and the VU poll are accepted-and-ignored.

- Two OSC transports behind one dispatch table, selected by `backend=` ("auto"/"python-osc"/"native"):

  - `python-osc` as an optional extra (`pip install softcut-py[osc]`); the core stays numpy-only. The default when the native transport is not built.

  - An **experimental**, dependency-free `native` transport (UDP socket + the vendored tinyosc codec, exposed as `_core._OscReceiver`/`_core._OscSender`), compiled in with the CMake option `SOFTCUT_ENABLE_TINYOSC` and reported by `softcut._core.HAVE_TINYOSC`. It is **not** enabled in the published wheels; opt in with a source build. Receiving and parsing run in C; dispatch runs under the GIL because the DSP command queue is single-producer, so it is a dependency-free transport rather than a GIL-free fast path. IPv4-only and less battle-tested than python-osc.

- OSC server documentation: a `docs/guide/osc.md` guide (running a server, the two transports, the complete address table, the phase poll, and known partials) plus an OSC section in the README.

- `benchmarks/osc_dispatch.py`: a micro-benchmark comparing python-osc dispatch, native (C) dispatch, and the UDP transport floor. It quantifies why the native transport dispatches under the GIL rather than via a GIL-free fast path: OSC is transport-bound (a socket syscall dominates each message), so native dispatch — ~18x faster in isolation — saves only about 7% of one `recvfrom`. The command queue is kept single-producer accordingly.

### Changed

- CI gains a `native-osc` matrix leg (Linux/macOS/Windows) that builds with `SOFTCUT_ENABLE_TINYOSC=ON` and runs the suite against both OSC transports, asserting the native transport actually compiled in. A `make build-tinyosc` target builds the extension with the native transport enabled.

### Fixed

- `make clean` no longer deletes compiled extensions inside `.venv`: its `find . -name "*.so"` ran from the repo root and removed dependency `.so` files (e.g. numpy's), breaking the environment. It now prunes `.venv` and `.git`.

- `make build` / `make build-tinyosc` use the correct distribution name (`softcut-py`) for `--reinstall-package`, so a rebuild is actually forced after C/C++ changes instead of silently reusing a cached build.

## [0.1.1]

### Added

- norns-compatible API layer (`softcut.norns`, import as `from softcut import norns as softcut`): the flat, 1-based, singleton norns `softcut` namespace (6 voices, 2 global mono buffers) over the object core. Implements Tier A (attribute passthrough: rate, level, pan, play/rec/loop, loop points, filters, slews, phase, `position`, `buffer`, `voice_sync`, `level_cut_cut`, `reset`) and Tier B (buffer/disk ops in numpy plus the stdlib `wave` module, no new dependency: `buffer_read_*`/`buffer_write_*`, `buffer_copy_*`, `buffer_clear*`, with preserve/mix crossfade, edge `fade_time` and `reverse`). Buffer ops write in place and never reallocate, so they are safe against a running audio thread; reads are non-resampling (file-rate), matching norns. Phase polling (Tier C) and the slew/routing gaps needing core changes (Tier D) are not included; see `docs/dev/norns-api.md`.

- `softcut._wavio`: stdlib-`wave` WAV codec (`read_wav`, `read_wav_mono`, `write_wav`) shared by the norns layer and the demos; the demo `_util` helpers now delegate to it.

- Demo `12_norns_api.py`: a narrated progression (forward loop, low-pass, octave-down, reverse, stereo, reversed buffer copy) driven exclusively through the norns API, each feature separated by a second of silence. Renders offline or performs live with `--play`.

- Voice-to-voice feedback routing: `Engine.feedback(src, dst, amount)` mixes one voice's output into another's input (one block delayed; `src == dst` is a self-feedback delay line), plus a per-voice `input_gain` for the engine's external input.

- Lock-free single-producer/single-consumer command queue: while the device is running, voice DSP parameter changes from Python are applied on the audio thread instead of racing it. Setters apply directly when no engine is running.

- Device selection: `softcut.list_devices()` enumerates the system audio devices, and `Engine(output_device=..., input_device=...)` selects one by index (`-1` = system default).

- Release engineering: GitHub Actions CI (QA plus a Linux/macOS/Windows build smoke matrix) and a tag-triggered `cibuildwheel` workflow that builds wheels for CPython 3.10-3.14 across Linux (x86_64/aarch64), macOS (x86_64/arm64) and Windows, plus the sdist, and publishes to PyPI via trusted publishing.

### Changed

- CMake links `ole32` on Windows (COM, used by miniaudio's WASAPI backend).

- CMake defines `_USE_MATH_DEFINES` under MSVC so `M_PI`/`M_PI_2` (used by the vendored DSP sources) are visible from `<cmath>`, fixing the Windows build.

- Wheels: cibuildwheel sets `MACOSX_DEPLOYMENT_TARGET=10.14` so nanobind's C++17 aligned new/delete compiles (the default x86_64 target of 10.9 fails), and the no-op `pp*` skip selector was dropped (PyPy is not enabled).

### Fixed

- Uninitialized DSP state in the vendored softcut-lib that produced nondeterministic `NaN`/denormal output (heap-garbage dependent, so it surfaced intermittently in CI):

  - `Svf`: `reset()` ran `setFc()`/`setRq()` before `setSampleRate()`, clamping the corner frequency against uninitialized bounds and computing coefficients from an uninitialized `pi_sr`; the corrupted `fc` was never re-clamped, yielding an unstable filter whose state diverged to `inf` and, via a zeroed output mix, `NaN`. The constructor now seeds self-consistent state.

  - `Resampler`: the `inBuf_`/`outBuf_` interpolation buffers were never zeroed (`reset()` is not called by the host), so the first recording `poke()` interpolated over uninitialized history and wrote garbage into the buffer. The constructor now zeroes them.

  - `ReadWriteHead`: added default member initializers (notably `buf`, `sr`, `loopFlag`, `pre`, `rec`, which `init()` does not set) so the head is well-defined regardless of setter call order.

## [0.1.0]

### Added

- `Voice`: nanobind binding of the complete `softcut::Voice` per-voice DSP engine. Property-style parameters (rate, loop, record/play, fades, slews, pre/post state-variable filters, phase quant/offset, rec offset), numpy `float32` buffers with power-of-two enforcement, and `process()` for offline mono block processing. Buffers are caller-owned and can be shared between voices.

- `Engine`: multi-voice realtime host over a miniaudio device. Context manager (entering starts the device, exiting stops it), sequence protocol over its voices, `allocate()`, `sync()`, and an offline `render()` that shares the per-block path with the GIL-free audio callback. `duplex` (live mic in) and `playback` modes, with per-voice `level`/`pan` mixing to stereo.

- Pythonic sugar on `Voice`: `configure(**params)`, `loop_region`, the non-blocking `record()` context manager, blocking `record_for()`, and a stateful `__repr__`.

- `next_power_of_two()` helper and a `_core.pyi` type stub; `py.typed` shipped.

- Demos (`demos/`): varispeed, loop points, overdub, stereo layering, filter sweep, live mic looper, Frippertronics-style overdub/replace/decay, tape-stop rate slew, pre/post filters, phase sync, and capture modes. `make demos` plays the offline demos in sequence; `make demo-looper` runs the interactive one. Audio I/O uses only the standard library plus numpy.

### Changed

- Replaced the scaffold `add`/`greet` example module with the softcut API.

- `numpy` is now a runtime dependency.

- sdist force-includes the native build inputs (softcut-lib, miniaudio) so it always builds, and excludes the JACK/OSC client and demo audio fixtures.

- `Softcut` is retained as a deprecated alias for `Engine`.

### Fixed

- Vendored `softcut-lib` host-portability fixes (it relied on zero-initialized static storage that does not exist for a host-allocated `Voice`):

  - `FadeCurves`: default-initialize the window-ratio members read by `calcPreFade()`/`calcRecFade()` before `init()` assigns them (a garbage ratio overran a stack buffer).

  - `SubHead`: default-initialize `wrIdx_`/`active_` and related members so recording does not index the buffer out of bounds.

  - `TestBuffers`: stub out the 3 MB Matlab-dump buffer, shrinking `sizeof(Voice)` from ~3.15 MB to ~9 KB.
