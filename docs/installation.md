# Installation

## From PyPI

```bash
pip install softcut-py
```

(The distribution is `softcut-py`; the import name is `softcut`.)

Wheels are published for CPython 3.10–3.14 on Linux (x86_64/aarch64), macOS (x86_64/arm64) and Windows.

There are **no dependencies**, numpy included. One optional extra is available:

```bash
pip install softcut-py[osc]     # the pure-Python OSC transport (python-osc)
```

Buffers are `array.array("f")` and every entry point takes any C-contiguous
float32 buffer, so numpy arrays work wherever you care to use them, and
`numpy.asarray` wraps what softcut returns without copying.

## From source

Building from source needs a C++17 compiler and CMake; everything else (nanobind, scikit-build-core) is fetched by the build. No audio backend development packages are required — miniaudio runtime-loads the platform audio libraries (ALSA/PulseAudio/JACK, WASAPI/DirectSound/WinMM, CoreAudio).

```bash
git clone --recurse-submodules https://github.com/shakfu/softcut-py
cd softcut-py
pip install .
```

### Developing

The project uses [uv](https://docs.astral.sh/uv/) and a `Makefile` wrapper:

```bash
make sync      # create the environment and build the extension
make test      # run the test suite
make qa        # test + lint (ruff) + typecheck (mypy) + format
make build     # rebuild the extension after editing C++/Python
```

Set `SOFTCUT_TEST_AUDIO=1` to additionally exercise a real audio device in the test suite (otherwise those tests skip).

## Optional: audio file I/O

softcut buffers are plain float32 buffers, so loading and saving audio is up to you. The standard library `wave` module works for PCM WAV (used by the demos); [`soundfile`](https://python-soundfile.readthedocs.io/) adds FLAC/OGG and more:

```bash
pip install soundfile
```
