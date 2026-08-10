// Softcut buffer disk operations for the standalone server, backed by dr_wav.
//
// The sample-level arithmetic these are built on lives in
// `src/shared/buffer_ops.hpp` and is shared with the Python extension; what is
// here is the disk layer, which needs dr_wav and this host's own Engine type and
// so is not shareable as it stands.
//
// Semantics match softcut-py's NornsSoftcut (and norns): reads copy file frames
// into a buffer *at file rate with no resampling* -- a sample-rate mismatch
// shifts pitch, as on norns -- with overwrite (preserve=0, mix=1). Writes emit
// 16-bit PCM WAV at the engine sample rate. All ops run on the caller's (OSC)
// thread and touch the shared buffers directly, so loading/saving while the
// audio thread plays the same region may click -- the same caveat as clears and
// as the Python server.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "dr_wav.h"
#include "engine.hpp"
#include "shared/buffer_ops.hpp"

namespace scosc {

using scsh::apply_to_buffer;
using scsh::extract_channel;
using scsh::fade_env_at;
using scsh::float_to_s16;

// Optionally resample a mono f32 channel from in_sr to out_sr (miniaudio's linear
// resampler). Off by default (see --resample-on-read); softcut/norns copy at file
// rate, which shifts pitch on a sample-rate mismatch, so this only runs when the
// caller opts in. Returns the input unchanged if rates match or on failure.
inline std::vector<float> resample_channel(const std::vector<float> &in,
                                           unsigned in_sr, unsigned out_sr) {
    if (in.empty() || in_sr == 0 || out_sr == 0 || in_sr == out_sr) return in;
    ma_resampler_config cfg = ma_resampler_config_init(
        ma_format_f32, 1, in_sr, out_sr, ma_resample_algorithm_linear);
    ma_resampler rs;
    if (ma_resampler_init(&cfg, nullptr, &rs) != MA_SUCCESS) {
        std::fprintf(stderr, "softcut-osc: resampler init failed; copying at file rate\n");
        return in;
    }
    ma_uint64 expected = 0;
    ma_resampler_get_expected_output_frame_count(&rs, in.size(), &expected);
    std::vector<float> out(static_cast<size_t>(expected));
    ma_uint64 in_off = 0, out_off = 0;
    for (int guard = 0; in_off < in.size() && guard < 1000000; ++guard) {
        ma_uint64 ic = in.size() - in_off;
        if (out.size() <= out_off) out.resize(out.size() + 4096);
        ma_uint64 oc = out.size() - out_off;
        if (ma_resampler_process_pcm_frames(&rs, in.data() + in_off, &ic,
                                            out.data() + out_off, &oc) != MA_SUCCESS)
            break;
        in_off += ic;
        out_off += oc;
        if (ic == 0 && oc == 0) break;  // no progress
    }
    ma_resampler_uninit(&rs, nullptr);
    out.resize(static_cast<size_t>(out_off));
    return out;
}

// Copy one channel of a WAV file into buffer `ch_dst` (0/1) with an edge
// crossfade (see apply_to_buffer). Read at file rate, no resampling. preserve
// keeps a fraction of the existing buffer; mix scales the incoming file.
inline bool read_mono(Engine &e, const std::string &path, double start_src,
                      double start_dst, double dur, int ch_src, int ch_dst,
                      float preserve, float mix, long fade, bool resample) {
    if (ch_dst != 0 && ch_dst != 1) return false;
    unsigned channels = 0, sr = 0;
    drwav_uint64 total = 0;
    float *data = drwav_open_file_and_read_pcm_frames_f32(
        path.c_str(), &channels, &sr, &total, nullptr);
    if (data == nullptr || channels == 0) {
        std::fprintf(stderr, "softcut-osc: read_mono: cannot open %s\n", path.c_str());
        return false;
    }
    const int col = std::min(std::max(ch_src, 0), static_cast<int>(channels) - 1);
    const long s0 = std::lround(start_src * sr);
    long avail = static_cast<long>(total) - s0;
    if (avail < 0) avail = 0;
    const long want = dur < 0.0 ? avail : std::lround(dur * sr);
    const long ncopy = std::min(want, avail);
    std::vector<float> src = extract_channel(data, channels, col, s0, ncopy,
                                             static_cast<long>(total));
    drwav_free(data, nullptr);
    if (resample)
        src = resample_channel(src, sr, static_cast<unsigned>(e.sample_rate()));
    const long d0 = std::lround(start_dst * e.sample_rate());
    apply_to_buffer(e.buffer_data(ch_dst), static_cast<long>(e.buffer_frames()),
                    d0, src.data(), static_cast<long>(src.size()), preserve, mix, fade);
    return true;
}

// Read a (possibly mono) file into both buffers with an edge crossfade: file ch0
// -> buf0, ch1 -> buf1; a mono file spreads its single channel to both.
inline bool read_stereo(Engine &e, const std::string &path, double start_src,
                        double start_dst, double dur, float preserve, float mix,
                        long fade, bool resample) {
    unsigned channels = 0, sr = 0;
    drwav_uint64 total = 0;
    float *data = drwav_open_file_and_read_pcm_frames_f32(
        path.c_str(), &channels, &sr, &total, nullptr);
    if (data == nullptr || channels == 0) {
        std::fprintf(stderr, "softcut-osc: read_stereo: cannot open %s\n", path.c_str());
        return false;
    }
    const long s0 = std::lround(start_src * sr);
    long avail = static_cast<long>(total) - s0;
    if (avail < 0) avail = 0;
    const long want = dur < 0.0 ? avail : std::lround(dur * sr);
    const long ncopy = std::min(want, avail);
    const long d0 = std::lround(start_dst * e.sample_rate());
    const long bframes = static_cast<long>(e.buffer_frames());
    for (int bi = 0; bi < 2; ++bi) {
        const int col = bi < static_cast<int>(channels) ? bi : static_cast<int>(channels) - 1;
        std::vector<float> src = extract_channel(data, channels, col, s0, ncopy,
                                                 static_cast<long>(total));
        if (resample)
            src = resample_channel(src, sr, static_cast<unsigned>(e.sample_rate()));
        apply_to_buffer(e.buffer_data(bi), bframes, d0, src.data(),
                        static_cast<long>(src.size()), preserve, mix, fade);
    }
    drwav_free(data, nullptr);
    return true;
}

// Zero a frame range in one buffer (channel 0/1) or both (channel < 0), with an
// edge crossfade so the cleared region fades into the surrounding audio.
inline void clear_region(Engine &e, int channel, double start_sec, double dur_sec,
                         long fade) {
    const long bframes = static_cast<long>(e.buffer_frames());
    long a = start_sec <= 0.0 ? 0 : std::lround(start_sec * e.sample_rate());
    if (a < 0) a = 0;
    if (a > bframes) a = bframes;
    const long n = dur_sec < 0.0 ? (bframes - a) : std::lround(dur_sec * e.sample_rate());
    if (n <= 0) return;
    for (int ch = 0; ch < 2; ++ch) {
        if (channel >= 0 && ch != channel) continue;
        apply_to_buffer(e.buffer_data(ch), bframes, a, nullptr, n, 0.0f, 0.0f, fade);
    }
}

inline bool write_wav16(const std::string &path, const std::vector<int16_t> &interleaved,
                        unsigned channels, unsigned sample_rate, drwav_uint64 frames) {
    drwav_data_format fmt;
    fmt.container = drwav_container_riff;
    fmt.format = DR_WAVE_FORMAT_PCM;
    fmt.channels = channels;
    fmt.sampleRate = sample_rate;
    fmt.bitsPerSample = 16;
    drwav wav;
    if (!drwav_init_file_write(&wav, path.c_str(), &fmt, nullptr)) {
        std::fprintf(stderr, "softcut-osc: cannot open %s for writing\n", path.c_str());
        return false;
    }
    drwav_uint64 written = drwav_write_pcm_frames(&wav, frames, interleaved.data());
    drwav_uninit(&wav);
    return written == frames;
}

// Write a region of buffer `ch` (0/1) to a mono 16-bit WAV at the engine rate.
inline bool write_mono(Engine &e, const std::string &path, double start, double dur, int ch) {
    if (ch != 0 && ch != 1) return false;
    const float *buf = e.buffer_data(ch);
    const long bframes = static_cast<long>(e.buffer_frames());
    long s0 = std::lround(start * e.sample_rate());
    if (s0 < 0) s0 = 0;
    if (s0 > bframes) s0 = bframes;
    long avail = bframes - s0;
    const long n = dur < 0.0 ? avail : std::min(std::lround(dur * e.sample_rate()), avail);
    std::vector<int16_t> out(static_cast<size_t>(std::max<long>(n, 0)));
    for (long i = 0; i < n; ++i) out[static_cast<size_t>(i)] = float_to_s16(buf[s0 + i]);
    return write_wav16(path, out, 1, static_cast<unsigned>(e.sample_rate()),
                       static_cast<drwav_uint64>(n));
}

// Write buffers 0 and 1 interleaved to a stereo 16-bit WAV at the engine rate.
inline bool write_stereo(Engine &e, const std::string &path, double start, double dur) {
    const float *b0 = e.buffer_data(0);
    const float *b1 = e.buffer_data(1);
    const long bframes = static_cast<long>(e.buffer_frames());
    long s0 = std::lround(start * e.sample_rate());
    if (s0 < 0) s0 = 0;
    if (s0 > bframes) s0 = bframes;
    long avail = bframes - s0;
    const long n = dur < 0.0 ? avail : std::min(std::lround(dur * e.sample_rate()), avail);
    std::vector<int16_t> out(static_cast<size_t>(std::max<long>(n, 0)) * 2);
    for (long i = 0; i < n; ++i) {
        out[static_cast<size_t>(i) * 2 + 0] = float_to_s16(b0[s0 + i]);
        out[static_cast<size_t>(i) * 2 + 1] = float_to_s16(b1[s0 + i]);
    }
    return write_wav16(path, out, 2, static_cast<unsigned>(e.sample_rate()),
                       static_cast<drwav_uint64>(n));
}

}  // namespace scosc
