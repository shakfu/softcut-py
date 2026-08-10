// nanobind binding for softcut-lib's per-voice DSP engine (softcut::Voice).
//
// softcut-lib owns no buffer memory: softcut::Voice::setBuffer only stores a
// pointer. We therefore wrap each softcut::Voice in our own Voice struct, which
// holds a reference to the numpy array backing the buffer (keeping it alive)
// and mirrors every write-only parameter so Python can read back what it set.
// (The wrapped softcut class is always spelled softcut::Voice; the unqualified
// Voice below is this binding's wrapper.)

#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <array>
#include <atomic>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <optional>
#include <thread>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "softcut/Voice.h"
#include "softcut/Types.h"

#include "miniaudio.h"

#include "shared/buffer_ops.hpp"
#include "shared/command_queue.hpp"
#include "shared/device.hpp"
#include "shared/mixer.hpp"

// Native OSC codec, compiled in only when the CMake option SOFTCUT_ENABLE_TINYOSC
// is set. The default build ships without it; the pure-Python option (python-osc)
// covers OSC at runtime instead.
#ifdef SOFTCUT_TINYOSC
#include "tinyosc.h"
#include "shared/osc_socket.hpp"  // UDP socket helpers + platform network headers
#include <condition_variable>
#include <limits>
#include <mutex>
#include <chrono>
#include <cstring>
#endif

namespace nb = nanobind;
using namespace nb::literals;

// 1-D, C-contiguous, float32, CPU array (softcut::sample_t == float).
using BufferArray = nb::ndarray<float, nb::ndim<1>, nb::c_contig, nb::device::cpu>;

namespace {

// The SPSC command queue lives in a shared header (used by the standalone server
// too). This binding uses two instances -- one fed by the Python control thread
// (GIL held), one by the native OSC receiver (GIL-free) -- each drained by the
// audio thread, so each stays strictly single-producer. See shared/command_queue.hpp.
using scsh::CommandQueue;

// Benchmark-only apply-latency probe, compiled in only when the CMake option
// SOFTCUT_ENABLE_BENCH_PROBE is set (SOFTCUT_BENCH_PROBE); absent from normal and
// published builds. When enabled, each parameter apply -- in the native OSC fast
// path or in the Python setter -- records a steady_clock timestamp plus the
// applied value, so benchmarks/osc_jitter.py can measure send->apply latency
// without a GIL-gated Python observer in the loop (paired with the send-side
// _steady_clock_ns).
//
// The value is published last with release, and the benchmark reads it with
// acquire (_bench_probe_applied): once it sees its own value, the paired
// timestamp store (sequenced-before the release) is guaranteed visible, so the
// timestamp it reads belongs to *this* apply -- never a stale earlier one.
//
// When the flag is off, probe_stamp() is an empty inline the optimizer removes,
// so the production apply path carries no probe code at all.
#ifdef SOFTCUT_BENCH_PROBE
static std::atomic<int64_t> g_probe_ns{0};
static std::atomic<uint32_t> g_probe_val{0};  // float bits, published last

static inline void probe_stamp(float value) {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    g_probe_ns.store(
        std::chrono::duration_cast<std::chrono::nanoseconds>(now).count(),
        std::memory_order_relaxed);
    uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    g_probe_val.store(bits, std::memory_order_release);
}
#else
static inline void probe_stamp(float) {}
#endif

struct Voice {
    softcut::Voice v;
    nb::object buffer_ref;  // keepalive for the numpy array passed to setBuffer
    float sample_rate = 48000.0f;

    // Engine mix parameters (not softcut params): read by Engine's mixer.
    // level is a linear gain; pan is -1 (left) .. 0 (center) .. +1 (right).
    // input_gain scales the engine's external input fed into this voice.
    //
    // Atomic because the audio thread reads them while Python writes them: the
    // public API and the OSC layer both allow adjusting these live. Every access
    // is relaxed, which on the platforms softcut-py targets is the same
    // instruction a plain float would compile to -- the point is not ordering
    // but that a concurrent read is defined behaviour rather than a data race.
    // The observable behaviour is unchanged: a read is at worst one block stale.
    std::atomic<float> level_{1.0f};
    std::atomic<float> pan_{0.0f};
    std::atomic<float> input_gain_{1.0f};

    // Set when the voice is hosted by an Engine. While the engine is running,
    // DSP setters are applied on the audio thread via a command queue. There are
    // two queues, one per producer, so each stays single-producer/single-
    // consumer: cmd_queue_ is fed by the Python control thread (GIL held) and
    // osc_queue_ by the native OSC receiver thread (GIL-free). The audio thread
    // is the sole consumer of both (Engine::drain_commands drains each).
    CommandQueue *cmd_queue_ = nullptr;
    CommandQueue *osc_queue_ = nullptr;
    std::atomic<bool> *engine_running_ = nullptr;

    // How long to wait for space before giving up on a full command queue.
    //
    // The audio thread drains the whole queue once per block, so waiting is only
    // ever waiting for the next callback -- and it has to be a real wait: a
    // yield-spin returns in microseconds while a block is milliseconds, so it
    // gives up long before the drain it is waiting for. 100 x 250us covers about
    // two block periods at 512 frames / 48 kHz, and costs nothing at all unless
    // the queue is genuinely full, which needs >4096 changes inside one block.
    static constexpr int kPushAttempts = 100;
    static constexpr std::chrono::microseconds kPushWait{250};

    // Commands dropped because the queue stayed full. Nonzero means control
    // changes were lost -- the engine is being driven faster than the audio
    // thread drains, or the device stopped calling back.
    std::atomic<uint64_t> dropped_commands_{0};

    // Push with a bounded retry, counting a drop if it never lands.
    //
    // The alternative -- applying the change here when the queue is full -- is
    // what this deliberately does not do: it would mutate DSP state on the
    // producer thread while the audio thread reads it, which is precisely the
    // race the queue exists to prevent. A lost parameter update is recoverable
    // (the next one supersedes it, and every softcut set is idempotent); a torn
    // one is not, and it is silent.
    bool queue_apply(CommandQueue *queue, const std::function<void()> &fn) {
        for (int attempt = 0; attempt < kPushAttempts; ++attempt) {
            if (queue->push(fn)) return true;
            std::this_thread::sleep_for(kPushWait);
        }
        dropped_commands_.fetch_add(1, std::memory_order_relaxed);
        return false;
    }

    // Apply a softcut DSP change now, or defer it to the audio thread if an
    // engine is running (so the audio thread never reads a half-written param).
    void dsp_apply(std::function<void()> fn) {
        if (engine_running_ != nullptr &&
            engine_running_->load(std::memory_order_acquire)) {
            queue_apply(cmd_queue_, fn);
            return;
        }
        fn();  // no audio thread to race: apply directly
    }

    // Like dsp_apply, but posts to the native OSC receiver's dedicated queue.
    // Called only from the OSC receiver thread, so osc_queue_ has one producer.
    void osc_apply(std::function<void()> fn) {
        if (engine_running_ != nullptr &&
            engine_running_->load(std::memory_order_acquire) &&
            osc_queue_ != nullptr) {
            queue_apply(osc_queue_, fn);
            return;
        }
        fn();
    }

