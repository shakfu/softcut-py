# Live looping

The engine opens a real audio device and runs on a background thread, so it is built for dynamic, interactive use — set a parameter and you hear the change on the next block.

## Nothing blocks

`eng.start()` spawns miniaudio's realtime thread and returns immediately. From then on, every voice with `play`/`rec` set is processed continuously; your Python thread is free:

```python
import softcut, time

eng = softcut.Engine(voices=1)     # duplex: mic in, speakers out
eng.allocate(seconds=4)
eng.start()                        # returns instantly; audio runs in background

eng[0].configure(loop_region=(0, 4), rate=1.0)
eng[0].rec = True                  # records the mic on the audio thread
# ... the REPL is responsive here; eng[0].position updates live ...
eng[0].rec = False                 # keeps looping what it captured

eng.stop()
```

Use it as a **context manager** to handle the device lifecycle:

```python
with softcut.Engine(voices=2) as eng:   # start() on enter, stop() on exit
    ...
```

## The capture gesture

`Voice.record()` is a context manager over the rec toggle — non-blocking. On entry it (optionally) cuts to a position and turns play and rec on; on exit it turns rec off so the voice keeps looping:

```python
with eng[0].record(at=0):    # rec + play on, head cut to 0s
    time.sleep(4)            # let it capture 4s; the REPL thread just waits
# rec is now off; the loop plays back
```

The `time.sleep(4)` is **you choosing how long to capture** — recording happens on the audio thread regardless. You could run other commands instead.

## Blocking capture

When you genuinely want "record exactly this long, then continue", use `record_for`, which blocks the calling thread for the duration:

```python
eng[0].record_for(4, at=0)   # rec on -> wait 4s -> rec off; returns the voice
```

## Discoverability

Voices and the engine have informative reprs, so they read well in a REPL:

```python
>>> eng[0]
Voice(rate=1, loop=[0, 4], rec=False, play=True, level=0.8, pan=-0.3, pos=1.230)
>>> eng
Engine(voices=2, sr=48000, mode='duplex', block_size=512, running=True)
```

!!! tip "Setting parameters live is safe" While the device runs, parameter changes are applied on the audio thread via a lock-free queue rather than racing it. See [Realtime parameters](realtime.md).
