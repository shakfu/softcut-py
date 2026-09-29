# comparison between py/rs implementrations

rec_once in softcut-py

Yes, it is implemented. It has two problems.

- Wiring. Voice.rec_once maps to softcut::Voice::setRecOnceFlag (src/softcut/_core.cpp:1093). It is also exposed through OSC (_core.cpp:719, clients/softcut-osc/osc_server.hpp:76) and norns (src/softcut/norns.py:94).
- DSP. I recorded a 0.25 s loop from a 1.0 s input. The buffer kept the first pass. Later input at a different level did not overwrite it.
- Bug: the getters go stale. When the pass ends, upstream clears recFlag on the audio thread (thirdparty/.../Voice.cpp:110-116). The Python mirrors rec_ and rec_once_ are only updated by setters (_core.cpp:224), so both still read True after recording has stopped. A loop like while v.rec_once: ... never exits, and if not v.rec: v.rec = True never re-arms recording. softcut-rs knows about this: scripts/gen_fixtures.py:11 says Python's rec/play "mirror the last set value, not DSP state". Rust's Voice::rec_once() reads head state (softcut-rs/softcut/src/voice.rs:502).
- No test in softcut-py. tests/ never mentions rec_once. demos/11_capture.py uses it, but test_demos.py only checks that the output is finite and not silent. The only real check is indirect: Rust's rec_once golden fixture was generated from softcut-py.

Other softcut-py problems found along the way

1. fade_time reports the wrong value after construction or reset(). Voice::reset() sets 0.01 s. It then calls sch.init(), which sets 0.1 s (ReadWriteHead.cpp:22), so the voice really uses 0.1 s. reset_params() mirrors 0.01 (_core.cpp:273). Rust records this as an upstream quirk (voice.rs:52-59).
2. Recorded audio is polarity-inverted, with gain 1.2. I measured -1.2x at DC, 50, 220 and 1000 Hz, with and without the pre-filter. The cause is upstream: the Raised rec-fade table is sinf(x) * -1.f (FadeCurves.cpp, calcRecFade). Its steady-state lookup reads about -1. The 1.2 is SoftClip's default gain. softcut-py does not document this and has no way to turn it off. Rust has Quirks::Fixed.

Gaps: what softcut-rs has that softcut-py lacks

┌───────────────────────────────────┬───────────────────────┬─────────────────────────────┐
│              Feature              │      softcut-rs       │         softcut-py          │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Getters that read DSP state       │ yes                   │ mirrors of the last set     │
│                                   │                       │ value (see above)           │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Fixes for upstream quirks         │ Quirks::Fixed         │ none                        │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Fade shapes and ratios            │                       │                             │
│ (set_rec_fade_shape,              │ yes                   │ not bound                   │
│ rec_delay_ratio, ...)             │                       │                             │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Head inspection (Voice::heads,    │ yes                   │ only position,              │
│ rec_fade_value)                   │                       │ saved_position, quant_phase │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Multichannel input matrix         │                       │ mono only;                  │
│ (EngineCmd::InputLevel)           │ yes                   │ /set/level/in_cut ignores   │
│                                   │                       │ the channel (osc.py:455)    │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Per-voice insert effects          │ yes                   │ no                          │
│ (process_with)                    │                       │                             │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Effects chain (softcut-fx)        │ yes                   │ no                          │
├───────────────────────────────────┼───────────────────────┼─────────────────────────────┤
│ Cross-platform golden parity      │ yes (x86/Windows      │                             │
│ tests                             │ still unverified, per │ n/a (it is the reference)   │
│                                   │  its TODO)            │                             │
└───────────────────────────────────┴───────────────────────┴─────────────────────────────┘

Gaps: what softcut-py has that softcut-rs lacks

- Audio device I/O in the library (miniaudio, Engine.start(), a null device). Rust's core crate has none; only the demo opens devices.
- WAV I/O in the library. Rust's softcut-osc returns ReadMono/WriteMono actions and leaves file I/O to the host. Only the demo implements it.
- A norns-compatible Python API (norns.py), including buffer_copy_mono/stereo. Rust has CopyRegion but no norns-named layer.
- A standalone native OSC server with a disk worker (clients/softcut-osc), and a TouchOSC layout.
- Python bindings and numpy interop. Rust has no PyO3 layer.

Neither OSC implementation exposes a buffer copy command. Neither does the upstream softcut_jack_osc, so both are faithful to it.

Another way to frame this

Both projects compute the same DSP; the Rust goldens were generated from softcut-py. So the real gap is state reporting, not features. softcut-rs reads state back from the DSP; softcut-py returns the last value it was given. Fix that in softcut-py and three of the problems above go away: stale rec, stale rec_once, and wrong fade_time. That means reading the flags back from softcut-lib (getRecOnceActive, plus a new recFlag getter) instead of adding more mirror patches. The cost is patching the vendored softcut-lib further.

Open questions for you:
- Should softcut-py keep the polarity inversion to stay sample-exact with norns, or add a Quirks-style switch? Adding a switch breaks sample parity with Rust's goldens unless both sides default to upstream behaviour.
- Is multichannel input in scope for softcut-py? Ignoring the in_cut channel is a silent protocol deviation.
