# Design proposal: making the native (tinyosc) OSC backend maximally performant

Status: #4 (GIL-free hot path) implemented; #1-#3 still proposals.

See the "Implemented: GIL-free hot path" section below for what shipped.

## Scope

This analyses how far the experimental `native` OSC transport (vendored tinyosc + UDP, gated on `SOFTCUT_ENABLE_TINYOSC`) can be pushed, and which of those wins are worth building. It refers to the implementation in `src/softcut/_core.cpp` (`OscReceiver`, `OscSender`), the shared `CommandQueue` (now `src/shared/command_queue.hpp`), `src/softcut/osc.py` (`SoftcutOSC._dispatch`, `native_cb`), and the earlier `benchmarks/osc_dispatch.py`.

## First: pin down the metric

"Maximally performant" is ambiguous and splits into three metrics with different optima. The codebase has so far reasoned about only the first.

- **Throughput** (messages/second) - the benchmark's focus. Its conclusion holds: a message is transport-bound (one `recvfrom` syscall, ~0.5-1 us on loopback), not dispatch-bound, so moving dispatch into C saves a rounding error per message.

- **Control latency** (OSC-in to audible effect) - dominated by *block quantization*, not by anything in the OSC path. The DSP command queue drains once per audio block (`process_core`). At 48 kHz with a 128-frame block that is ~2.7 ms of unavoidable latency. A GIL acquire costs hundreds of nanoseconds - three to four orders of magnitude below that floor.

- **Latency jitter / robustness under burst** - the metric nobody has measured, and the only place the native path can actually win.

The existing "it is a transport, not a fast path" framing is therefore correct for throughput and latency. The block-quantization floor even strengthens it: shaving GIL time off dispatch is noise against 2.7 ms. If the goal is control latency, the lever is `block_size`, not OSC.

## The current native path is the worst of both worlds

Per datagram, the native receiver:

1. `recvfrom` - one syscall (the transport floor).

2. Parses in C with tinyosc (tens of nanoseconds).

3. Acquires the GIL (`deliver`, `_core.cpp`).

4. Builds an `nb::list`, appending each argument (Python object allocations).

5. Calls the Python callback, which does `list(args)` - a **second** copy (`native_cb`, `osc.py`).

6. Runs the full Python dispatch: `dict.get` + `_v_f` + `int()+1` / `float()` + host method + `dsp_apply` + queue push.

So it pays the C parse cost *plus* full Python dispatch cost *plus* per-message GIL churn. It is very likely slower per message than pure python-osc, while adding GIL-acquire traffic that contends with the phase-poll thread and the user control thread.

## Wins that remain, ordered by leverage

### 1. Protocol-level batching (highest leverage, zero core changes)

tinyosc already parses bundles (`OscReceiver::run` handles `tosc_isBundle`). If the *sender* (norns Lua / SuperCollider) packs many `/set/param/cut/*` messages into one OSC bundle, that is one datagram = one syscall = one GIL acquire for the whole batch. Strictly better than any receive-side micro-optimisation, and it needs only documentation plus a sender helper. For audio-rate parameter modulation this is the answer.

### 2. Batch the receive syscall

The floor is one syscall per datagram. On Linux, `recvmmsg()` drains N datagrams in one call. macOS and Windows lack it - emulate by making the socket non-blocking and draining in a loop (`recvfrom` until `EWOULDBLOCK`) after each wakeup. This attacks the only cost the benchmark says matters. Add it only if drops are actually observed.

### 3. Enlarge `SO_RCVBUF`

The receiver sets only `SO_REUSEADDR` and `SO_RCVTIMEO`. Default socket receive buffers overflow silently under burst and drop control messages. A larger `SO_RCVBUF` is a one-line robustness win.

### 4. Remove the double list and Python round-trip for the hot path

The `/set/param/cut/*` `if` messages are effectively all real-time traffic. Dispatch those entirely in C (address hash -> write mirror field + `dsp_apply`), with the GIL released, and fall back to the Python callback only for the rare buffer/lifecycle messages. This removes the per-message GIL acquire and both list allocations for the hot 99%. Independently of that, the `list(args)` re-copy in `native_cb` is pure waste and can be dropped immediately.

**The catch:** it makes the receive thread a *second producer* into `CommandQueue`, which is single-producer/single-consumer by construction (documented at the top of `CommandQueue` in `_core.cpp`). GIL-free C dispatch therefore requires a **multi-producer** queue (CAS on `head`, or per-producer SPSC rings the consumer round-robins). That is the redesign the benchmark declined - correctly, if throughput were the only goal. The under-examined justification for doing it anyway is burst robustness: at high control rates the receive thread's per-message GIL acquire contends with the phase-poll and user control threads. A GIL-free hot path removes the receiver from GIL contention entirely.

## Implemented: GIL-free hot path

Item #4 shipped, using the **per-producer SPSC** variant rather than a lock-free MPSC ring - it is the same idea with far less risk. The design:

- **Two SPSC queues, one consumer.** `Engine` now owns a second `CommandQueue osc_queue` beside the existing `queue`. `queue` keeps its single producer (the Python control thread, GIL held); `osc_queue` has exactly one producer (the native receiver thread, GIL-free). The audio thread is the sole consumer of both - `Engine::drain_commands()` drains them in turn each block. No CAS, no locks, no MPSC ring; each queue stays SPSC by construction. `Voice::osc_apply()` mirrors `dsp_apply()` but posts to `osc_queue`.

