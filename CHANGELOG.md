# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-04

### Added

- OSC server (`softcut.osc`) exposing softcut over the monome softcut wire
  protocol, so norns/Lua scripts, SuperCollider, Max, or any OSC controller can
  drive softcut-py as a drop-in engine. It is a thin dispatch layer over the
  `softcut.norns` host: the full reference address namespace (`/set/param/cut/*`,
  routing, `/softcut/buffer/*`, `/softcut/reset`) maps to host methods, with the
  0-based wire protocol translated to the host's 1-based API. A background phase
  poll (`/poll/start|stop/cut/phase`) reports quantized playhead position back as
  `/poll/softcut/phase <voice> <phase>`. Run with `python -m softcut.osc`
  (defaults: listen UDP 9999, reply 127.0.0.1:57120, matching the reference).
  Partial/known gaps mirror the norns layer: `enabled` maps to play, `in_cut`
  uses the scalar input gain (no per-channel ADC matrix), level/pan slew and the
  VU poll are accepted-and-ignored.

- Two OSC transports behind one dispatch table, selected by `backend=`
  ("auto"/"python-osc"/"native"):
  - `python-osc` as an optional extra (`pip install softcut-py[osc]`); the core
    stays numpy-only. The default when the native transport is not built.
  - An **experimental**, dependency-free `native` transport (UDP socket + the
    vendored tinyosc codec, exposed as `_core._OscReceiver`/`_core._OscSender`),
    compiled in with the CMake option `SOFTCUT_ENABLE_TINYOSC` and reported by
    `softcut._core.HAVE_TINYOSC`. It is **not** enabled in the published wheels;
    opt in with a source build. Receiving and parsing run in C; dispatch runs
    under the GIL because the DSP command queue is single-producer, so it is a
    dependency-free transport rather than a GIL-free fast path. IPv4-only and
    less battle-tested than python-osc.

- OSC server documentation: a `docs/guide/osc.md` guide (running a server, the
  two transports, the complete address table, the phase poll, and known
  partials) plus an OSC section in the README.

- `benchmarks/osc_dispatch.py`: a micro-benchmark comparing python-osc dispatch,
  native (C) dispatch, and the UDP transport floor. It quantifies why the native
  transport dispatches under the GIL rather than via a GIL-free fast path: OSC is
  transport-bound (a socket syscall dominates each message), so native dispatch —
  ~18x faster in isolation — saves only about 7% of one `recvfrom`. The command
  queue is kept single-producer accordingly.

### Changed

- CI gains a `native-osc` matrix leg (Linux/macOS/Windows) that builds with
  `SOFTCUT_ENABLE_TINYOSC=ON` and runs the suite against both OSC transports,
  asserting the native transport actually compiled in. A `make build-tinyosc`
  target builds the extension with the native transport enabled.

### Fixed

- `make clean` no longer deletes compiled extensions inside `.venv`: its
  `find . -name "*.so"` ran from the repo root and removed dependency `.so`
  files (e.g. numpy's), breaking the environment. It now prunes `.venv` and
  `.git`.

- `make build` / `make build-tinyosc` use the correct distribution name
  (`softcut-py`) for `--reinstall-package`, so a rebuild is actually forced after
  C/C++ changes instead of silently reusing a cached build.

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
