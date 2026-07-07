// Standalone softcut engine: voices, shared buffers, mixing, and a miniaudio
// device -- the same DSP core as the Python extension's _core.cpp Engine, but
// with no Python/nanobind. Parameter changes arrive from the OSC thread and are
// applied on the audio thread via a lock-free SPSC command queue, so the audio
// callback never blocks and never races a half-written parameter.
#pragma once

#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <memory>
#include <stdexcept>
#include <vector>

#include "miniaudio.h"
#include "shared/command_queue.hpp"
#include "shared/device.hpp"
#include "shared/mixer.hpp"
#include "softcut/Voice.h"
#include "softcut/Types.h"

namespace scosc {

using scsh::CommandQueue;

// Round up to the next power of two (softcut wraps the head with `phase &
// (frames - 1)`, so buffer frame counts must be powers of two).
inline uint32_t next_pow2(uint32_t n) {
    if (n < 2) return 1;
    n--;
    n |= n >> 1; n |= n >> 2; n |= n >> 4; n |= n >> 8; n |= n >> 16;
    return n + 1;
}

struct Voice {
    softcut::Voice sc;
    // Engine-mix params, read and written only on the audio thread (mutated via
    // the command queue), so no atomics are needed.
    float level = 1.0f;
    float pan = 0.0f;
    float input_gain = 1.0f;
    int buffer_index = 0;  // which shared buffer this voice reads/writes
};

class Engine {
public:
    Engine(int n_voices, float sample_rate, int block_size, uint32_t buffer_frames,
           bool duplex, int out_channels)
        : n_voices_(n_voices), sample_rate_(sample_rate), block_size_(block_size),
          buffer_frames_(next_pow2(buffer_frames)), duplex_(duplex),
          out_channels_(out_channels) {
        if (n_voices_ < 1) throw std::invalid_argument("voices must be >= 1");
        if (block_size_ < 1) throw std::invalid_argument("block_size must be >= 1");
        // Two shared mono buffers, norns-style (voices route to one of them).
        for (auto &b : buffers_) b.assign(buffer_frames_, 0.0f);
        voices_.reserve(static_cast<size_t>(n_voices_));
        for (int i = 0; i < n_voices_; ++i) {
            auto v = std::make_unique<Voice>();  // Voice is non-movable (atomic in sc)
            v->sc.setSampleRate(sample_rate_);
            v->sc.setBuffer(buffers_[0].data(), buffer_frames_);  // default: buf 1
            voices_.push_back(std::move(v));
        }
        silence_.assign(block_size_, 0.0f);
        voice_in_.assign(block_size_, 0.0f);
        fb_.assign(static_cast<size_t>(n_voices_) * n_voices_, 0.0f);
        prev_out_.assign(static_cast<size_t>(n_voices_) * block_size_, 0.0f);
        cur_out_.assign(static_cast<size_t>(n_voices_) * block_size_, 0.0f);
        mix_.resize(static_cast<size_t>(n_voices_));
    }

    ~Engine() { stop(); }

    int n_voices() const { return n_voices_; }
    float sample_rate() const { return sample_rate_; }
    uint32_t buffer_frames() const { return buffer_frames_; }
    Voice &voice(int i) { return *voices_[static_cast<size_t>(i)]; }
    bool running() const { return running_.load(std::memory_order_acquire); }

    float *buffer_data(int index) { return buffers_[index & 1].data(); }

    // Apply a change now, or defer it to the audio thread while the device runs
    // (so the audio thread never reads a half-written parameter).
    void apply(std::function<void()> fn) {
        if (running_.load(std::memory_order_acquire) && queue_.push(fn)) return;
        fn();
    }

    // --- device lifecycle ---------------------------------------------------

    // Print the system playback/capture devices with their indices, for use with
    // --output-device / --input-device. Uses a throwaway default context.
    static void list_devices() {
        ma_context ctx;
        if (ma_context_init(nullptr, 0, nullptr, &ctx) != MA_SUCCESS) {
            std::fprintf(stderr, "failed to init audio context\n");
            return;
        }
        ma_device_info *pb, *cap;
        ma_uint32 npb, ncap;
        if (ma_context_get_devices(&ctx, &pb, &npb, &cap, &ncap) != MA_SUCCESS) {
            std::fprintf(stderr, "failed to enumerate audio devices\n");
            ma_context_uninit(&ctx);
            return;
        }
        std::printf("playback devices:\n");
        for (ma_uint32 i = 0; i < npb; ++i)
            std::printf("  [%u] %s%s\n", i, pb[i].name, pb[i].isDefault ? "  (default)" : "");
        std::printf("capture devices:\n");
        for (ma_uint32 i = 0; i < ncap; ++i)
            std::printf("  [%u] %s%s\n", i, cap[i].name, cap[i].isDefault ? "  (default)" : "");
        ma_context_uninit(&ctx);
    }

