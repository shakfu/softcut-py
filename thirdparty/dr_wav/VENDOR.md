# dr_wav (vendored)

- Upstream: https://github.com/mackron/dr_libs
- Version: dr_wav v0.14.6 (fetched 2026-07-07 from master)
- License: choice of public domain (Unlicense) or MIT-0 (see the license block
  at the end of `dr_wav.h`), compatible with this project's MIT.

A single-file WAV reader/writer by the author of miniaudio. Used only by the
standalone `clients/softcut-osc` binary for the softcut buffer disk operations
(`/softcut/buffer/read_*`, `/softcut/buffer/write_*`); the vendored miniaudio in
this repo is trimmed (`MA_NO_DECODING`/`MA_NO_ENCODING`) and carries no WAV codec.
The Python extension does not use dr_wav (it reads/writes WAV via numpy + the
stdlib `wave` module).

Vendored unmodified. The implementation is compiled in exactly one translation
unit (`clients/softcut-osc/dr_wav_impl.c`, which defines `DR_WAV_IMPLEMENTATION`);
everywhere else includes the header for declarations only. If upstream changes
are ever needed, keep them as patches under `thirdparty/patches/` rather than
editing this file in place.