    // Mirrors of write-only parameters, seeded with softcut::Voice::reset()
    // defaults so getters are meaningful before the first set. Atomic because
    // the native OSC fast path (OscReceiver) writes them off the GIL while
    // Python getters read them; every access is a relaxed load/store, so there
    // is no data race. See std::atomic's implicit T conversion (used by the
    // FPROP/BPROP getters and setters below).
    std::atomic<float> rate_{1.0f};
    std::atomic<float> loop_start_{0.0f};
    std::atomic<float> loop_end_{0.0f};
    std::atomic<bool> loop_{false};
    std::atomic<bool> rec_{false};
    std::atomic<bool> rec_once_{false};
    std::atomic<bool> play_{false};
    std::atomic<float> fade_time_{0.01f};
    std::atomic<float> rec_level_{0.0f};
    std::atomic<float> pre_level_{0.0f};
    std::atomic<float> rec_offset_{-8.0f / 48000.0f};
    std::atomic<float> rec_pre_slew_time_{0.001f};
    std::atomic<float> rate_slew_time_{0.001f};
    std::atomic<float> phase_quant_{0.0f};
    std::atomic<float> phase_offset_{0.0f};

    // Pre filter
    std::atomic<float> pre_filter_fc_{16000.0f};
    std::atomic<float> pre_filter_rq_{4.0f};
    std::atomic<float> pre_filter_lp_{1.0f};
    std::atomic<float> pre_filter_hp_{0.0f};
    std::atomic<float> pre_filter_bp_{0.0f};
    std::atomic<float> pre_filter_br_{0.0f};
    std::atomic<float> pre_filter_dry_{0.0f};
    std::atomic<float> pre_filter_fc_mod_{1.0f};

    // Post filter
    std::atomic<float> post_filter_fc_{12000.0f};
    std::atomic<float> post_filter_rq_{4.0f};
    std::atomic<float> post_filter_lp_{0.0f};
    std::atomic<float> post_filter_hp_{0.0f};
    std::atomic<float> post_filter_bp_{0.0f};
    std::atomic<float> post_filter_br_{0.0f};
    std::atomic<float> post_filter_dry_{1.0f};

    // Restore the mirrors to what a freshly reset softcut::Voice holds. The
    // declarations above say they are "seeded with softcut::Voice::reset()
    // defaults", but until this existed only *construction* established that:
    // after reset() the DSP was back to defaults while every Python getter still
    // reported the pre-reset value. Any of these that stops matching its
    // initializer above is a bug in one of the two.
    void reset_params() {
        rate_ = 1.0f;
        loop_start_ = 0.0f;
        loop_end_ = 0.0f;
        loop_ = false;
        rec_ = false;
        rec_once_ = false;
        play_ = false;
        fade_time_ = 0.01f;
        rec_level_ = 0.0f;
        pre_level_ = 0.0f;
        rec_offset_ = -8.0f / 48000.0f;
        rec_pre_slew_time_ = 0.001f;
        rate_slew_time_ = 0.001f;
        phase_quant_ = 0.0f;
        phase_offset_ = 0.0f;
        pre_filter_fc_ = 16000.0f;
        pre_filter_rq_ = 4.0f;
        pre_filter_lp_ = 1.0f;
        pre_filter_hp_ = 0.0f;
        pre_filter_bp_ = 0.0f;
        pre_filter_br_ = 0.0f;
        pre_filter_dry_ = 0.0f;
        pre_filter_fc_mod_ = 1.0f;
        post_filter_fc_ = 12000.0f;
        post_filter_rq_ = 4.0f;
        post_filter_lp_ = 0.0f;
        post_filter_hp_ = 0.0f;
        post_filter_bp_ = 0.0f;
        post_filter_br_ = 0.0f;
        post_filter_dry_ = 1.0f;
    }

    explicit Voice(float sr) : sample_rate(sr) {
        v.setSampleRate(sr);
    }

    void set_sample_rate(float hz) {
        sample_rate = hz;
        v.setSampleRate(hz);
    }

    void set_buffer(nb::object arr) {
        // convert=false: never accept a temporary copy, since setBuffer only
        // stores the pointer and we must keep the real array alive.
        BufferArray a = nb::cast<BufferArray>(arr, false);
        size_t n = a.shape(0);
        // The read/write head wraps its index with `phase & (frames - 1)`, so
        // the frame count MUST be a positive power of two. A non-power-of-two
        // length produces an out-of-bounds head (silent in release builds).
        if (n == 0 || (n & (n - 1)) != 0) {
            throw std::invalid_argument(
                "softcut buffer length must be a positive power of two (got " +
                std::to_string(n) + ")");
        }
        v.setBuffer(a.data(), static_cast<unsigned int>(n));
        buffer_ref = std::move(arr);
    }

    // Process one mono block into the caller's buffer, which is returned. The
    // extension never allocates the result: softcut.Voice.process supplies a
    // buffer when the caller does not, which is what keeps numpy out of the
    // extension entirely.
    nb::object process(nb::object input, nb::object out) {
        BufferArray in = nb::cast<BufferArray>(input);
        size_t n = in.shape(0);

        BufferArray o = nb::cast<BufferArray>(out);
        if (o.shape(0) < n) {
            throw std::invalid_argument(
                "out is too small: need " + std::to_string(n) + " samples, got " +
                std::to_string(o.shape(0)));
        }
        v.processBlockMono(in.data(), o.data(), static_cast<int>(n));
        return out;
    }
};

// Multi-voice host: owns a set of Voice* and a miniaudio device. The same
// per-block routine (process_core) drives both the realtime device callback
// (on miniaudio's audio thread, no GIL) and the offline render() path.
struct Engine {
    std::vector<Voice *> voices;
    int n_voices;
    float sample_rate;
    int block_size;
    bool duplex;       // true: capture mic input; false: playback only
    int out_channels;  // device playback channels (typically 2)
    int output_device_index;  // -1 = default device
    int input_device_index;   // -1 = default device

    std::vector<float> silence;   // zeroed mono input for playback / no input
    std::vector<float> voice_in;  // per-voice input scratch (block_size)
    // Feedback matrix, fb[src*n_voices + dst]. Atomic for the same reason as the
    // mix scalars; the shared mixer takes a plain `const float *`, so each block
    // snapshots it into fb_snap rather than the mixer learning about atomics.
    std::vector<std::atomic<float>> fb;
    std::vector<float> fb_snap;
    std::vector<float> prev_out;  // last block's per-voice output (n*block_size)
    std::vector<float> cur_out;   // this block's per-voice output (n*block_size)
    std::vector<scsh::VoiceMix> mix;  // per-block voice view (refreshed each block)

    CommandQueue queue;      // producer: Python control thread (GIL held)
    CommandQueue osc_queue;  // producer: native OSC receiver thread (GIL-free)
    std::atomic<bool> running{false};

    ma_context context;
    bool context_inited = false;
    ma_device device;
    bool device_inited = false;
    bool device_started = false;

    Engine(std::vector<Voice *> vs, float sr, int block, bool dup, int out_ch,
           int out_dev, int in_dev)
        : voices(std::move(vs)), n_voices(static_cast<int>(voices.size())),
          sample_rate(sr), block_size(block), duplex(dup), out_channels(out_ch),
          output_device_index(out_dev), input_device_index(in_dev) {
        if (block_size < 1) throw std::invalid_argument("block_size must be >= 1");
        if (out_channels < 1) throw std::invalid_argument("out_channels must be >= 1");
        silence.assign(block_size, 0.0f);
        voice_in.assign(block_size, 0.0f);
        // vector's fill constructor value-initializes, which zeroes an atomic
        // even in C++17 where its default constructor does not.
        fb = std::vector<std::atomic<float>>(static_cast<size_t>(n_voices) * n_voices);
        fb_snap.assign(static_cast<size_t>(n_voices) * n_voices, 0.0f);
        prev_out.assign(static_cast<size_t>(n_voices) * block_size, 0.0f);
        cur_out.assign(static_cast<size_t>(n_voices) * block_size, 0.0f);
        mix.resize(static_cast<size_t>(n_voices));
        for (Voice *vp : voices) {
            vp->set_sample_rate(sr);
            vp->cmd_queue_ = &queue;
            vp->osc_queue_ = &osc_queue;
            vp->engine_running_ = &running;
        }
    }

