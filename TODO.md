# TODO

## Critical

## High

## Medium

## Low

- [ ] Multichannel input. softcut-lib's `Voice` is mono, but its reference client mixes two input channels into each voice (`inLevel[2][NumVoices]`, `softcut_jack_osc/src/SoftcutClient.h:42`), so this belongs in `src/shared/mixer.hpp`. Until then, `/set/level/in_cut` drops its channel argument (`src/softcut/osc.py:455`).
