# softcut-osc: standalone (no-Python) softcut OSC server

A single native binary that speaks the monome softcut OSC protocol, with no CPython runtime. It links the same three vendored pieces the Python extension uses -- **softcut-lib** (DSP), **tinyosc** (OSC codec), and **miniaudio** (device) -- and dispatches the OSC namespace directly to the engine in C++.

It is the "pure C++" counterpart to `softcut.osc`: identical DSP, identical wire protocol, but nothing on the control path touches a GIL because there is no interpreter. Use it when you want a headless, dependency-free softcut server (embedded targets, a background audio process) rather than a Python library.

## Build and run

```sh
make build-standalone          # -> build/softcut-osc/softcut-osc
# or:
cmake -S clients/softcut-osc -B build/softcut-osc -DCMAKE_BUILD_TYPE=Release
cmake --build build/softcut-osc

./build/softcut-osc/softcut-osc --help
./build/softcut-osc/softcut-osc                 # listen on 9999, reply to 57120
./build/softcut-osc/softcut-osc --duplex        # also capture mic input
./build/softcut-osc/softcut-osc --null          # headless, no hardware (tests/CI)
```

Options: `--listen-host/-port`, `--reply-host/-port`, `--voices`, `--sample-rate`, `--block-size`, `--buffer-frames`, `--crossfade-ms`, `--resample-on-read`, `--output-device`, `--input-device`, `--list-devices`, `--duplex`, `--null`, `--no-audio`. Run `--list-devices` to see device indices for `--output-device`/`--input-device` (default `-1` = system default).

Headless smoke test (drives the binary over UDP; Python is only the client):

```sh
make test-standalone
```

## OSC namespace

The wire protocol is **0-based** (voices `0..N-1`, buffers `0..1`) and matches `softcut.osc` / the reference `softcut_jack_osc` client:

- **Per-voice params** `if` (voice, value): `/set/param/cut/{rate,loop_start, loop_end,loop_flag,fade_time,rec_level,pre_level,rec_flag,rec_once,play_flag, rec_offset,position,recpre_slew_time,rate_slew_time,phase_quant,phase_offset, pre_filter_*,post_filter_*}`.

- **Mix / routing**: `/set/level/cut` `if`, `/set/pan/cut` `if`, `/set/enabled/cut` `if`, `/set/level/cut_cut` `iif` (src, dst, amount), `/set/level/in_cut` `iif` (in_ch ignored, voice, level), `/set/param/cut/voice_sync` `iif` (dst, src, offset), `/set/param/cut/buffer` `ii` (voice, buffer).

- **Buffer** (in-memory): `/softcut/buffer/clear`, `/softcut/buffer/clear_channel` `i`, `/softcut/buffer/clear_region` `ff`, `/softcut/buffer/clear_region_channel` `iff`, `/softcut/reset`.

- **Disk** (WAV, via dr_wav): `/softcut/buffer/read_mono` `sfffii[ff]` (path, start_src, start_dst, dur, ch_src, ch_dst, [preserve], [mix]), `/softcut/buffer/read_stereo` `sfff[ff]` (..., [preserve], [mix]), `/softcut/buffer/write_mono` `sffi` (path, start, dur, ch), `/softcut/buffer/write_stereo` `sff`. Reads copy at file rate with no resampling by default (a sample-rate mismatch shifts pitch, as on norns; pass `--resample-on-read` to convert to the engine rate instead). The optional `preserve`/`mix` blend the incoming audio with the existing buffer (`dst = dst*preserve + file*mix`, default 0/1 = overwrite). Writes emit 16-bit PCM at the engine rate. Trailing optional args may be omitted.

- **Phase poll**: `/poll/start/cut/phase`, `/poll/stop/cut/phase`; replies `/poll/softcut/phase` `if` (voice, phase) to the reply address on change.

- **Lifecycle**: `/hello`, `/quit`, `/goodbye`.

## Architecture

- **Audio thread** (miniaudio callback) runs the mixer and DSP in pure C++.

- **OSC receive thread** parses with tinyosc and applies each message via a lock-free SPSC command queue drained on the audio thread, so the callback never blocks and never reads a half-written parameter.

- **Disk-worker thread** runs buffer read/write/clear jobs posted by the OSC thread (FIFO, so a write observes a preceding read), keeping file I/O off both the realtime and OSC paths. Buffer-modifying jobs (reads, clears) apply an edge crossfade (`--crossfade-ms`, default 2 ms) so loading or clearing a region the audio thread is playing ramps instead of clicking. The worker drains its queue on shutdown, so a write issued just before `/quit` still flushes.

- **Phase-poll thread** reads each voice's quantized phase and sends replies in C. No thread on the control path acquires a lock the audio thread holds.

## Status

Disk WAV I/O runs on a dedicated worker thread with click-avoidance crossfades
(dr_wav, vendored under `thirdparty/dr_wav`). The server implements the full
softcut OSC namespace.

**By default, reads copy at file rate with no resampling -- this matches
softcut/norns and is deliberate.** The reference `BufDiskWorker::readBufferMono`
reads frames at the file's native rate (libsndfile `readf`) and copies them into
the buffer sample-for-sample, so a file whose sample rate differs from the
engine's plays back pitch-shifted; softcut-py does the same. This is the default
here too.

`--resample-on-read` opts *out* of that softcut behaviour: with it, a file is
converted to the engine sample rate on read (miniaudio's linear `ma_resampler`),
so it loads at its original pitch and occupies its real duration in the buffer.
It is a *beyond-softcut* convenience and stays **off by default** so the default
keeps matching softcut/norns.

`--output-device`/`--input-device` selection is implemented (with bounds
checking) but only exercised manually -- the automated smoke test runs on the
null backend.