    ~Engine() {
        running.store(false, std::memory_order_release);
        if (device_inited) ma_device_uninit(&device);  // joins the audio thread
        if (context_inited) ma_context_uninit(&context);
        for (Voice *vp : voices) {  // never leave dangling pointers into us
            vp->cmd_queue_ = nullptr;
            vp->osc_queue_ = nullptr;
            vp->engine_running_ = nullptr;
        }
    }

    // Drain and apply any queued parameter changes. Runs on the audio thread at
    // each block, and on the control thread once the device has stopped.
    void drain_commands() {
        std::function<void()> fn;
        while (queue.pop(fn)) fn();       // Python-control-thread commands
        while (osc_queue.pop(fn)) fn();   // native-OSC-thread commands
    }

    // Process up to block_size frames of mono input into interleaved stereo
    // output, applying per-voice input gain and voice->voice feedback (delayed
    // by one block). GIL-free and allocation-free.
    void process_core(const float *ext_in, float *out, int frames) {
        // Refresh the per-voice view (mix params live in our Voice wrapper), then
        // run the shared mixer. GIL-free and allocation-free.
        for (int i = 0; i < n_voices; ++i) {
            Voice *vp = voices[i];
            mix[static_cast<size_t>(i)] = {&vp->v,
                                           vp->level_.load(std::memory_order_relaxed),
                                           vp->pan_.load(std::memory_order_relaxed),
                                           vp->input_gain_.load(std::memory_order_relaxed)};
        }
        for (size_t i = 0; i < fb.size(); ++i)
            fb_snap[i] = fb[i].load(std::memory_order_relaxed);
        scsh::process_block(mix.data(), n_voices, block_size, out_channels,
                            fb_snap.data(), ext_in, out, frames, voice_in.data(),
                            prev_out.data(), cur_out.data());
        std::swap(prev_out, cur_out);  // this block's outputs feed the next
    }

    void set_feedback(int src, int dst, float amount) {
        if (src < 0 || src >= n_voices || dst < 0 || dst >= n_voices)
            throw std::out_of_range("voice index out of range");
        fb[static_cast<size_t>(src) * n_voices + dst].store(amount,
                                                           std::memory_order_relaxed);
    }

    float get_feedback(int src, int dst) const {
        if (src < 0 || src >= n_voices || dst < 0 || dst >= n_voices)
            throw std::out_of_range("voice index out of range");
        return fb[static_cast<size_t>(src) * n_voices + dst].load(
            std::memory_order_relaxed);
    }

    // Called from miniaudio's audio thread. Chunks frameCount to block_size.
    void callback_process(const float *in, float *out, int frameCount) {
        drain_commands();
        int done = 0;
        while (done < frameCount) {
            int chunk = std::min(block_size, frameCount - done);
            const float *cin = (duplex && in != nullptr) ? (in + done) : silence.data();
            process_core(cin, out + done * out_channels, chunk);
            done += chunk;
        }
    }

    // Offline: mono input (n,) -> interleaved output written into `dest`, a flat
    // buffer of n*out_channels floats, which is returned. As with process(), the
    // extension never allocates the result.
    nb::object render(nb::object input, nb::object dest) {
        BufferArray in = nb::cast<BufferArray>(input);
        size_t n = in.shape(0);
        const float *inp = in.data();
        const size_t needed = n * static_cast<size_t>(out_channels);

        BufferArray d = nb::cast<BufferArray>(dest);
        if (d.shape(0) < needed) {
            throw std::invalid_argument(
                "out is too small: need " + std::to_string(needed) +
                " samples (n * out_channels), got " + std::to_string(d.shape(0)));
        }
        float *out = d.data();

        size_t done = 0;
        while (done < n) {
            int chunk = static_cast<int>(std::min(static_cast<size_t>(block_size), n - done));
            process_core(inp + done, out + done * out_channels, chunk);
            done += static_cast<size_t>(chunk);
        }
        return dest;
    }

    void start() {
        if (device_started) return;
        if (!device_inited) {
            ma_device_id playback_id, capture_id;
            ma_device_id *p_playback_id = nullptr;
            ma_device_id *p_capture_id = nullptr;

            // Explicit device selection requires a context to resolve ids.
            if (output_device_index >= 0 || input_device_index >= 0) {
                if (!context_inited) {
                    if (ma_context_init(nullptr, 0, nullptr, &context) != MA_SUCCESS)
                        throw std::runtime_error("failed to initialize audio context");
                    context_inited = true;
                }
                scsh::resolve_device_ids(context, output_device_index,
                                         input_device_index, duplex, playback_id,
                                         capture_id, p_playback_id, p_capture_id);
            }

            ma_device_config cfg = scsh::make_device_config(
                sample_rate, block_size, duplex, out_channels, p_playback_id,
                p_capture_id, &Engine::data_callback, this);
            ma_context *p_ctx = context_inited ? &context : nullptr;
            if (ma_device_init(p_ctx, &cfg, &device) != MA_SUCCESS)
                throw std::runtime_error("failed to initialize audio device");
            device_inited = true;
        }
        if (ma_device_start(&device) != MA_SUCCESS)
            throw std::runtime_error("failed to start audio device");
        device_started = true;
        running.store(true, std::memory_order_release);
    }

    void stop() {
        if (!device_started) return;
        running.store(false, std::memory_order_release);
        ma_device_stop(&device);  // synchronous: no callback runs after this
        drain_commands();         // apply anything queued but not yet consumed
        device_started = false;
    }