    // Open and start the device. output_device/input_device select a device by
    // its index from list_devices() (-1 = system default). null_backend runs
    // headless (silence in, discard out) for tests/CI.
    void start(int output_device, int input_device, bool null_backend) {
        if (running_.load()) return;
        ma_context *pctx = nullptr;
        ma_device_id playback_id, capture_id;
        ma_device_id *p_playback_id = nullptr;
        ma_device_id *p_capture_id = nullptr;

        // A context is needed for the null backend or to resolve explicit ids.
        if (null_backend || output_device >= 0 || input_device >= 0) {
            ma_result r;
            if (null_backend) {
                ma_backend backends[] = {ma_backend_null};
                r = ma_context_init(backends, 1, nullptr, &context_);
            } else {
                r = ma_context_init(nullptr, 0, nullptr, &context_);
            }
            if (r != MA_SUCCESS)
                throw std::runtime_error("failed to init audio context");
            context_inited_ = true;
            pctx = &context_;
        }

        if (!null_backend && (output_device >= 0 || input_device >= 0)) {
            scsh::resolve_device_ids(context_, output_device, input_device, duplex_,
                                     playback_id, capture_id, p_playback_id,
                                     p_capture_id);
        }

        ma_device_config cfg = scsh::make_device_config(
            sample_rate_, block_size_, duplex_, out_channels_, p_playback_id,
            p_capture_id, &Engine::audio_cb, this);

        if (ma_device_init(pctx, &cfg, &device_) != MA_SUCCESS)
            throw std::runtime_error("failed to init audio device");
        device_inited_ = true;
        running_.store(true, std::memory_order_release);
        if (ma_device_start(&device_) != MA_SUCCESS) {
            running_.store(false, std::memory_order_release);
            throw std::runtime_error("failed to start audio device");
        }
    }

    void stop() {
        running_.store(false, std::memory_order_release);
        if (device_inited_) { ma_device_uninit(&device_); device_inited_ = false; }
        if (context_inited_) { ma_context_uninit(&context_); context_inited_ = false; }
        drain_commands();  // apply anything queued but not yet consumed
    }

private:
    static void audio_cb(ma_device *dev, void *out, const void *in, ma_uint32 frames) {
        static_cast<Engine *>(dev->pUserData)->on_audio(
            static_cast<float *>(out), static_cast<const float *>(in),
            static_cast<int>(frames));
    }

    void drain_commands() {
        std::function<void()> fn;
        while (queue_.pop(fn)) fn();
    }

    void on_audio(float *out, const float *in, int frames) {
        drain_commands();
        int done = 0;
        while (done < frames) {
            int chunk = std::min(block_size_, frames - done);
            const float *cin = (duplex_ && in != nullptr) ? (in + done) : silence_.data();
            process_core(cin, out + done * out_channels_, chunk);
            done += chunk;
        }
    }

    // Mono input -> interleaved output via the shared mixer (per-voice input
    // gain + voice->voice feedback delayed one block). Refresh the view from our
    // voices, then run scsh::process_block. Allocation-free.
    void process_core(const float *ext_in, float *out, int frames) {
        for (int i = 0; i < n_voices_; ++i) {
            Voice &v = *voices_[static_cast<size_t>(i)];
            mix_[static_cast<size_t>(i)] = {&v.sc, v.level, v.pan, v.input_gain};
        }
        scsh::process_block(mix_.data(), n_voices_, block_size_, out_channels_,
                            fb_.data(), ext_in, out, frames, voice_in_.data(),
                            prev_out_.data(), cur_out_.data());
        std::swap(prev_out_, cur_out_);  // this block's outputs feed the next
    }

public:
    // Feedback matrix accessor (set on the audio thread via apply()).
    void set_feedback(int src, int dst, float amount) {
        if (src < 0 || src >= n_voices_ || dst < 0 || dst >= n_voices_) return;
        fb_[static_cast<size_t>(src) * n_voices_ + dst] = amount;
    }

private:
    int n_voices_;
    float sample_rate_;
    int block_size_;
    uint32_t buffer_frames_;
    bool duplex_;
    int out_channels_;

    std::vector<std::unique_ptr<Voice>> voices_;
    std::array<std::vector<float>, 2> buffers_;

    std::vector<float> silence_, voice_in_, fb_, prev_out_, cur_out_;
    std::vector<scsh::VoiceMix> mix_;  // per-block voice view (refreshed each block)

    CommandQueue queue_;
    std::atomic<bool> running_{false};

    ma_context context_;
    bool context_inited_ = false;
    ma_device device_;
    bool device_inited_ = false;
};

}  // namespace scosc
