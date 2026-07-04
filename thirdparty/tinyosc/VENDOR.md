# tinyosc (vendored)

- Upstream: https://github.com/mhroth/tinyosc
- Commit: 7acc37ad4ea555c1ab8b89c4e94eac84e6af8d3a (master, fetched 2026-07-04)
- License: ISC-style permissive (see `LICENSE`), compatible with this project's MIT.

A single-file OSC 1.0 message parser/serializer. It handles only the wire
format (`tosc_parseMessage` / `tosc_writeMessage` and friends); it opens no
sockets. The UDP transport and receive thread are supplied by this project's
own C++ glue, mirroring how `miniaudio` is vendored as plain sources compiled
straight into the `_core` extension.

Vendored unmodified. If upstream changes are ever needed, keep them as patches
under `thirdparty/patches/` rather than editing these files in place.