    static void data_callback(ma_device *dev, void *pOutput, const void *pInput,
                              ma_uint32 frameCount) {
        Engine *e = static_cast<Engine *>(dev->pUserData);
        e->callback_process(static_cast<const float *>(pInput),
                            static_cast<float *>(pOutput),
                            static_cast<int>(frameCount));
    }
};

// Enumerate the system audio devices. Returns a list of dicts with keys
// index/name/type/is_default; the index is what Engine(output_device=...) and
// Engine(input_device=...) expect for the matching type.
nb::list list_audio_devices() {
    nb::list result;
    ma_context ctx;
    if (ma_context_init(nullptr, 0, nullptr, &ctx) != MA_SUCCESS)
        throw std::runtime_error("failed to initialize audio context");
    ma_device_info *playback_infos, *capture_infos;
    ma_uint32 n_playback, n_capture;
    if (ma_context_get_devices(&ctx, &playback_infos, &n_playback, &capture_infos,
                               &n_capture) != MA_SUCCESS) {
        ma_context_uninit(&ctx);
        throw std::runtime_error("failed to enumerate audio devices");
    }
    auto add = [&](ma_device_info *infos, ma_uint32 count, const char *type) {
        for (ma_uint32 i = 0; i < count; ++i) {
            nb::dict d;
            d["index"] = static_cast<int>(i);
            d["name"] = infos[i].name;
            d["type"] = type;
            d["is_default"] = static_cast<bool>(infos[i].isDefault);
            result.append(d);
        }
    };
    add(playback_infos, n_playback, "playback");
    add(capture_infos, n_capture, "capture");
    ma_context_uninit(&ctx);
    return result;
}

#ifdef SOFTCUT_TINYOSC
// --- Native OSC transport (UDP + vendored tinyosc), gated on the CMake option.
//
// The receiver owns a UDP socket and a background thread; each datagram is
// parsed in C with tinyosc and handed up to a Python callback. Dispatch happens
// under the GIL, on purpose: the DSP command queue is single-producer (the
// Python control thread), so parameter writes must serialize with it via the
// GIL rather than posting from a second thread. This makes the native path a
// dependency-free transport (no python-osc), not a GIL-free fast path.

// UDP socket helpers are shared with the standalone server (see
// shared/osc_socket.hpp, included at file scope above).
using scsh::socket_t;
using scsh::kInvalidSocket;
using scsh::close_socket;
using scsh::ensure_sockets;
using scsh::set_recv_timeout_ms;
using scsh::make_sockaddr;

// --- Native fast-path dispatch tables ---------------------------------------
//
// The per-voice `/set/param/cut/*` messages are the real-time control traffic.
// Each maps 1:1 to a softcut::Voice setter plus a Voice mirror field, so the
// receiver can dispatch them entirely in C -- no GIL, no Python callback --
// writing the atomic mirror and posting the setter to the engine's OSC command
// queue. Everything else (buffer ops, level/pan, lifecycle, position, ...) is
// not in these tables and falls back to the Python callback path in deliver().
struct FloatParam {
    std::atomic<float> Voice::*mirror;
    void (softcut::Voice::*setter)(float);
};
struct BoolParam {
    std::atomic<bool> Voice::*mirror;
    void (softcut::Voice::*setter)(bool);
};

static const std::unordered_map<std::string, FloatParam> &float_params() {
    static const std::unordered_map<std::string, FloatParam> t = {
        {"/set/param/cut/rate", {&Voice::rate_, &softcut::Voice::setRate}},
        {"/set/param/cut/loop_start", {&Voice::loop_start_, &softcut::Voice::setLoopStart}},
        {"/set/param/cut/loop_end", {&Voice::loop_end_, &softcut::Voice::setLoopEnd}},
        {"/set/param/cut/fade_time", {&Voice::fade_time_, &softcut::Voice::setFadeTime}},
        {"/set/param/cut/rec_level", {&Voice::rec_level_, &softcut::Voice::setRecLevel}},
        {"/set/param/cut/pre_level", {&Voice::pre_level_, &softcut::Voice::setPreLevel}},
        {"/set/param/cut/rec_offset", {&Voice::rec_offset_, &softcut::Voice::setRecOffset}},
        {"/set/param/cut/recpre_slew_time", {&Voice::rec_pre_slew_time_, &softcut::Voice::setRecPreSlewTime}},
        {"/set/param/cut/rate_slew_time", {&Voice::rate_slew_time_, &softcut::Voice::setRateSlewTime}},
        {"/set/param/cut/phase_quant", {&Voice::phase_quant_, &softcut::Voice::setPhaseQuant}},
        {"/set/param/cut/phase_offset", {&Voice::phase_offset_, &softcut::Voice::setPhaseOffset}},
        {"/set/param/cut/pre_filter_fc", {&Voice::pre_filter_fc_, &softcut::Voice::setPreFilterFc}},
        {"/set/param/cut/pre_filter_fc_mod", {&Voice::pre_filter_fc_mod_, &softcut::Voice::setPreFilterFcMod}},
        {"/set/param/cut/pre_filter_rq", {&Voice::pre_filter_rq_, &softcut::Voice::setPreFilterRq}},
        {"/set/param/cut/pre_filter_lp", {&Voice::pre_filter_lp_, &softcut::Voice::setPreFilterLp}},
        {"/set/param/cut/pre_filter_hp", {&Voice::pre_filter_hp_, &softcut::Voice::setPreFilterHp}},
        {"/set/param/cut/pre_filter_bp", {&Voice::pre_filter_bp_, &softcut::Voice::setPreFilterBp}},
        {"/set/param/cut/pre_filter_br", {&Voice::pre_filter_br_, &softcut::Voice::setPreFilterBr}},
        {"/set/param/cut/pre_filter_dry", {&Voice::pre_filter_dry_, &softcut::Voice::setPreFilterDry}},
        {"/set/param/cut/post_filter_fc", {&Voice::post_filter_fc_, &softcut::Voice::setPostFilterFc}},
        {"/set/param/cut/post_filter_rq", {&Voice::post_filter_rq_, &softcut::Voice::setPostFilterRq}},
        {"/set/param/cut/post_filter_lp", {&Voice::post_filter_lp_, &softcut::Voice::setPostFilterLp}},
        {"/set/param/cut/post_filter_hp", {&Voice::post_filter_hp_, &softcut::Voice::setPostFilterHp}},
        {"/set/param/cut/post_filter_bp", {&Voice::post_filter_bp_, &softcut::Voice::setPostFilterBp}},
        {"/set/param/cut/post_filter_br", {&Voice::post_filter_br_, &softcut::Voice::setPostFilterBr}},
        {"/set/param/cut/post_filter_dry", {&Voice::post_filter_dry_, &softcut::Voice::setPostFilterDry}},
    };
    return t;
}

static const std::unordered_map<std::string, BoolParam> &bool_params() {
    static const std::unordered_map<std::string, BoolParam> t = {
        {"/set/param/cut/loop_flag", {&Voice::loop_, &softcut::Voice::setLoopFlag}},
        {"/set/param/cut/rec_flag", {&Voice::rec_, &softcut::Voice::setRecFlag}},
        {"/set/param/cut/rec_once", {&Voice::rec_once_, &softcut::Voice::setRecOnceFlag}},
        {"/set/param/cut/play_flag", {&Voice::play_, &softcut::Voice::setPlayFlag}},
    };
    return t;
}

// Receives OSC over UDP. Per-voice param messages are dispatched in C without
// the GIL (see try_fast_dispatch); every other address is delivered to a Python
// callback as (address: str, args: list). Args cover the i/f/s/h/d/T/F type tags
// used by the softcut protocol; an unknown tag stops parsing that message.
class OscReceiver {
public:
    OscReceiver(const std::string &host, int port, nb::callable cb,
                Engine *engine)
        : callback_(std::move(cb)), engine_(engine) {
        ensure_sockets();
        sock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ == kInvalidSocket)
            throw std::runtime_error("OSC receiver: socket() failed");
        int one = 1;
        setsockopt(sock_, SOL_SOCKET, SO_REUSEADDR,
                   reinterpret_cast<const char *>(&one), sizeof(one));
        sockaddr_in addr;
        make_sockaddr(host, port, addr);
        if (::bind(sock_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
            close_socket(sock_);
            sock_ = kInvalidSocket;
            throw std::runtime_error("OSC receiver: bind() failed on port " +
                                     std::to_string(port));
        }
        set_recv_timeout_ms(sock_, 100);  // so the loop can poll running_
        sockaddr_in bound;
        socklen_t bl = sizeof(bound);
        port_ = (getsockname(sock_, reinterpret_cast<sockaddr *>(&bound), &bl) == 0)
                    ? ntohs(bound.sin_port)
                    : port;
    }

    ~OscReceiver() {
        stop();
        if (sock_ != kInvalidSocket) close_socket(sock_);
    }

    int port() const { return port_; }

    void start() {
        if (running_.exchange(true)) return;
        thread_ = std::thread([this] { run(); });
    }

    void stop() {
        running_.store(false);
        if (thread_.joinable()) {
            // Release the GIL so the recv thread can acquire it to finish an
            // in-flight callback, otherwise join() would deadlock.
            nb::gil_scoped_release rel;
            thread_.join();
        }
        // Close the socket here rather than leaving it to the destructor, so
        // stopping a server frees its port immediately -- as the python-osc
        // backend's server_close() does. Waiting for the object to be collected
        // would leave the port held for an indeterminate time, and the two
        // backends must behave the same. stop() is terminal: the receiver is not
        // restartable afterwards, matching the other backend.
        if (sock_ != kInvalidSocket) {
            close_socket(sock_);
            sock_ = kInvalidSocket;
        }
    }

private:
    void run() {
        std::vector<char> buf(4096);
        while (running_.load()) {
            sockaddr_in from;
            socklen_t fl = sizeof(from);
            int n = static_cast<int>(::recvfrom(sock_, buf.data(),
                static_cast<int>(buf.size()), 0,
                reinterpret_cast<sockaddr *>(&from), &fl));
            if (n <= 0) continue;  // timeout or error
            if (tosc_isBundle(buf.data())) {
                tosc_bundle b;
                tosc_parseBundle(&b, buf.data(), n);
                tosc_message m;
                while (tosc_getNextMessage(&b, &m)) handle(&m);
            } else {
                tosc_message m;
                if (tosc_parseMessage(&m, buf.data(), n) == 0) handle(&m);
            }
        }
    }

