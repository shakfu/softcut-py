// Buffer arithmetic shared by the Python extension (src/softcut/_core.cpp) and
// the standalone server (clients/softcut-osc). These are the sample-level
// primitives every buffer operation reduces to -- read, copy and clear are all
// one blended write -- kept apart from the disk layer that calls them
// (clients/softcut-osc/buffer_io.hpp) because that layer needs dr_wav and a
// host's own Engine type, and these need neither.
//
// Semantics match softcut-py's NornsSoftcut (and norns before it), which is the
// reason for sharing: the same arithmetic was written twice, in numpy on the
// Python side and here, and the two have to agree sample for sample.
// Pure C++, no allocation in the hot loop.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace scsh {

// Clamp and quantize one sample to signed 16-bit PCM.
inline int16_t float_to_s16(float x) {
    float v = x < -1.0f ? -1.0f : (x > 1.0f ? 1.0f : x);
    long s = std::lround(v * 32767.0f);
    if (s < -32768) s = -32768;
    if (s > 32767) s = 32767;
    return static_cast<int16_t>(s);
}

// Linear edge envelope: 1 in the interior, a 0->1 / 1->0 ramp over `f` frames at
// each edge (f capped to n/2 by the caller). Mirrors softcut-py's _fade_env.
inline float fade_env_at(long i, long n, long f) {
    if (f <= 0) return 1.0f;
    if (i < f) return static_cast<float>(i) / static_cast<float>(f);
    if (i >= n - f) return static_cast<float>(n - 1 - i) / static_cast<float>(f);
    return 1.0f;
}

// In-place blended write over [start, start+nsrc) of `buf`, with an edge
// crossfade: dst = dst*(1-env) + (dst*preserve + src*mix)*env. This one
// primitive backs read (preserve=0, mix=1), copy (mix=1) and clear
// (src=nullptr, mix=0), so an overwrite fades into the surrounding audio
// instead of clicking. Mirrors softcut-py's NornsSoftcut._apply. `fade` is in
// frames; a negative `start` trims the head of the source.
inline void apply_to_buffer(float *buf, long bframes, long start,
                            const float *src, long nsrc,
                            float preserve, float mix, long fade) {
    long off = 0;
    if (start < 0) { off = -start; start = 0; }  // negative dst trims src head
    long n = std::min(nsrc - off, bframes - start);
    if (n <= 0) return;
    const long f = std::min(fade, n / 2);

    // Unfaded is the common case -- every read, copy and clear that does not ask
    // for an edge crossfade -- and there env is 1 throughout, which collapses the
    // blend to the target. Kept as its own loop so the envelope's branches stay
    // out of it and it vectorizes; the faded path is unchanged.
    if (f <= 0) {
        if (src == nullptr && preserve == 0.0f) {
            std::fill(buf + start, buf + start + n, 0.0f);
        } else if (src == nullptr) {
            for (long i = 0; i < n; ++i) buf[start + i] *= preserve;
        } else {
            for (long i = 0; i < n; ++i)
                buf[start + i] = buf[start + i] * preserve + src[off + i] * mix;
        }
        return;
    }

    for (long i = 0; i < n; ++i) {
        const float env = fade_env_at(i, n, f);
        const float d = buf[start + i];
        const float sv = src ? src[off + i] : 0.0f;
        const float target = d * preserve + sv * mix;
        buf[start + i] = d * (1.0f - env) + target * env;
    }
}

// De-interleave `ncopy` frames of one channel out of interleaved frame data,
// starting at frame `s0`, into caller-provided storage. Frames outside
// [0, total) read as silence rather than out of bounds, so a read that starts
// before the file or runs past its end is padded instead of refused.
inline void extract_channel_into(float *out, const float *data, unsigned channels,
                                 int col, long s0, long ncopy, long total) {
    for (long i = 0; i < ncopy; ++i) {
        const long si = s0 + i;
        out[i] = (si >= 0 && si < total)
                     ? data[si * static_cast<long>(channels) + col]
                     : 0.0f;
    }
}

// The allocating form, for callers that have nowhere to put it yet.
inline std::vector<float> extract_channel(const float *data, unsigned channels,
                                          int col, long s0, long ncopy, long total) {
    std::vector<float> out(static_cast<size_t>(std::max<long>(ncopy, 0)));
    extract_channel_into(out.data(), data, channels, col, s0,
                         std::max<long>(ncopy, 0), total);
    return out;
}

}  // namespace scsh
