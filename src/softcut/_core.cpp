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

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <vector>

#include "softcut/Voice.h"
#include "softcut/Types.h"

#include "miniaudio.h"

namespace nb = nanobind;
using namespace nb::literals;

// 1-D, C-contiguous, float32, CPU array (softcut::sample_t == float).
using BufferArray = nb::ndarray<float, nb::ndim<1>, nb::c_contig, nb::device::cpu>;

namespace {

struct Voice {
    softcut::Voice v;
    nb::object buffer_ref;  // keepalive for the numpy array passed to setBuffer
    float sample_rate = 48000.0f;

    // Engine mix parameters (not softcut params): read by Engine's mixer.
    // level is a linear gain; pan is -1 (left) .. 0 (center) .. +1 (right).
    float level_ = 1.0f;
    float pan_ = 0.0f;

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
    float sample_rate;
    int block_size;
    bool duplex;       // true: capture mic input; false: playback only
    int out_channels;  // device playback channels (typically 2)

    std::vector<float> silence;  // zeroed mono input for playback mode / no input
    std::vector<float> scratch;  // per-voice mono output, reused across voices

    ma_device device;
    bool device_inited = false;
    bool device_started = false;

    Engine(std::vector<Voice *> vs, float sr, int block, bool dup, int out_ch)
        : voices(std::move(vs)), sample_rate(sr), block_size(block),
          duplex(dup), out_channels(out_ch) {
        if (block_size < 1) throw std::invalid_argument("block_size must be >= 1");
        if (out_channels < 1) throw std::invalid_argument("out_channels must be >= 1");
        silence.assign(block_size, 0.0f);
        scratch.assign(block_size, 0.0f);
        for (Voice *vp : voices) vp->set_sample_rate(sr);
    }

    ~Engine() {
        if (device_inited) {
            ma_device_uninit(&device);  // stops the audio thread synchronously
            device_inited = false;
            device_started = false;
        }
    }

    // Process up to block_size frames of mono input into interleaved stereo
    // output. GIL-free and allocation-free. `in` has `frames` mono samples.
    void process_core(const float *in, float *out, int frames) {
        for (int i = 0; i < frames * out_channels; ++i) out[i] = 0.0f;
        for (Voice *vp : voices) {
            vp->v.processBlockMono(in, scratch.data(), frames);
            const float level = vp->level_;
            // equal-power pan: pan -1..1 -> angle 0..pi/2
            const float theta = (vp->pan_ * 0.5f + 0.5f) * 1.5707963267948966f;
            const float gl = level * std::cos(theta);
            const float gr = level * std::sin(theta);
            for (int f = 0; f < frames; ++f) {
                const float s = scratch[f];
                out[f * out_channels + 0] += s * gl;
                if (out_channels > 1) out[f * out_channels + 1] += s * gr;
            }
        }
    }

    // Called from miniaudio's audio thread. Chunks frameCount to block_size.
    void callback_process(const float *in, float *out, int frameCount) {
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
            ma_device_config cfg = ma_device_config_init(
                duplex ? ma_device_type_duplex : ma_device_type_playback);
            cfg.sampleRate = static_cast<ma_uint32>(sample_rate);
            cfg.periodSizeInFrames = static_cast<ma_uint32>(block_size);
            cfg.playback.format = ma_format_f32;
            cfg.playback.channels = static_cast<ma_uint32>(out_channels);
            if (duplex) {
                cfg.capture.format = ma_format_f32;
                cfg.capture.channels = 1;  // miniaudio sums device channels to mono
            }
            cfg.dataCallback = &Engine::data_callback;
            cfg.pUserData = this;
            if (ma_device_init(nullptr, &cfg, &device) != MA_SUCCESS)
                throw std::runtime_error("failed to initialize audio device");
            device_inited = true;
        }
        if (ma_device_start(&device) != MA_SUCCESS)
            throw std::runtime_error("failed to start audio device");
        device_started = true;
    }

    void stop() {
        if (device_started) {
            ma_device_stop(&device);
            device_started = false;
        }
    }

    static void data_callback(ma_device *dev, void *pOutput, const void *pInput,
                              ma_uint32 frameCount) {
        Engine *e = static_cast<Engine *>(dev->pUserData);
        e->callback_process(static_cast<const float *>(pInput),
                            static_cast<float *>(pOutput),
                            static_cast<int>(frameCount));
    }
};

}  // namespace

// float property: mirror field + forwarding setter
#define FPROP(name, field, setter)                                    \
    def_prop_rw(                                                       \
        name, [](Voice &s) { return s.field; },                     \
        [](Voice &s, float x) { s.field = x; s.v.setter(x); })

// bool property
#define BPROP(name, field, setter)                                    \
    def_prop_rw(                                                       \
        name, [](Voice &s) { return s.field; },                     \
        [](Voice &s, bool x) { s.field = x; s.v.setter(x); })

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
        .def("cut_to", [](Voice &s, float sec) { s.v.cutToPos(sec); }, "sec"_a,
            "Jump the head to the given position in seconds (with a crossfade).")
        .def("stop", [](Voice &s) { s.v.stop(); },
            "Immediately stop both subheads.")
        .def("reset", [](Voice &s) { s.v.reset(); },
            "Reset the voice's DSP state to defaults.");

    // Low-level realtime host. The Python-facing facade (softcut.Engine) wraps
    // this and owns the Voice objects; keep_alive ties their lifetime to the
    // engine so the audio thread never sees a freed voice.
    nb::class_<Engine>(m, "_Engine",
        "Low-level multi-voice realtime host over a miniaudio device. Use the "
        "softcut.Engine facade instead.")
        .def(nb::init<std::vector<Voice *>, float, int, bool, int>(),
            "voices"_a, "sample_rate"_a, "block_size"_a, "duplex"_a, "out_channels"_a,
            nb::keep_alive<1, 2>())
        .def("start", &Engine::start, nb::call_guard<nb::gil_scoped_release>(),
            "Open (if needed) and start the audio device. Non-blocking.")
        .def("stop", &Engine::stop, nb::call_guard<nb::gil_scoped_release>(),
            "Stop the audio device.")
        .def("render", &Engine::render, "input"_a,
            "Offline: process a 1-D float32 mono input array through all voices "
            "and return an (n, out_channels) float32 array. Do not call while "
            "the device is running.")
        .def_prop_ro("running", [](Engine &e) { return e.device_started; },
            "True while the audio device is started.")
        .def_prop_ro("block_size", [](Engine &e) { return e.block_size; })
        .def_prop_ro("out_channels", [](Engine &e) { return e.out_channels; })
        .def_prop_ro("duplex", [](Engine &e) { return e.duplex; });
}
