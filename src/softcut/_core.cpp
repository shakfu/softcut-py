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
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <array>
#include <atomic>
#include <algorithm>
#include <cmath>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

#include "softcut/Voice.h"
#include "softcut/Types.h"

#include "miniaudio.h"

// Native OSC codec, compiled in only when the CMake option SOFTCUT_ENABLE_TINYOSC
// is set. The default build ships without it; the pure-Python option (python-osc)
// covers OSC at runtime instead.
#ifdef SOFTCUT_TINYOSC
#include "tinyosc.h"
#include <thread>
#include <chrono>
#include <cstring>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/time.h>
#include <unistd.h>
#endif
#endif

namespace nb = nanobind;
using namespace nb::literals;

// 1-D, C-contiguous, float32, CPU array (softcut::sample_t == float).
using BufferArray = nb::ndarray<float, nb::ndim<1>, nb::c_contig, nb::device::cpu>;

namespace {

// Single-producer / single-consumer lock-free ring of small commands. The
// control thread (Python, GIL held) pushes; the audio thread drains at each
// block. Commands are tiny lambdas (a Voice* plus a scalar) that fit in
// std::function's small-buffer storage, so push/pop/call never touch the heap.
//
// The native OSC receiver (SOFTCUT_TINYOSC) is not a second producer: it
// dispatches under the GIL, so its queue pushes serialize with the Python
// control thread on the GIL, and this stays single-producer. A GIL-free native
// dispatch path would need a multi-producer queue -- see benchmarks/osc_dispatch.py
// for why that path is not worth building (it is transport-bound, not
// dispatch-bound).
struct CommandQueue {
    static constexpr size_t CAP = 4096;  // power of two
    std::array<std::function<void()>, CAP> buf;
    std::atomic<size_t> head{0};  // producer writes here
    std::atomic<size_t> tail{0};  // consumer reads here

    bool push(const std::function<void()> &fn) {
        const size_t h = head.load(std::memory_order_relaxed);
        const size_t n = (h + 1) & (CAP - 1);
        if (n == tail.load(std::memory_order_acquire)) return false;  // full
        buf[h] = fn;
        head.store(n, std::memory_order_release);
        return true;
    }

    bool pop(std::function<void()> &out) {
        const size_t t = tail.load(std::memory_order_relaxed);
        if (t == head.load(std::memory_order_acquire)) return false;  // empty
        out = std::move(buf[t]);
        tail.store((t + 1) & (CAP - 1), std::memory_order_release);
        return true;
    }
};

struct Voice {
    softcut::Voice v;
    nb::object buffer_ref;  // keepalive for the numpy array passed to setBuffer
    float sample_rate = 48000.0f;

    // Engine mix parameters (not softcut params): read by Engine's mixer.
    // level is a linear gain; pan is -1 (left) .. 0 (center) .. +1 (right).
    // input_gain scales the engine's external input fed into this voice.
    float level_ = 1.0f;
    float pan_ = 0.0f;
    float input_gain_ = 1.0f;

    // Set when the voice is hosted by an Engine. While the engine is running,
    // DSP setters are applied on the audio thread via the command queue.
    CommandQueue *cmd_queue_ = nullptr;
    std::atomic<bool> *engine_running_ = nullptr;

    // Apply a softcut DSP change now, or defer it to the audio thread if an
    // engine is running (so the audio thread never reads a half-written param).
    void dsp_apply(std::function<void()> fn) {
        if (engine_running_ != nullptr &&
            engine_running_->load(std::memory_order_acquire) &&
            cmd_queue_->push(fn)) {
            return;
        }
        fn();
    }

