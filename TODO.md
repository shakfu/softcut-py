# TODO

## Critical

## High

## Medium

- [ ] Expose fade-curve shapes and ratios (`FadeCurves::setRecShape`, `setPreShape`, `setRecDelayRatio`, `setPreWindowRatio`). softcut-lib implements them, but its `Voice` does not expose them. softcut-rs does. Then test the `fixed` pre-curve fix, which is untested because no shape can be set.
- [ ] Expose per-head state (position, fade, gain) for visualizing crossfades, as softcut-rs `Voice::heads` does.

## Low

- [ ] Multichannel input. softcut-lib's `Voice` is mono, but its reference client mixes two input channels into each voice (`inLevel[2][NumVoices]`, `softcut_jack_osc/src/SoftcutClient.h:42`), so this belongs in `src/shared/mixer.hpp`. Until then, `/set/level/in_cut` drops its channel argument (`src/softcut/osc.py:455`).
