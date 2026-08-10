# softcut

Python bindings for [softcut-lib](https://github.com/monome/softcut-lib) — the per-voice DSP engine behind monome norns' softcut — with realtime audio I/O via [miniaudio](https://github.com/mackron/miniaudio). Built with [nanobind](https://github.com/wjakob/nanobind).

This is **not** a port of the norns Lua API. It exposes softcut as Python objects: a [`Voice`](api.md#voice) is one DSP unit, and an [`Engine`](api.md#engine) hosts several voices over an audio device.

## Quick look

=== "Live looping"

    ```python
    import softcut, time

    with softcut.Engine(voices=2) as eng:      # opens the audio device
        eng.allocate(seconds=8)                # shared power-of-two buffer
        eng[0].configure(loop_region=(0, 4), rate=1.0, level=0.8, pan=-0.3)

        with eng[0].record(at=0):              # rec + play on; capture 4s of mic
            time.sleep(4)
        # on exit: rec off, the voice keeps looping what it captured

        time.sleep(8)
    # device closed automatically
    ```

=== "Offline rendering"

    ```python
    import array, softcut

    eng = softcut.Engine(voices=1, mode="playback")
    eng.allocate(seconds=2)
    eng[0].configure(loop_region=(0, 1), rate=1.0, rec_level=1.0)
    eng[0].rec = eng[0].play = True
    eng[0].cut_to(0)

    # two laps: the first records the input, the second plays it back
    eng.render_to("out.wav", array.array("f", [0.3] * 96000))
    ```

## Highlights

- The complete `softcut::Voice` parameter set as plain attributes: rate, loop, record/play, fades, slews, pre/post filters, phase quant, position read-out.

- A realtime [`Engine`](api.md#engine): a context manager and a sequence of voices, running live (mic/speakers on a background thread) or offline.

- [Voice-to-voice feedback](guide/routing.md) and per-voice level/pan/input gain.

- [Safe realtime parameter updates](guide/realtime.md) via a lock-free queue.

- [Device selection](guide/devices.md) and a numpy-native buffer model.

- Port norns scripts through the [norns-compatible layer](guide/norns.md), which mirrors the flat Lua API.
- Drive it from any controller over the [OSC server](guide/osc.md) — a dependency-free GIL-free native transport built in by default, and a standalone, no-Python OSC server binary (`make build-standalone`).

Start with [Installation](installation.md) and [Concepts](concepts.md).
