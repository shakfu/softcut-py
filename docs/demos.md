# Demos

The [`demos/`](https://github.com/shakfu/softcut-py/tree/main/demos) directory has small, self-contained examples driving softcut with the audio files in `tests/data/`. The demos are written in numpy; audio I/O goes through the library's own WAV helpers (the standard library's `wave` module plus the C sample conversion).

The offline demos render to WAV files under `build/out/` so you can listen to the results; most also accept `--play` to play live to the speakers instead.

```bash
uv run python demos/01_varispeed.py            # render to build/out/
uv run python demos/04_stereo_layers.py --play  # also play live
make demos                                      # play every offline demo in sequence
make demo-looper                                # the interactive mic looper (06)
```

| Demo | Shows |
| --- | --- |
| `01_varispeed.py` | `rate` playback speed, including reverse (negative rate) |
| `02_loop_points.py` | tight `loop_region` windows scanned across a file |
| `03_overdub.py` | sound-on-sound: record a loop, then overdub with `pre_level` feedback |
| `04_stereo_layers.py` | multiple voices at different rates, panned and mixed to stereo |
| `05_filter_sweep.py` | automating the per-voice post filter cutoff across a render |
| `06_live_looper.py` | realtime duplex mic looper (`record_for`); needs a device + mic |
| `07_frippertronics.py` | overdub, replace, partial-replace (fade in/out), and feedback decay |
| `08_tape_stop.py` | `rate_slew_time`: tape-stop, spin-up, and pitch glides |
| `09_filters.py` | pre filter (record colouration) and post filter modes (lp/hp/bp/br) |
| `10_phase_sync.py` | `sync()`, `phase_quant`/`quant_phase`, and live `position` polling |
| `11_capture.py` | `rec_once` one-shot, reverse record, and `rec_offset` feedback delay |
| `12_norns_api.py` | the norns `softcut` compatibility API (`from softcut import norns as softcut`) |

`demos/_util.py` holds the shared helpers (`load_wav_mono`, `write_wav`, `to_buffer`, `render_seconds`, `play`). Because softcut voices are mono, stereo sources are summed to mono on load; the engine mixes voices back to stereo via each voice's `level` and `pan`.