    // Mirrors of write-only parameters, seeded with softcut::Voice::reset()
    // defaults so getters are meaningful before the first set.
    float rate_ = 1.0f;
    float loop_start_ = 0.0f;
    float loop_end_ = 0.0f;
    bool loop_ = false;
    bool rec_ = false;
    bool rec_once_ = false;
    bool play_ = false;
    float fade_time_ = 0.01f;
    float rec_level_ = 0.0f;
    float pre_level_ = 0.0f;
    float rec_offset_ = -8.0f / 48000.0f;
    float rec_pre_slew_time_ = 0.001f;
    float rate_slew_time_ = 0.001f;
    float phase_quant_ = 0.0f;
    float phase_offset_ = 0.0f;

    // Pre filter
    float pre_filter_fc_ = 16000.0f;
    float pre_filter_rq_ = 4.0f;
    float pre_filter_lp_ = 1.0f;
    float pre_filter_hp_ = 0.0f;
    float pre_filter_bp_ = 0.0f;
    float pre_filter_br_ = 0.0f;
    float pre_filter_dry_ = 0.0f;
    float pre_filter_fc_mod_ = 1.0f;

    // Post filter
    float post_filter_fc_ = 12000.0f;
    float post_filter_rq_ = 4.0f;
    float post_filter_lp_ = 0.0f;
    float post_filter_hp_ = 0.0f;
    float post_filter_bp_ = 0.0f;
    float post_filter_br_ = 0.0f;
    float post_filter_dry_ = 1.0f;

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

    // Process one mono block: float32 input -> newly-allocated float32 output.
    nb::object process(nb::object input) {
        BufferArray in = nb::cast<BufferArray>(input);
        size_t n = in.shape(0);

        float *out_data = new float[n == 0 ? 1 : n];
        nb::capsule owner(out_data, [](void *p) noexcept { delete[] static_cast<float *>(p); });

        v.processBlockMono(in.data(), out_data, static_cast<int>(n));

        return nb::cast(nb::ndarray<nb::numpy, float, nb::ndim<1>>(out_data, {n}, owner));
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
    std::vector<float> fb;        // feedback matrix, fb[src*n_voices + dst]
    std::vector<float> prev_out;  // last block's per-voice output (n*block_size)
    std::vector<float> cur_out;   // this block's per-voice output (n*block_size)

    CommandQueue queue;
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
        fb.assign(static_cast<size_t>(n_voices) * n_voices, 0.0f);
        prev_out.assign(static_cast<size_t>(n_voices) * block_size, 0.0f);
        cur_out.assign(static_cast<size_t>(n_voices) * block_size, 0.0f);
        for (Voice *vp : voices) {
            vp->set_sample_rate(sr);
            vp->cmd_queue_ = &queue;
            vp->engine_running_ = &running;
        }
    }

    ~Engine() {
        running.store(false, std::memory_order_release);
        if (device_inited) ma_device_uninit(&device);  // joins the audio thread
        if (context_inited) ma_context_uninit(&context);
        for (Voice *vp : voices) {  // never leave dangling pointers into us
            vp->cmd_queue_ = nullptr;
            vp->engine_running_ = nullptr;
        }
    }

    // Drain and apply any queued parameter changes. Runs on the audio thread at
    // each block, and on the control thread once the device has stopped.
    void drain_commands() {
        std::function<void()> fn;
        while (queue.pop(fn)) fn();
    }

