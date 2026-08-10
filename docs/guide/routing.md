# Routing & feedback

The examples below share one engine:

```python
import softcut

eng = softcut.Engine(voices=2, mode="playback")
eng.allocate(seconds=4)
```

## Output mix: level and pan

Each voice mixes to the stereo output through its `level` (linear gain) and `pan` (`-1` left, `0` center, `+1` right, equal-power):

```python
eng[0].level = 0.8
eng[0].pan = -0.3
```

These are engine-mix parameters, not softcut DSP — they are ignored by a standalone `Voice.process()`.

## External input: input_gain

In duplex mode the engine feeds its external (mic) input to every voice. Each voice scales that input with `input_gain`:

```python
eng[1].input_gain = 0.0   # voice 1 ignores the mic
```

A voice only writes input into its buffer when it is recording, so `input_gain` matters for recording voices.

## Voice-to-voice feedback

`Engine.feedback(src, dst, amount)` routes one voice's **output** into another voice's **input**, mixed in one block late:

```python
eng.feedback(0, 1, 0.4)        # voice 0 -> voice 1 input, at 0.4 gain
amount = eng.feedback(0, 1)    # omit amount to read it back
```

What the destination does with that input depends on the destination: a recording voice writes it into its buffer; a playing-only voice ignores it (softcut output is the buffer read). So feedback is typically used to record one voice's output into another's loop.

`src == dst` is **self-feedback** — a one-block delay line / regeneration:

```python
eng.feedback(0, 0, 0.5)   # voice 0 feeds itself: a short feedback delay
```

!!! note "One-block delay" Feedback uses the previous block's output (softcut processes per block, so a sample-accurate cross-feedback loop is not possible). The delay is one processing block, so it scales with `block_size`.

## Putting it together

```python
eng = softcut.Engine(voices=2, sample_rate=48000)
eng.allocate(seconds=4)
eng[0].configure(loop_region=(0, 4), pan=-0.5)
eng[1].configure(loop_region=(0, 4), pan=0.5, rec=True, play=True)
eng[1].cut_to(0)
eng.feedback(0, 1, 0.6)   # voice 0's output is recorded into voice 1
```
