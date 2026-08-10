# Offline rendering

`Engine.render()` processes audio without a device: it takes a mono block, runs it through all voices, and returns the mixed stereo output. It is synchronous and deterministic — ideal for batch processing and tests.

Output frames are interleaved in a flat `array.array("f")` of `n * out_channels` samples; `numpy.asarray(out).reshape(-1, eng.out_channels)` is the 2-D view, taken without copying. Pass your own buffer as `out=` to fill it in place and skip the allocation — useful in a loop, and it may be an ndarray.

```python
import numpy as np, softcut

eng = softcut.Engine(voices=1, mode="playback")
v = eng[0]
v.buffer = np.zeros(2**16, dtype=np.float32)
v.configure(loop_region=(0, 1), rate=1.0, rec_level=1.0)
v.rec = v.play = True
v.cut_to(0)

block = np.random.randn(48000).astype(np.float32)   # 1s of mono input
out = eng.render(block)                              # shape (48000, 2), float32
```

The input is always the array you pass (the `mode` only affects the live device). Voice head positions **persist across calls**, so consecutive renders concatenate into continuous audio — handy for automating a parameter between chunks:

```python
chunks = []
for fc in range(500, 8000, 250):
    v.post_filter_fc = fc          # change a parameter ...
    chunks.append(eng.render(silence(0.1)))   # ... and render the next slice
sweep = np.concatenate(chunks)     # one continuous filter sweep
```

!!! note Do not call `render()` while the device is running — it raises. Stop the device first, or use the live path.

## A single voice

For one mono voice you can skip the engine and call `Voice.process()` directly; it returns a mono array:

```python
v = softcut.Voice(48000)
v.buffer = np.zeros(2**16, dtype=np.float32)
v.configure(loop_region=(0, 1))
v.play = True
v.cut_to(0)
mono_out = v.process(np.zeros(1024, dtype=np.float32))   # shape (1024,)
```

## Audio files

Buffers are plain float32 buffers, so use any library to read and write audio. The demos use only the standard library `wave` module; `soundfile` is a richer option:

```python
import soundfile as sf
data, sr = sf.read("loop.wav", dtype="float32")   # mono or (n, channels)
v.buffer = to_power_of_two(data.mean(axis=1) if data.ndim > 1 else data)
sf.write("out.wav", eng.render(block), sr)
```
