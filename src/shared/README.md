# src/shared — Python-free C++ core

Header-only pieces shared by the two hosts that wrap softcut-lib:

- **`src/softcut/_core.cpp`** — the nanobind Python extension (`softcut._core`).
- **`clients/softcut-osc/`** — the standalone, no-Python OSC server binary.

Everything here is pure C++ with no nanobind/Python dependency, so both builds
compile the same source. Both add `src/` to their include path and reference it
as `#include "shared/…"`; the namespace is `scsh`.

| Header | What it provides |
|---|---|
| `command_queue.hpp` | `scsh::CommandQueue` — the SPSC lock-free command ring drained on the audio thread. |
| `osc_socket.hpp` | Cross-platform IPv4 UDP socket helpers (`socket_t`, `make_sockaddr`, …) plus the platform network headers. Used only where OSC is compiled in. |
| `mixer.hpp` | `scsh::VoiceMix` + `process_block()` — the multi-voice mix (per-voice input gain, voice→voice feedback, equal-power pan). Each host refreshes a `VoiceMix` view from its own Voice objects each block. |
| `device.hpp` | miniaudio device-id resolution and config building (`resolve_device_ids`, `make_device_config`). |
| `buffer_ops.hpp` | `scsh::apply_to_buffer` and friends -- the sample-level arithmetic every buffer operation (read, copy, clear) reduces to, plus the edge-fade envelope, de-interleaving and 16-bit quantization. Bound into Python as `_core._buffer_apply` / `_buffer_extract_channel`. |

## What is deliberately *not* shared

- **The `Voice` wrapper.** The extension's `Voice` carries Python state (a numpy
  buffer keepalive, atomic mirror fields so Python getters see native OSC writes);
  the standalone's is leaner. They cannot be one type, which is why `mixer.hpp`
  takes a `VoiceMix` view rather than a shared Voice.
- **The device *lifecycle* and device listing.** The extension is lazy and
  restartable and returns the device list to Python as a list of dicts; the
  standalone re-inits per start and adds a `--null` headless backend and a
  text-printing lister. Only the mechanical config/selection is shared (above).
- **The buffer *disk* layer.** `clients/softcut-osc/buffer_io.hpp` builds on
  `buffer_ops.hpp` but needs dr_wav and that host's own `Engine` type, neither of
  which the extension compiles; softcut-py reads and writes WAV through numpy and
  the stdlib `wave` module instead. Only the arithmetic underneath is shared.
- **The OSC param dispatch tables.** Both map `/set/param/cut/*` to the same
  `softcut::Voice` setters, but the extension's fast path also binds each address
  to an atomic mirror field (a `Voice::*` pointer into the Python wrapper) that
  has no analogue in the standalone. Sharing only the setter list would not remove
  that mirror table, so the small address list stays duplicated on purpose.
