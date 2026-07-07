# Realtime parameters

Audio runs on miniaudio's realtime thread, separate from the Python thread that sets parameters. softcut makes concurrent parameter changes safe without you having to think about it.

## How it works

While the device is **running**, a voice's DSP parameter change from Python is not applied directly. Instead it is pushed onto a lock-free, single-producer / single-consumer queue and applied on the audio thread at the start of the next block. The audio thread therefore never reads a half-written parameter.

- The commands are tiny (a voice pointer plus one scalar) and live in `std::function`'s small-buffer storage, so enqueue/apply never touch the heap — there is no allocation on the audio thread.

- When **no engine is running** (offline use, or before `start()`), setters apply immediately.

- `stop()` drains any commands still queued, so the final state is consistent.

This covers the softcut DSP parameters (rate, loop, record/play, fades, slews, filters, phase, `cut_to`, `stop`, `reset`). The engine-mix scalars (`level`, `pan`, `input_gain`) and the feedback matrix are plain aligned writes — a concurrent read is at worst stale by one block, which is inaudible.

```python
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