- **C-side dispatch table.** `float_params()` / `bool_params()` in `_core.cpp` map each `/set/param/cut/*` address to a `softcut::Voice` setter plus the matching `Voice` mirror field (member pointers). `OscReceiver::try_fast_dispatch()` handles a two-arg `(voice:int, value:int|float)` message entirely in C - no GIL, no `nb::list`, no Python callback - and `handle()` falls back to the Python `deliver()` path for every other address (buffer ops, level/pan, lifecycle, `position`, `voice_sync`). It reads no args until it commits, so a fall-through leaves the message cursor intact for `deliver()`.

- **Atomic mirrors close the race.** The receiver writes the Python-visible mirror fields off the GIL, so the 26 float + 4 bool `FPROP`/`BPROP` mirrors on `Voice` are now `std::atomic` (relaxed load/store). Python getters and the C writer therefore never form a data race, and getters stay coherent with natively-dispatched writes - so the existing `native`-parametrized OSC tests, which assert via `eng[i].rate` etc., pass unchanged.

- **Wiring.** `_OscReceiver` takes the low-level engine (`host.engine._core`) as an optional last argument (`nb::keep_alive` ties its lifetime to the receiver); passing `None` keeps the pure Python-callback behaviour for the codec-only case.

- **Outbound too: a GIL-free phase poll.** The reply path was the last periodic GIL holder - the Python `_PhasePoll` thread woke every ~10 ms, read `quant_phase` (a nanobind getter -> GIL) per voice, and sent under the GIL. `_OscPhasePoll` (native backend only) moves the whole loop into C: a background thread reads `getQuantPhase()` (a plain C++ read, the same off-thread read the Python poll already did) and sends `/poll/softcut/phase` through its own socket, never touching the interpreter. With this, *nothing* on the softcut control path - inbound dispatch or outbound poll - needs the GIL, so a busy Python thread can neither delay it nor be delayed by it. Both `_PhasePoll` and `_OscPhasePoll` expose `poll_once()`/`reset()`, so the backend-parametrized test drives either.

What this does **not** change, consistent with the analysis above: throughput (still transport-bound) and control latency (still block-quantized). The payoff is the burst-robustness / GIL-decoupling one - native param changes now reach the audio thread, and phase replies now leave it, regardless of what the Python interpreter is doing.

### Measured: it holds up under a busy interpreter

`benchmarks/osc_jitter.py` quantifies the win while a background thread hogs the GIL. It reports **two** latencies per message, so the observer confound is measured rather than assumed:

- **dispatch-only** - send to the apply instant recorded *in C* by an apply-latency probe (`_bench_last_apply_ns`), stamped on whichever thread applied the value. This excludes the Python observer and is the true control-to-audio proxy.

- **end-to-end** - send to when a Python poller *sees* the value; its poll needs the GIL, so this carries the observer's own GIL wait.

Both come from one `steady_clock`, so end-to-end minus dispatch-only *is* the observer's contribution. The probe publishes the applied value with release and the poll acquires it, so the timestamp always belongs to that apply (no stale/negative samples). Representative run (2000 samples, 0.5 ms switch interval, macOS, p99):

| backend | load | dispatch-only p99 | end-to-end p99 |
|---|---|---|---|
| python-osc | idle | 0.26 ms | 0.27 ms |
| python-osc | gil-hog | **7.6 ms** | 7.8 ms |
| native | idle | 0.06 ms | 0.07 ms |
| native | gil-hog | **0.08 ms** | 0.82 ms |

The dispatch-only column is the clean result: under contention native stays **flat** (0.06 -> 0.08 ms - the GIL hog cannot delay it), while python-osc blows out ~30x to 7.6 ms (11 ms max). That is a ~**95x** tighter dispatch tail under load, with the observer removed. The native end-to-end row shows the confound directly: 0.82 ms p99 minus the 0.08 ms dispatch is ~0.75 ms of pure observer GIL wait - which is what a Python app *polling state* would feel, but not what the audio thread sees. At CPython's stock 5 ms switch interval the gap is starker still: python-osc drops control messages outright while native keeps applying.

The probe (`probe_stamp`) is compile-time gated on the CMake option `SOFTCUT_ENABLE_BENCH_PROBE` (off in normal and published builds; `make build-bench` turns it on together with tinyosc). When off it is an empty inline the optimizer removes, so the production apply path carries no probe code at all; the benchmark checks `_core.HAVE_BENCH_PROBE` and tells you to rebuild if absent.

## Recommendation

- **Do now** (high leverage, low risk, no queue redesign, no correctness exposure): a bundle sender helper plus documentation (#1), enlarge `SO_RCVBUF` (#3), and drop the `list(args)` double copy (#4, the trivial part).

- **Add if drops appear:** the non-blocking drain loop / `recvmmsg` (#2).

- **Done:** the GIL-free hot path (#4) is implemented via two per-producer SPSC queues (see "Implemented" above), which sidesteps the MPSC ring the benchmark warned against. `benchmarks/osc_jitter.py` measures the payoff with a C-side apply-timestamp probe that removes the observer confound: dispatch-only p99 stays flat under GIL contention for native (~0.08 ms) versus ~7.6 ms for python-osc - a ~95x tighter tail. The win is confirmed, not assumed.

## Open questions

- Target workload: occasional control tweaks, or audio-rate parameter modulation over OSC? Only the latter justifies the MPSC redesign.

- Deployment target: Linux (where `recvmmsg` is available) or macOS-primary (where it is not, so the drain-loop fallback is the only option)?