    // Process up to block_size frames of mono input into interleaved stereo
    // output, applying per-voice input gain and voice->voice feedback (delayed
    // by one block). GIL-free and allocation-free.
    void process_core(const float *ext_in, float *out, int frames) {
        for (int i = 0; i < frames * out_channels; ++i) out[i] = 0.0f;
        for (int dst = 0; dst < n_voices; ++dst) {
            Voice *vp = voices[dst];
            float *vin = voice_in.data();
            const float ig = vp->input_gain_;
            for (int i = 0; i < frames; ++i) vin[i] = ext_in ? ext_in[i] * ig : 0.0f;
            for (int src = 0; src < n_voices; ++src) {
                const float g = fb[static_cast<size_t>(src) * n_voices + dst];
                if (g == 0.0f) continue;
                const float *po = prev_out.data() + static_cast<size_t>(src) * block_size;
                for (int i = 0; i < frames; ++i) vin[i] += po[i] * g;
            }
            float *o = cur_out.data() + static_cast<size_t>(dst) * block_size;
            vp->v.processBlockMono(vin, o, frames);
            // equal-power pan: pan -1..1 -> angle 0..pi/2
            const float level = vp->level_;
            const float theta = (vp->pan_ * 0.5f + 0.5f) * 1.5707963267948966f;
            const float gl = level * std::cos(theta);
            const float gr = level * std::sin(theta);
            for (int f = 0; f < frames; ++f) {
                out[f * out_channels + 0] += o[f] * gl;
                if (out_channels > 1) out[f * out_channels + 1] += o[f] * gr;
            }
        }
        std::swap(prev_out, cur_out);  // this block's outputs feed the next
    }

    void set_feedback(int src, int dst, float amount) {
        if (src < 0 || src >= n_voices || dst < 0 || dst >= n_voices)
            throw std::out_of_range("voice index out of range");
        fb[static_cast<size_t>(src) * n_voices + dst] = amount;
    }

