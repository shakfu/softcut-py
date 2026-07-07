// Multi-voice mixing math, shared by the Python extension (src/softcut/_core.cpp)
// and the standalone server (clients/softcut-osc). Each host keeps its own Voice
// wrapper (the extension's carries numpy/Python state the standalone's does not),
// so this operates on a lightweight per-voice view rather than a shared Voice
// type. Pure C++, allocation-free, realtime-safe.
#pragma once

#include <cmath>
#include <cstddef>

#include "softcut/Voice.h"

namespace scsh {

// A per-voice view for one processed block: the DSP voice plus its engine-mix
// parameters. The caller refreshes these from its own Voice objects each block.
struct VoiceMix {
    softcut::Voice *sc;
    float level;
    float pan;
    float input_gain;
};

// Process up to block_size frames of mono input into interleaved output, applying
// per-voice input gain and voice->voice feedback delayed by one block. Each
// voice's dry output is written into cur_out; the caller swaps prev_out/cur_out
// afterwards so this block feeds the next. `voice_in` is scratch of >= frames;
// `prev_out`/`cur_out` are n_voices*block_size. `fb[src*n_voices + dst]` is the
// feedback gain from src into dst.
inline void process_block(const VoiceMix *voices, int n_voices, int block_size,
                          int out_channels, const float *fb, const float *ext_in,
                          float *out, int frames, float *voice_in,
                          const float *prev_out, float *cur_out) {
    for (int i = 0; i < frames * out_channels; ++i) out[i] = 0.0f;
    for (int dst = 0; dst < n_voices; ++dst) {
        const VoiceMix &vp = voices[dst];
        const float ig = vp.input_gain;
        for (int i = 0; i < frames; ++i) voice_in[i] = ext_in ? ext_in[i] * ig : 0.0f;
        for (int src = 0; src < n_voices; ++src) {
            const float g = fb[static_cast<size_t>(src) * n_voices + dst];
            if (g == 0.0f) continue;
            const float *po = prev_out + static_cast<size_t>(src) * block_size;
            for (int i = 0; i < frames; ++i) voice_in[i] += po[i] * g;
        }
        float *o = cur_out + static_cast<size_t>(dst) * block_size;
        vp.sc->processBlockMono(voice_in, o, frames);
        // equal-power pan: pan -1..1 -> angle 0..pi/2
        const float theta = (vp.pan * 0.5f + 0.5f) * 1.5707963267948966f;
        const float gl = vp.level * std::cos(theta);
        const float gr = vp.level * std::sin(theta);
        for (int f = 0; f < frames; ++f) {
            out[f * out_channels + 0] += o[f] * gl;
            if (out_channels > 1) out[f * out_channels + 1] += o[f] * gr;
        }
    }
}

}  // namespace scsh
