# Offline rendering

`Engine.render()` processes audio without a device: it takes a mono block, runs it through all voices, and returns the mixed stereo output. It is synchronous and deterministic — ideal for batch processing and tests.

`Engine.render_to(path, input)` is the short way to end one: `render` followed by
`write_wav`, taking the sample rate and channel count from the engine rather
than from you.

Both `render` and `render_to` take either `seconds`, which feeds the voices that
much silence, or an `input` buffer -- the engine's mono input, which voices with
`rec` on record. `seconds` is what most offline work wants: the voices are
playing material already in their buffers and there is nothing to record, so a
zeroed input buffer is ceremony.

```python
import array, softcut

eng = softcut.Engine(voices=1, mode="playback")
v = eng[0]
v.buffer = softcut.read_wav_mono("loop.wav")[0][: 2**16]
v.configure(loop_region=(0, 1), rate=1.0)
v.play = True
v.cut_to(0)

eng.render_to("out.wav", seconds=4)          # returns the path written
out = eng.render(seconds=4)                  # or keep the samples
```

`render` hands back interleaved frames in a flat `array.array("f")` of
`n * out_channels` samples. numpy is not needed for any of this, but if you have
it, `numpy.asarray(out).reshape(-1, eng.out_channels)` is the 2-D view, taken
without copying. Pass your own buffer as `out=` to fill it in place and skip the
allocation -- useful in a loop, and it may be an ndarray.

The input, when you supply one, is always the buffer you pass; `mode` only
affects the live device. Voice head positions **persist across calls**, so
consecutive renders concatenate into continuous audio -- handy for automating a
parameter between chunks:

```python
chunks = []
for fc in range(500, 8000, 250):
    v.post_filter_fc = fc                    # change a parameter ...
    chunks.append(eng.render(seconds=0.1))   # ... and render the next slice

sweep = array.array("f")                     # flat frames concatenate directly
for chunk in chunks:
    sweep.extend(chunk)
softcut.write_wav("sweep.wav", sweep, 48000, channels=eng.out_channels)
```

!!! note Do not call `render()` while the device is running — it raises. Stop the device first, or use the live path.

## A single voice

For one mono voice you can skip the engine and call `Voice.process()` directly; it returns a mono buffer of the same length:

```python
import array, softcut

v = softcut.Voice(48000)
v.buffer = array.array("f", bytes(4 * 2**16))
v.configure(loop_region=(0, 1))
v.play = True
v.cut_to(0)
mono_out = v.process(array.array("f", bytes(4 * 1024)))   # 1024 samples
```

## Audio files

`softcut.read_wav`, `read_wav_mono` and `write_wav` cover PCM WAV using only the
standard library, which is what the norns layer and the demos use:

```python
samples, sr = softcut.read_wav_mono("loop.wav")
buf = eng.allocate(frames=len(samples))      # rounded up to a power of two
buf[: len(samples)] = samples
```

For anything else -- FLAC, OGG, resampling -- use whatever you like. Note that
`render` returns *interleaved* frames, so a writer expecting 2-D needs the
reshape:

```python
import numpy as np, soundfile as sf

data, sr = sf.read("loop.wav", dtype="float32")           # mono or (n, channels)
mono = data.mean(axis=1) if data.ndim > 1 else data
v.buffer = np.resize(mono, softcut.next_power_of_two(len(mono)))

out = eng.render(seconds=4)
sf.write("out.wav", np.asarray(out).reshape(-1, eng.out_channels), sr)
```