    // Route one parsed message: try the GIL-free fast path, else the Python
    // callback. try_fast_dispatch must not consume the message's args unless it
    // commits to handling it, so a fall-through leaves the cursor for deliver().
    void handle(tosc_message *m) {
        if (engine_ != nullptr && try_fast_dispatch(m)) return;
        deliver(m);
    }

    // Dispatch a `/set/param/cut/*` (voice:int, value:int|float) message
    // entirely in C, without the GIL. Returns false (cursor untouched) for any
    // address or shape it does not handle, so deliver() can take over.
    bool try_fast_dispatch(tosc_message *m) {
        const char *fmt = tosc_getFormat(m);
        // Exactly two args: an int voice index and an int or float value.
        if (fmt == nullptr || fmt[0] != 'i') return false;
        const char t1 = fmt[1];
        if ((t1 != 'f' && t1 != 'i') || fmt[2] != '\0') return false;

        const char *addr = tosc_getAddress(m);
        const auto &fmap = float_params();
        const auto fit = fmap.find(addr);
        const auto &bmap = bool_params();
        const auto bit = (fit == fmap.end()) ? bmap.find(addr) : bmap.end();
        if (fit == fmap.end() && bit == bmap.end()) return false;

        // Committed: reading args now advances the message cursor.
        const int vi = tosc_getNextInt32(m);
        const float raw = (t1 == 'f') ? tosc_getNextFloat(m)
                                      : static_cast<float>(tosc_getNextInt32(m));
        if (vi < 0 || vi >= engine_->n_voices) return true;  // drop bad index
        Voice *pv = engine_->voices[static_cast<size_t>(vi)];

        if (fit != fmap.end()) {
            const FloatParam fp = fit->second;
            (pv->*(fp.mirror)).store(raw, std::memory_order_relaxed);
            const auto setter = fp.setter;
            pv->osc_apply([pv, setter, raw] { (pv->v.*setter)(raw); });
            probe_stamp(raw);  // apply-latency probe (benchmark only)
        } else {
            const BoolParam bp = bit->second;
            const bool val = raw != 0.0f;
            (pv->*(bp.mirror)).store(val, std::memory_order_relaxed);
            const auto setter = bp.setter;
            pv->osc_apply([pv, setter, val] { (pv->v.*setter)(val); });
            probe_stamp(val ? 1.0f : 0.0f);
        }
        return true;
    }

    void deliver(tosc_message *m) {
        const char *address = tosc_getAddress(m);
        const char *fmt = tosc_getFormat(m);
        nb::gil_scoped_acquire gil;
        nb::list args;
        for (const char *t = fmt; *t; ++t) {
            if (*t == 'i') args.append(tosc_getNextInt32(m));
            else if (*t == 'f') args.append(tosc_getNextFloat(m));
            else if (*t == 's') args.append(std::string(tosc_getNextString(m)));
            else if (*t == 'h') args.append(static_cast<int64_t>(tosc_getNextInt64(m)));
            else if (*t == 'd') args.append(tosc_getNextDouble(m));
            else if (*t == 'T') args.append(true);
            else if (*t == 'F') args.append(false);
            else break;  // unknown tag: stop to avoid misaligned reads
        }
        try {
            callback_(nb::str(address), args);
        } catch (nb::python_error &e) {
            e.restore();
            PyErr_Clear();  // isolate a bad callback; keep receiving
        } catch (...) {
        }
    }

    nb::callable callback_;
    Engine *engine_ = nullptr;  // fast-path target; null = Python callback only
    std::atomic<bool> running_{false};
    std::thread thread_;
    socket_t sock_ = kInvalidSocket;
    int port_ = 0;
};

// Minimal UDP sender for the softcut reply protocol. Only the outbound phase
// message (`/poll/softcut/phase <int> <float>`) is emitted, so it supports the
// empty and (int, float) signatures; other shapes raise.
class OscSender {
public:
    OscSender(const std::string &host, int port) {
        ensure_sockets();
        sock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ == kInvalidSocket)
            throw std::runtime_error("OSC sender: socket() failed");
        make_sockaddr(host, port, dest_);
    }

    ~OscSender() {
        if (sock_ != kInvalidSocket) close_socket(sock_);
    }

    void send_message(const std::string &address, nb::list args) {
        char buf[512];
        uint32_t n;
        size_t k = args.size();
        if (k == 0) {
            n = tosc_writeMessage(buf, sizeof(buf), address.c_str(), "");
        } else if (k == 2) {
            int i = nb::cast<int>(args[0]);
            float f = nb::cast<float>(args[1]);
            n = tosc_writeMessage(buf, sizeof(buf), address.c_str(), "if", i, f);
        } else {
            throw std::runtime_error(
                "_OscSender supports [] or [int, float] (softcut reply protocol)");
        }
        ::sendto(sock_, buf, static_cast<int>(n), 0,
                 reinterpret_cast<sockaddr *>(&dest_), sizeof(dest_));
    }

private:
    socket_t sock_ = kInvalidSocket;
    sockaddr_in dest_;
};

// Native, GIL-free phase poll: the outbound analogue of OscReceiver's fast path
// and a drop-in for the Python _PhasePoll. A background thread reads each voice's
// quantized phase (a plain C++ read, no GIL -- the same off-thread read the
// Python poll already does) and, when it changes, sends
// `/poll/softcut/phase <voice:int> <phase:float>` to the reply address in C.
// With this, nothing on the softcut control path -- inbound or outbound -- needs
// the interpreter, so a busy Python thread can no longer delay or jitter it.
class OscPhasePoll {
public:
    OscPhasePoll(Engine *engine, const std::string &host, int port, double period)
        : engine_(engine), period_(period) {
        ensure_sockets();
        sock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ == kInvalidSocket)
            throw std::runtime_error("OSC phase poll: socket() failed");
        make_sockaddr(host, port, dest_);
        const size_t n = engine_ ? static_cast<size_t>(engine_->n_voices) : 0;
        last_.assign(n, std::numeric_limits<double>::quiet_NaN());
    }

    ~OscPhasePoll() {
        stop();
        if (sock_ != kInvalidSocket) close_socket(sock_);
    }

    void start() {
        if (running_.exchange(true)) return;
        thread_ = std::thread([this] { run(); });
    }

    void stop() {
        if (!running_.exchange(false)) return;
        cv_.notify_all();  // wake the poll thread out of its wait_for
        if (thread_.joinable()) {
            nb::gil_scoped_release rel;  // the poll thread takes no GIL, but let
            thread_.join();              // other Python threads run during join
        }
    }

    bool running() const { return running_.load(); }

    // Clear change-detection state so the next poll_once() re-emits every voice.
    // Used by tests to force a resend (robust to a dropped reply datagram).
    void reset() {
        std::lock_guard<std::mutex> lk(mtx_);
        std::fill(last_.begin(), last_.end(),
                  std::numeric_limits<double>::quiet_NaN());
    }

    // One scan: send a message for each voice whose quantized phase changed.
    void poll_once() {
        std::lock_guard<std::mutex> lk(mtx_);
        scan_locked();
    }

