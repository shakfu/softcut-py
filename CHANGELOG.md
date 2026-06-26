# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Voice-to-voice feedback routing: `Engine.feedback(src, dst, amount)` mixes one
  voice's output into another's input (one block delayed; `src == dst` is a
  self-feedback delay line), plus a per-voice `input_gain` for the engine's
  external input.
- Lock-free single-producer/single-consumer command queue: while the device is
  running, voice DSP parameter changes from Python are applied on the audio
  thread instead of racing it. Setters apply directly when no engine is running.
- Device selection: `softcut.list_devices()` enumerates the system audio devices,
  and `Engine(output_device=..., input_device=...)` selects one by index
  (`-1` = system default).
- Release engineering: GitHub Actions CI (QA plus a Linux/macOS/Windows build
  smoke matrix) and a tag-triggered `cibuildwheel` workflow that builds wheels
  for CPython 3.10-3.14 across Linux (x86_64/aarch64), macOS (x86_64/arm64) and
  Windows, plus the sdist, and publishes to PyPI via trusted publishing.

### Changed
- CMake links `ole32` on Windows (COM, used by miniaudio's WASAPI backend).
- CMake defines `_USE_MATH_DEFINES` under MSVC so `M_PI`/`M_PI_2` (used by the
  vendored DSP sources) are visible from `<cmath>`, fixing the Windows build.
- Wheels: cibuildwheel sets `MACOSX_DEPLOYMENT_TARGET=10.14` so nanobind's C++17
  aligned new/delete compiles (the default x86_64 target of 10.9 fails), and the
  no-op `pp*` skip selector was dropped (PyPy is not enabled).

### Fixed
- Uninitialized DSP state in the vendored softcut-lib that produced
  nondeterministic `NaN`/denormal output (heap-garbage dependent, so it surfaced
  intermittently in CI):
  - `Svf`: `reset()` ran `setFc()`/`setRq()` before `setSampleRate()`, clamping
    the corner frequency against uninitialized bounds and computing coefficients
    from an uninitialized `pi_sr`; the corrupted `fc` was never re-clamped,
    yielding an unstable filter whose state diverged to `inf` and, via a zeroed
    output mix, `NaN`. The constructor now seeds self-consistent state.
  - `Resampler`: the `inBuf_`/`outBuf_` interpolation buffers were never zeroed
    (`reset()` is not called by the host), so the first recording `poke()`
    interpolated over uninitialized history and wrote garbage into the buffer.
    The constructor now zeroes them.
  - `ReadWriteHead`: added default member initializers (notably `buf`, `sr`,
    `loopFlag`, `pre`, `rec`, which `init()` does not set) so the head is
    well-defined regardless of setter call order.

## [0.1.0] - 2026-06-26

### Added
- `Voice`: nanobind binding of the complete `softcut::Voice` per-voice DSP
  engine. Property-style parameters (rate, loop, record/play, fades, slews,
  pre/post state-variable filters, phase quant/offset, rec offset), numpy
  `float32` buffers with power-of-two enforcement, and `process()` for offline
  mono block processing. Buffers are caller-owned and can be shared between
  voices.
- `Engine`: multi-voice realtime host over a miniaudio device. Context manager
  (entering starts the device, exiting stops it), sequence protocol over its
  voices, `allocate()`, `sync()`, and an offline `render()` that shares the
  per-block path with the GIL-free audio callback. `duplex` (live mic in) and
  `playback` modes, with per-voice `level`/`pan` mixing to stereo.
- Pythonic sugar on `Voice`: `configure(**params)`, `loop_region`, the
  non-blocking `record()` context manager, blocking `record_for()`, and a
  stateful `__repr__`.
- `next_power_of_two()` helper and a `_core.pyi` type stub; `py.typed` shipped.
- Demos (`demos/`): varispeed, loop points, overdub, stereo layering, filter
  sweep, live mic looper, Frippertronics-style overdub/replace/decay, tape-stop
  rate slew, pre/post filters, phase sync, and capture modes. `make demos`
  plays the offline demos in sequence; `make demo-looper` runs the interactive
  one. Audio I/O uses only the standard library plus numpy.

### Changed
- Replaced the scaffold `add`/`greet` example module with the softcut API.
- `numpy` is now a runtime dependency.
- sdist force-includes the native build inputs (softcut-lib, miniaudio) so it
  always builds, and excludes the JACK/OSC client and demo audio fixtures.
- `Softcut` is retained as a deprecated alias for `Engine`.

### Fixed
- Vendored `softcut-lib` host-portability fixes (it relied on zero-initialized
  static storage that does not exist for a host-allocated `Voice`):
  - `FadeCurves`: default-initialize the window-ratio members read by
    `calcPreFade()`/`calcRecFade()` before `init()` assigns them (a garbage
    ratio overran a stack buffer).
  - `SubHead`: default-initialize `wrIdx_`/`active_` and related members so
    recording does not index the buffer out of bounds.
  - `TestBuffers`: stub out the 3 MB Matlab-dump buffer, shrinking
    `sizeof(Voice)` from ~3.15 MB to ~9 KB.