    float get_feedback(int src, int dst) const {
        if (src < 0 || src >= n_voices || dst < 0 || dst >= n_voices)
            throw std::out_of_range("voice index out of range");
        return fb[static_cast<size_t>(src) * n_voices + dst];
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

    // Offline: mono input (n,) -> interleaved stereo output (n, out_channels).
    nb::object render(nb::object input) {
        BufferArray in = nb::cast<BufferArray>(input);
        size_t n = in.shape(0);
        const float *inp = in.data();

        size_t total = (n == 0 ? 1 : n) * static_cast<size_t>(out_channels);
        float *out = new float[total];
        nb::capsule owner(out, [](void *p) noexcept { delete[] static_cast<float *>(p); });

        size_t done = 0;
        while (done < n) {
            int chunk = static_cast<int>(std::min(static_cast<size_t>(block_size), n - done));
            process_core(inp + done, out + done * out_channels, chunk);
            done += static_cast<size_t>(chunk);
        }
        return nb::cast(nb::ndarray<nb::numpy, float, nb::ndim<2>>(
            out, {n, static_cast<size_t>(out_channels)}, owner));
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
                ma_device_info *playback_infos, *capture_infos;
                ma_uint32 n_playback, n_capture;
                if (ma_context_get_devices(&context, &playback_infos, &n_playback,
                                           &capture_infos, &n_capture) != MA_SUCCESS)
                    throw std::runtime_error("failed to enumerate audio devices");
                if (output_device_index >= 0) {
                    if (output_device_index >= static_cast<int>(n_playback))
                        throw std::invalid_argument("output_device index out of range");
                    playback_id = playback_infos[output_device_index].id;
                    p_playback_id = &playback_id;
                }
                if (duplex && input_device_index >= 0) {
                    if (input_device_index >= static_cast<int>(n_capture))
                        throw std::invalid_argument("input_device index out of range");
                    capture_id = capture_infos[input_device_index].id;
                    p_capture_id = &capture_id;
                }
            }

            ma_device_config cfg = ma_device_config_init(
                duplex ? ma_device_type_duplex : ma_device_type_playback);
            cfg.sampleRate = static_cast<ma_uint32>(sample_rate);
            cfg.periodSizeInFrames = static_cast<ma_uint32>(block_size);
            cfg.playback.format = ma_format_f32;
            cfg.playback.channels = static_cast<ma_uint32>(out_channels);
            cfg.playback.pDeviceID = p_playback_id;
            if (duplex) {
                cfg.capture.format = ma_format_f32;
                cfg.capture.channels = 1;  // miniaudio sums device channels to mono
                cfg.capture.pDeviceID = p_capture_id;
            }
            cfg.dataCallback = &Engine::data_callback;
            cfg.pUserData = this;
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

#ifdef _WIN32
using socket_t = SOCKET;
static const socket_t kInvalidSocket = INVALID_SOCKET;
static void close_socket(socket_t s) { closesocket(s); }
static void ensure_sockets() {
    static bool done = false;
    if (!done) {
        WSADATA w;
        WSAStartup(MAKEWORD(2, 2), &w);
        done = true;
    }
}
static void set_recv_timeout_ms(socket_t s, int ms) {
    DWORD tv = static_cast<DWORD>(ms);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char *>(&tv),
               sizeof(tv));
}
#else
using socket_t = int;
static const socket_t kInvalidSocket = -1;
static void close_socket(socket_t s) { ::close(s); }
static void ensure_sockets() {}
static void set_recv_timeout_ms(socket_t s, int ms) {
    struct timeval tv;
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}
#endif

static void make_sockaddr(const std::string &host, int port, sockaddr_in &addr) {
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (host.empty() || host == "0.0.0.0" ||
        inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        addr.sin_addr.s_addr = INADDR_ANY;
    }
}

// Receives OSC over UDP and delivers each parsed message to a Python callback
// as (address: str, args: list). Args cover the i/f/s/h/d/T/F type tags used by
// the softcut protocol; an unknown tag stops parsing that message.
class OscReceiver {
public:
    OscReceiver(const std::string &host, int port, nb::callable cb)
        : callback_(std::move(cb)) {
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
                while (tosc_getNextMessage(&b, &m)) deliver(&m);
            } else {
                tosc_message m;
                if (tosc_parseMessage(&m, buf.data(), n) == 0) deliver(&m);
            }
        }
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
#endif  // SOFTCUT_TINYOSC

}  // namespace

// float property: mirror field (read immediately) + DSP setter routed through
// the command queue when an engine is running.
#define FPROP(name, field, setter)                                       \
    def_prop_rw(                                                          \
        name, [](Voice &s) { return s.field; },                          \
        [](Voice &s, float x) {                                          \
            s.field = x;                                                  \
            Voice *p = &s;                                                \
            s.dsp_apply([p, x] { p->v.setter(x); });                     \
        })

// bool property
#define BPROP(name, field, setter)                                       \
    def_prop_rw(                                                          \
        name, [](Voice &s) { return s.field; },                          \
        [](Voice &s, bool x) {                                           \
            s.field = x;                                                  \
            Voice *p = &s;                                                \
            s.dsp_apply([p, x] { p->v.setter(x); });                     \
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
        .def_prop_rw("level",
            [](Voice &s) { return s.level_; },
            [](Voice &s, float x) { s.level_ = x; },
            "Output level (linear gain) applied when mixed by an Engine.")
        .def_prop_rw("pan",
            [](Voice &s) { return s.pan_; },
            [](Voice &s, float x) { s.pan_ = x; },
            "Stereo pan, -1 (left) to +1 (right), applied by an Engine mixer.")
        .def_prop_rw("input_gain",
            [](Voice &s) { return s.input_gain_; },
            [](Voice &s, float x) { s.input_gain_ = x; },
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
        .def("process", &Voice::process, "input"_a,
            "Process one mono block. Takes a 1-D float32 numpy array of input "
            "samples and returns a new float32 array of the same length.")
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
            },
            "Reset the voice's DSP state to defaults.");

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
        .def("render", &Engine::render, "input"_a,
            "Offline: process a 1-D float32 mono input array through all voices "
            "and return an (n, out_channels) float32 array. Do not call while "
            "the device is running.")
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
        "Native UDP OSC receiver (tinyosc). Delivers each message to a Python "
        "callback as (address, args); dispatch runs under the GIL.")
        .def(nb::init<const std::string &, int, nb::callable>(),
            "host"_a, "port"_a, "callback"_a)
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