private:
    void run() {
        std::unique_lock<std::mutex> lk(mtx_);
        while (running_.load()) {
            scan_locked();
            cv_.wait_for(lk, std::chrono::duration<double>(period_),
                         [this] { return !running_.load(); });
        }
    }

    // Caller holds mtx_. Sends are serialized with poll_once()/reset() this way.
    void scan_locked() {
        if (engine_ == nullptr) return;
        const int n = engine_->n_voices;
        for (int i = 0; i < n; ++i) {
            const double phase = engine_->voices[static_cast<size_t>(i)]->v.getQuantPhase();
            if (phase != last_[static_cast<size_t>(i)]) {  // exact: quantized steps
                last_[static_cast<size_t>(i)] = phase;
                send(i, static_cast<float>(phase));
            }
        }
    }

    void send(int voice, float phase) {
        char buf[64];
        uint32_t n = tosc_writeMessage(buf, sizeof(buf), "/poll/softcut/phase",
                                       "if", voice, phase);
        ::sendto(sock_, buf, static_cast<int>(n), 0,
                 reinterpret_cast<sockaddr *>(&dest_), sizeof(dest_));
    }

    Engine *engine_ = nullptr;
    double period_;
    std::vector<double> last_;  // last-sent quantized phase per voice (NaN = none)
    socket_t sock_ = kInvalidSocket;
    sockaddr_in dest_;
    std::atomic<bool> running_{false};
    std::thread thread_;
    std::mutex mtx_;
    std::condition_variable cv_;
};
#endif  // SOFTCUT_TINYOSC

}  // namespace

// float property: mirror field (read immediately) + DSP setter routed through
// the command queue when an engine is running. The mirror is atomic (the native
// OSC path writes it off the GIL), so read/write it explicitly.
#define FPROP(name, field, setter)                                       \
    def_prop_rw(                                                          \
        name,                                                            \
        [](Voice &s) { return s.field.load(std::memory_order_relaxed); },\
        [](Voice &s, float x) {                                          \
            s.field.store(x, std::memory_order_relaxed);                 \
            Voice *p = &s;                                                \
            s.dsp_apply([p, x] { p->v.setter(x); });                     \
            probe_stamp(x);                                              \
        })

// bool property
#define BPROP(name, field, setter)                                       \
    def_prop_rw(                                                          \
        name,                                                            \
        [](Voice &s) { return s.field.load(std::memory_order_relaxed); },\
        [](Voice &s, bool x) {                                           \
            s.field.store(x, std::memory_order_relaxed);                 \
            Voice *p = &s;                                                \
            s.dsp_apply([p, x] { p->v.setter(x); });                     \
            probe_stamp(x ? 1.0f : 0.0f);                                \
        })

