# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
