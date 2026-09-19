# Realtime parameters

Audio runs on miniaudio's realtime thread, separate from the Python thread that sets parameters. softcut makes concurrent parameter changes safe without you having to think about it.

## How it works

While the device is **running**, a voice's DSP parameter change from Python is not applied directly. Instead it is pushed onto a lock-free, single-producer / single-consumer queue and applied on the audio thread at the start of the next block. The audio thread therefore never reads a half-written parameter.

- The commands are tiny (a voice pointer plus one scalar) and live in `std::function`'s small-buffer storage, so enqueue/apply never touch the heap — there is no allocation on the audio thread.

- When **no engine is running** (offline use, or before `start()`), setters apply immediately.

- `stop()` drains any commands still queued, so the final state is consistent.

"Running" means the audio callback can be live, not merely that `start()` has returned: the flag goes up before the device starts and comes down only once `ma_device_stop()` has returned, and the check that reads it is held against a concurrent `start()`/`stop()`. A setter can therefore never take the direct path into state a callback is reading.

Pointing a voice at a *different array* is the one change whose safety depends on what changes. A different **length** raises while running: `ReadWriteHead::setBuffer` stores the pointer and the frame count separately, once per subhead, so the callback can read a new count against an old pointer — and it pokes as well as peeks, making that an out-of-bounds write with `rec` on. Stop the engine to reallocate, or allocate once at the longest length you need and move `loop_start`/`loop_end` live.

The **same** length is accepted and queued, so both subheads move together at a block boundary. This is the norns operation: `softcut.buffer(voice, b)` switches a voice between the two equal global buffers, live, and is reachable over OSC and from a TouchOSC button. The array it displaces is held one generation longer, so a caller that drops its own reference does not free memory a queued swap has not consumed yet.

This covers the softcut DSP parameters (rate, loop, record/play, fades, slews, filters, phase, `sample_rate`, `cut_to`, `stop`, `reset`). The engine-mix scalars (`level`, `pan`, `input_gain`) and the feedback matrix are relaxed atomics rather than queued — a concurrent read is at worst stale by one block, which is inaudible, and being atomic makes that a defined outcome rather than a data race.

If the queue fills — which needs more than 4096 changes inside one block period, roughly 380,000 per second at 512 frames / 48 kHz — the setter waits for the audio thread's next drain rather than applying the change itself. Applying it there would mutate state the audio thread is reading, which is the race the queue exists to prevent. A change that still cannot be queued after about two block periods is dropped and counted on `Voice.dropped_commands`; anything nonzero there means control changes were lost.

```python
import softcut, time

with softcut.Engine(voices=1) as eng:
    eng.allocate(seconds=4)
    eng[0].configure(loop_region=(0, 4))
    eng[0].play = True
    eng[0].cut_to(0)
    # set freely from the control thread while audio runs:
    for r in (1.0, 1.5, 0.5, 2.0):
        eng[0].rate = r        # enqueued, applied on the audio thread
        time.sleep(0.5)
```

!!! warning "Single producer" The queue assumes a single producer (the GIL-holding Python thread). This is why free-threaded (`cp31Xt`) wheels are intentionally not built — multiple Python threads setting parameters concurrently would violate that assumption. Drive parameters from one thread. (The optional native OSC transport does not break this: its GIL-free receiver posts to its *own* separate single-producer queue, which the audio thread drains alongside this one.)

## Reading state back

Reading a parameter returns the last value you set (the Python-side mirror), even if the underlying DSP change is still one block away. Read-only head state is always live: `position` (audio-thread view), `saved_position` (updated once per block, safe from any thread) and `quant_phase`.