NB_MODULE(_core, m) {
    m.doc() = "Python binding for softcut-lib's per-voice DSP engine.";

    nb::class_<Voice>(m, "Voice",
        "A single softcut DSP voice: a crossfading read/write head over a "
        "caller-owned audio buffer, with rate, loop, record/play and "
        "pre/post filtering. Buffers are numpy float32 arrays you own; assign "
        "the same array to several voices to share it.")
        .def(nb::init<float>(), "sample_rate"_a = 48000.0f)

        .def_prop_rw("sample_rate",
            [](Voice &s) { return s.sample_rate; },
            [](Voice &s, float hz) { s.set_sample_rate(hz); },
            "Sample rate in Hz.")

        .def_prop_rw("buffer",
            [](Voice &s) { return s.buffer_ref; },
            [](Voice &s, nb::object a) { s.set_buffer(std::move(a)); },
            "The voice's audio buffer as a 1-D float32 numpy array. The voice "
            "reads from and records into this memory in place.")

        // transport / loop
        .FPROP("rate", rate_, setRate)
        .FPROP("loop_start", loop_start_, setLoopStart)
        .FPROP("loop_end", loop_end_, setLoopEnd)
        .BPROP("loop", loop_, setLoopFlag)
        .FPROP("fade_time", fade_time_, setFadeTime)

        // record / play
        .FPROP("rec_level", rec_level_, setRecLevel)
        .FPROP("pre_level", pre_level_, setPreLevel)
        .BPROP("rec", rec_, setRecFlag)
        .BPROP("rec_once", rec_once_, setRecOnceFlag)
        .BPROP("play", play_, setPlayFlag)
        .FPROP("rec_offset", rec_offset_, setRecOffset)

        // slew
        .FPROP("rec_pre_slew_time", rec_pre_slew_time_, setRecPreSlewTime)
        .FPROP("rate_slew_time", rate_slew_time_, setRateSlewTime)

        // phase
        .FPROP("phase_quant", phase_quant_, setPhaseQuant)
        .FPROP("phase_offset", phase_offset_, setPhaseOffset)

        // pre filter
        .FPROP("pre_filter_fc", pre_filter_fc_, setPreFilterFc)
        .FPROP("pre_filter_rq", pre_filter_rq_, setPreFilterRq)
        .FPROP("pre_filter_lp", pre_filter_lp_, setPreFilterLp)
        .FPROP("pre_filter_hp", pre_filter_hp_, setPreFilterHp)
        .FPROP("pre_filter_bp", pre_filter_bp_, setPreFilterBp)
        .FPROP("pre_filter_br", pre_filter_br_, setPreFilterBr)
        .FPROP("pre_filter_dry", pre_filter_dry_, setPreFilterDry)
        .FPROP("pre_filter_fc_mod", pre_filter_fc_mod_, setPreFilterFcMod)

        // post filter
        .FPROP("post_filter_fc", post_filter_fc_, setPostFilterFc)
        .FPROP("post_filter_rq", post_filter_rq_, setPostFilterRq)
        .FPROP("post_filter_lp", post_filter_lp_, setPostFilterLp)
        .FPROP("post_filter_hp", post_filter_hp_, setPostFilterHp)
        .FPROP("post_filter_bp", post_filter_bp_, setPostFilterBp)
        .FPROP("post_filter_br", post_filter_br_, setPostFilterBr)
        .FPROP("post_filter_dry", post_filter_dry_, setPostFilterDry)

        // engine mix (used by Engine; ignored by standalone Voice.process)
        .def_prop_ro("dropped_commands",
            [](Voice &s) { return s.dropped_commands_.load(std::memory_order_relaxed); },
            "Control changes lost because the audio thread's command queue stayed "
            "full. Nonzero means the engine is being driven faster than it drains; "
            "those changes were dropped rather than applied off-thread.")
        .def_prop_rw("level",
            [](Voice &s) { return s.level_.load(std::memory_order_relaxed); },
            [](Voice &s, float x) { s.level_.store(x, std::memory_order_relaxed); },
            "Output level (linear gain) applied when mixed by an Engine.")
        .def_prop_rw("pan",
            [](Voice &s) { return s.pan_.load(std::memory_order_relaxed); },
            [](Voice &s, float x) { s.pan_.store(x, std::memory_order_relaxed); },
            "Stereo pan, -1 (left) to +1 (right), applied by an Engine mixer.")
        .def_prop_rw("input_gain",
            [](Voice &s) { return s.input_gain_.load(std::memory_order_relaxed); },
            [](Voice &s, float x) { s.input_gain_.store(x, std::memory_order_relaxed); },
            "Gain applied to the engine's external input fed into this voice.")

        // read-only state
        .def_prop_ro("position", [](Voice &s) { return s.v.getActivePosition(); },
            "Current play/record head position in seconds (audio-thread view).")
        .def_prop_ro("saved_position", [](Voice &s) { return s.v.getSavedPosition(); },
            "Head position in seconds, updated once per processed block; safe "
            "to read from any thread.")
        .def_prop_ro("quant_phase", [](Voice &s) { return s.v.getQuantPhase(); },
            "Quantized phase (in units of phase_quant).")

        // actions
        .def("process", &Voice::process, "input"_a, "out"_a,
            "Process one mono block of any 1-D C-contiguous float32 buffer into "
            "`out`, which is returned. Use softcut.Voice.process, which supplies "
            "`out` when you do not.")
        .def("cut_to", [](Voice &s, float sec) {
                Voice *p = &s;
                s.dsp_apply([p, sec] { p->v.cutToPos(sec); });
            }, "sec"_a,
            "Jump the head to the given position in seconds (with a crossfade).")
        .def("stop", [](Voice &s) {
                Voice *p = &s;
                s.dsp_apply([p] { p->v.stop(); });
            },
            "Immediately stop both subheads.")
        .def("reset", [](Voice &s) {
                Voice *p = &s;
                s.dsp_apply([p] { p->v.reset(); });
                // The DSP reset happens on the audio thread when one is running;
                // the mirrors are Python-side state, so they are restored here.
                s.reset_params();
            },
            "Reset the voice's DSP state and parameter read-backs to defaults.");

    // Low-level realtime host. The Python-facing facade (softcut.Engine) wraps
    // this and owns the Voice objects; keep_alive ties their lifetime to the
    // engine so the audio thread never sees a freed voice.
    nb::class_<Engine>(m, "_Engine",
        "Low-level multi-voice realtime host over a miniaudio device. Use the "
        "softcut.Engine facade instead.")
        .def(nb::init<std::vector<Voice *>, float, int, bool, int, int, int>(),
            "voices"_a, "sample_rate"_a, "block_size"_a, "duplex"_a, "out_channels"_a,
            "output_device"_a, "input_device"_a,
            nb::keep_alive<1, 2>())
        .def("start", &Engine::start, nb::call_guard<nb::gil_scoped_release>(),
            "Open (if needed) and start the audio device. Non-blocking.")
        .def("stop", &Engine::stop, nb::call_guard<nb::gil_scoped_release>(),
            "Stop the audio device.")
        .def("render", &Engine::render, "input"_a, "out"_a,
            "Offline: process a 1-D float32 mono input buffer through all voices, "
            "writing interleaved frames into `out` (a flat buffer of "
            "n*out_channels floats), which is returned. Do not call while the "
            "device is running.")
        .def("set_feedback", &Engine::set_feedback, "src"_a, "dst"_a, "amount"_a,
            "Set the feedback gain from voice src's output into voice dst's input.")
        .def("get_feedback", &Engine::get_feedback, "src"_a, "dst"_a,
            "Get the feedback gain from voice src into voice dst.")
        .def_prop_ro("running", [](Engine &e) { return e.device_started; },
            "True while the audio device is started.")
        .def_prop_ro("block_size", [](Engine &e) { return e.block_size; })
        .def_prop_ro("out_channels", [](Engine &e) { return e.out_channels; })
        .def_prop_ro("duplex", [](Engine &e) { return e.duplex; });

    m.def("list_devices", &list_audio_devices,
        "List the system audio devices as dicts with keys "
        "index/name/type/is_default.");

    // --- buffer arithmetic (shared/buffer_ops.hpp) ---
    // The sample-level primitives every buffer operation reduces to, shared with
    // the standalone server so the two hosts cannot drift. They take any
    // C-contiguous float32 buffer (numpy array, array.array, memoryview), write
    // in place, and hold no GIL while looping -- so a long buffer edit does not
    // starve anything else in the interpreter.
    m.def("_buffer_apply",
        [](BufferArray dst, int64_t start, std::optional<BufferArray> src,
           float preserve, float mix, int64_t fade, int64_t count) {
            float *d = dst.data();
            const long dn = static_cast<long>(dst.shape(0));
            const float *s = src ? src->data() : nullptr;
            long n = count >= 0
                         ? static_cast<long>(count)
                         : (src ? static_cast<long>(src->shape(0)) : 0);
            {
                nb::gil_scoped_release nogil;
                scsh::apply_to_buffer(d, dn, static_cast<long>(start), s, n,
                                      preserve, mix, static_cast<long>(fade));
            }
        },
        "dst"_a, "start"_a, "src"_a.none(), "preserve"_a = 0.0f, "mix"_a = 1.0f,
        "fade"_a = 0, "count"_a = -1,
        "Blended in-place write into dst at frame `start`: "
        "dst = dst*(1-env) + (dst*preserve + src*mix)*env, where env is a linear "
        "edge fade over `fade` frames at each end. src=None writes silence "
        "(a clear), in which case `count` gives the length. A negative `start` "
        "trims the head of src. Read, copy and clear are all this one operation.");

    // --- PCM conversion ---
    // The sample-format half of WAV I/O, which is the only part of it that is
    // per-sample work. Container parsing stays in Python on the stdlib `wave`
    // module, so no WAV decoder is vendored into the extension. Not in
    // shared/: the standalone server has dr_wav and no use for these.
    m.def("_pcm_decode",
        [](BufferArray out, nb::ndarray<const uint8_t, nb::ndim<1>, nb::c_contig> raw,
           int width) {
            float *o = out.data();
            const uint8_t *r = raw.data();
            const int64_t n = std::min<int64_t>(
                static_cast<int64_t>(out.shape(0)),
                static_cast<int64_t>(raw.shape(0)) / width);
            nb::gil_scoped_release nogil;
            switch (width) {
                case 1:  // unsigned 8-bit, midpoint 128
                    for (int64_t i = 0; i < n; ++i)
                        o[i] = (static_cast<float>(r[i]) - 128.0f) / 128.0f;
                    break;
                case 2:
                    for (int64_t i = 0; i < n; ++i) {
                        const int16_t v = static_cast<int16_t>(
                            static_cast<uint16_t>(r[2 * i]) |
                            (static_cast<uint16_t>(r[2 * i + 1]) << 8));
                        o[i] = static_cast<float>(v) / 32768.0f;
                    }
                    break;
                case 3: {  // packed little-endian signed 24-bit
                    for (int64_t i = 0; i < n; ++i) {
                        int32_t v = static_cast<int32_t>(r[3 * i]) |
                                    (static_cast<int32_t>(r[3 * i + 1]) << 8) |
                                    (static_cast<int32_t>(r[3 * i + 2]) << 16);
                        if (v & 0x800000) v -= 0x1000000;
                        o[i] = static_cast<float>(v) / 8388608.0f;
                    }
                    break;
                }
                case 4:
                    for (int64_t i = 0; i < n; ++i) {
                        const int32_t v = static_cast<int32_t>(
                            static_cast<uint32_t>(r[4 * i]) |
                            (static_cast<uint32_t>(r[4 * i + 1]) << 8) |
                            (static_cast<uint32_t>(r[4 * i + 2]) << 16) |
                            (static_cast<uint32_t>(r[4 * i + 3]) << 24));
                        o[i] = static_cast<float>(v) / 2147483648.0f;
                    }
                    break;
                default:
                    break;  // rejected in Python, where the error can say why
            }
        },
        "out"_a, "raw"_a, "width"_a,
        "Decode little-endian integer PCM (1/2/3/4 bytes per sample, 8-bit "
        "unsigned and the rest signed) into float32 in [-1, 1].");

    m.def("_pcm_encode_s16",
        [](BufferArray src) {
            const int64_t n = static_cast<int64_t>(src.shape(0));
            std::vector<uint8_t> out(static_cast<size_t>(n) * 2);
            {
                const float *s = src.data();
                uint8_t *o = out.data();
                nb::gil_scoped_release nogil;
                for (int64_t i = 0; i < n; ++i) {
                    const uint16_t v = static_cast<uint16_t>(scsh::float_to_s16(s[i]));
                    o[2 * i] = static_cast<uint8_t>(v & 0xFF);
                    o[2 * i + 1] = static_cast<uint8_t>((v >> 8) & 0xFF);
                }
            }
            return nb::bytes(out.data(), out.size());
        },
        "src"_a,
        "Quantize float32 to little-endian signed 16-bit PCM bytes, clipping "
        "to [-1, 1] first.");

    m.def("_buffer_extract_channel",
        [](BufferArray out, BufferArray interleaved, int channels, int col,
           int64_t start, int64_t total) {
            float *o = out.data();
            const long n = static_cast<long>(out.shape(0));
            const float *d = interleaved.data();
            const long tot = total >= 0
                                 ? static_cast<long>(total)
                                 : static_cast<long>(interleaved.shape(0)) / channels;
            {
                nb::gil_scoped_release nogil;
                scsh::extract_channel_into(o, d, static_cast<unsigned>(channels), col,
                                           static_cast<long>(start), n, tot);
            }
        },
        "out"_a, "interleaved"_a, "channels"_a, "col"_a, "start"_a = 0, "total"_a = -1,
        "De-interleave channel `col` of interleaved frame data into `out`, "
        "starting at frame `start`. Frames outside the source read as silence, "
        "so a range overrunning either end is padded rather than refused.");

    // --- apply-latency probe (benchmark only; compile-time gated) ---
    // Present only when built with SOFTCUT_ENABLE_BENCH_PROBE=ON. It isolates the
    // OSC-dispatch cost from a GIL-gated Python observer: timestamp the send with
    // _steady_clock_ns(), poll _bench_probe_applied(value) until it reports the
    // apply, then read _bench_last_apply_ns() -- the apply instant recorded in C
    // on whichever thread applied it. Both timestamps share one steady_clock, so
    // their difference excludes the observer's own GIL wait. HAVE_BENCH_PROBE lets
    // benchmarks/osc_jitter.py detect the build. See that file.
#ifdef SOFTCUT_BENCH_PROBE
    m.attr("HAVE_BENCH_PROBE") = true;
    m.def("_steady_clock_ns", []() {
        return static_cast<int64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count());
    }, "Current steady_clock value in nanoseconds (matches the apply probe).");
    m.def("_bench_probe_applied", [](float value) {
        uint32_t want;
        std::memcpy(&want, &value, sizeof(want));
        return g_probe_val.load(std::memory_order_acquire) == want;
    }, "value"_a,
        "True once the probe has recorded an apply of this exact value; "
        "acquire-loads so a following _bench_last_apply_ns() is its timestamp.");
    m.def("_bench_last_apply_ns", []() {
        return g_probe_ns.load(std::memory_order_relaxed);
    }, "steady_clock ns of the last probed apply; read only after "
        "_bench_probe_applied() is true for your value.");
#else
    m.attr("HAVE_BENCH_PROBE") = false;
#endif

    // Whether the native tinyosc OSC codec was compiled in (CMake option
    // SOFTCUT_ENABLE_TINYOSC). Lets Python pick the native path or fall back to
    // the pure-Python (python-osc) option.
#ifdef SOFTCUT_TINYOSC
    m.attr("HAVE_TINYOSC") = true;

    // Vendored tinyosc smoke test: round-trip an OSC message through the wire
    // codec (serialize then parse) entirely in C, returning the decoded args.
    // Confirms tinyosc is compiled and linked into the extension; also a hook
    // for the OSC-server work to build on. Returns (address, int, float, str).
    m.def("_osc_selftest", []() {
        char buf[64];
        uint32_t n = tosc_writeMessage(buf, sizeof(buf), "/sc/test", "ifs",
                                       42, 3.5f, "hi");
        tosc_message msg;
        if (tosc_parseMessage(&msg, buf, static_cast<int>(n)) != 0)
            throw std::runtime_error("tinyosc parse failed");
        const char *address = tosc_getAddress(&msg);
        int32_t i = tosc_getNextInt32(&msg);
        float f = tosc_getNextFloat(&msg);
        const char *s = tosc_getNextString(&msg);
        return nb::make_tuple(std::string(address), i, f, std::string(s));
    }, "Round-trip an OSC message through the vendored tinyosc codec.");

    nb::class_<OscReceiver>(m, "_OscReceiver",
        "Native UDP OSC receiver (tinyosc). Per-voice /set/param/cut/* messages "
        "are dispatched in C without the GIL when an engine is given; every "
        "other address is delivered to the Python callback as (address, args).")
        .def(nb::init<const std::string &, int, nb::callable, Engine *>(),
            "host"_a, "port"_a, "callback"_a, "engine"_a.none() = nullptr,
            nb::keep_alive<1, 5>())
        .def("start", &OscReceiver::start,
            "Begin receiving on a background thread.")
        .def("stop", &OscReceiver::stop,
            "Stop receiving and join the thread.")
        .def_prop_ro("port", &OscReceiver::port,
            "The actual bound UDP port (resolves an ephemeral port 0).");

    nb::class_<OscSender>(m, "_OscSender",
        "Native UDP OSC sender (tinyosc) for the softcut reply protocol.")
        .def(nb::init<const std::string &, int>(), "host"_a, "port"_a)
        .def("send_message", &OscSender::send_message, "address"_a, "args"_a,
            "Send an OSC message; supports [] or [int, float] arguments.");

    nb::class_<OscPhasePoll>(m, "_OscPhasePoll",
        "Native GIL-free phase poll (tinyosc): a background thread reads each "
        "voice's quantized phase and sends /poll/softcut/phase in C, so the "
        "softcut reply path never touches the interpreter.")
        .def(nb::init<Engine *, const std::string &, int, double>(),
            "engine"_a, "host"_a, "port"_a, "period"_a = 0.01,
            nb::keep_alive<1, 2>())
        .def("start", &OscPhasePoll::start,
            "Begin polling on a background thread.")
        .def("stop", &OscPhasePoll::stop, "Stop polling and join the thread.")
        .def("poll_once", &OscPhasePoll::poll_once,
            "Scan all voices once, emitting a message for each changed phase.")
        .def("reset", &OscPhasePoll::reset,
            "Reset change detection so the next poll re-emits every voice.")
        .def_prop_ro("running", &OscPhasePoll::running,
            "True while the background poll thread is active.");

    // --- benchmark / prototype hooks (dev only) ---
    // Cost of the proposed native fast path: parse + address match + post to
    // the command queue, N times, entirely in C with the GIL released.
    m.def("_bench_native_dispatch", [](Voice &v, int n) {
        char buf[64];
        uint32_t len = tosc_writeMessage(buf, sizeof(buf), "/set/param/cut/rate",
                                         "if", 0, 1.0f);
        double secs;
        {
            nb::gil_scoped_release rel;
            auto t0 = std::chrono::steady_clock::now();
            for (int k = 0; k < n; ++k) {
                tosc_message msg;
                if (tosc_parseMessage(&msg, buf, static_cast<int>(len)) != 0) continue;
                const char *addr = tosc_getAddress(&msg);
                int voice = tosc_getNextInt32(&msg);
                float val = tosc_getNextFloat(&msg);
                (void) voice;  // one voice in this microbench
                if (std::strcmp(addr, "/set/param/cut/rate") == 0) {
                    Voice *p = &v;
                    v.rate_ = val;
                    v.dsp_apply([p, val] { p->v.setRate(val); });
                }
            }
            auto t1 = std::chrono::steady_clock::now();
            secs = std::chrono::duration<double>(t1 - t0).count();
        }
        return secs;
    }, "voice"_a, "n"_a,
        "Benchmark N native OSC rate dispatches (GIL released); returns seconds.");
#else
    m.attr("HAVE_TINYOSC") = false;
#endif
}
