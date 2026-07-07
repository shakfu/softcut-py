// softcut-osc: a standalone, no-Python softcut OSC server.
//
// Links softcut-lib (DSP) + tinyosc (OSC codec) + miniaudio (device) into a
// single native binary that speaks the monome softcut OSC protocol -- the same
// namespace as softcut-py's SoftcutOSC and the reference softcut_jack_osc client.
// It is the "pure C++" counterpart to the Python extension: the same DSP core,
// no interpreter, so nothing on the control path can touch a GIL.
//
// Scope: the full softcut OSC namespace -- params, mix, feedback, routing,
// voice_sync, buffer clear/read/write, phase poll, lifecycle -- plus device
// selection (--output-device/--input-device/--list-devices). WAV disk I/O is
// backed by the vendored dr_wav and reads at file rate with no resampling, which
// matches softcut/norns (a sample-rate mismatch shifts pitch, by design).
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>

#include "engine.hpp"
#include "osc_server.hpp"

namespace {

std::atomic<bool> g_interrupted{false};
void on_sigint(int) { g_interrupted.store(true); }

struct Options {
    std::string listen_host = "0.0.0.0";
    int listen_port = 9999;
    std::string reply_host = "127.0.0.1";
    int reply_port = 57120;
    int voices = 6;
    float sample_rate = 48000.0f;
    int block_size = 512;
    uint32_t buffer_frames = 1u << 21;  // ~43.7 s at 48 kHz; power of two
    bool duplex = false;                // playback-only by default
    int out_channels = 2;
    double phase_period = 0.01;
    double crossfade_ms = 2.0;          // edge crossfade for buffer read/clear
    int output_device = -1;             // device index from --list-devices (-1 = default)
    int input_device = -1;
    bool resample_on_read = false;      // opt-in: convert files to engine rate on read
    bool null_backend = false;          // headless (no hardware) for tests/CI
    bool no_audio = false;              // do not open a device at all
    bool list_devices = false;
};

int parse_int(const char *s) { return static_cast<int>(std::strtol(s, nullptr, 10)); }

void usage(const char *prog) {
    std::printf(
        "usage: %s [options]\n"
        "  --listen-host H     bind address (default 0.0.0.0)\n"
        "  --listen-port P     OSC listen port (default 9999)\n"
        "  --reply-host H      phase-reply address (default 127.0.0.1)\n"
        "  --reply-port P      phase-reply port (default 57120)\n"
        "  --voices N          voice count (default 6)\n"
        "  --sample-rate HZ    sample rate (default 48000)\n"
        "  --block-size N      DSP block size in frames (default 512)\n"
        "  --buffer-frames N   per-buffer length; rounded up to a power of two\n"
        "  --crossfade-ms MS   edge crossfade for buffer read/clear (default 2)\n"
        "  --resample-on-read  convert files to the engine rate on read (default\n"
        "                      off = copy at file rate, matching softcut/norns)\n"
        "  --output-device N   playback device index (see --list-devices)\n"
        "  --input-device N    capture device index (with --duplex)\n"
        "  --list-devices      print audio devices and exit\n"
        "  --duplex            capture mic input (default playback only)\n"
        "  --null              run headless on miniaudio's null backend\n"
        "  --no-audio          do not open an audio device at all\n"
        "  -h, --help          this message\n", prog);
}

bool parse_args(int argc, char **argv, Options &o) {
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto need = [&](const char *name) -> const char * {
            if (i + 1 >= argc) { std::fprintf(stderr, "%s needs a value\n", name); std::exit(2); }
            return argv[++i];
        };
        if (a == "--listen-host") o.listen_host = need("--listen-host");
        else if (a == "--listen-port") o.listen_port = parse_int(need("--listen-port"));
        else if (a == "--reply-host") o.reply_host = need("--reply-host");
        else if (a == "--reply-port") o.reply_port = parse_int(need("--reply-port"));
        else if (a == "--voices") o.voices = parse_int(need("--voices"));
        else if (a == "--sample-rate") o.sample_rate = static_cast<float>(std::atof(need("--sample-rate")));
        else if (a == "--block-size") o.block_size = parse_int(need("--block-size"));
        else if (a == "--buffer-frames") o.buffer_frames = static_cast<uint32_t>(parse_int(need("--buffer-frames")));
        else if (a == "--crossfade-ms") o.crossfade_ms = std::atof(need("--crossfade-ms"));
        else if (a == "--output-device") o.output_device = parse_int(need("--output-device"));
        else if (a == "--input-device") o.input_device = parse_int(need("--input-device"));
        else if (a == "--resample-on-read") o.resample_on_read = true;
        else if (a == "--list-devices") o.list_devices = true;
        else if (a == "--duplex") o.duplex = true;
        else if (a == "--null") o.null_backend = true;
        else if (a == "--no-audio") o.no_audio = true;
        else if (a == "-h" || a == "--help") { usage(argv[0]); return false; }
        else { std::fprintf(stderr, "unknown option: %s\n", a.c_str()); usage(argv[0]); std::exit(2); }
    }
    return true;
}

}  // namespace

int main(int argc, char **argv) {
    Options o;
    if (!parse_args(argc, argv, o)) return 0;

    if (o.list_devices) {
        scosc::Engine::list_devices();
        return 0;
    }

    std::signal(SIGINT, on_sigint);
    std::signal(SIGTERM, on_sigint);

    try {
        scosc::Engine engine(o.voices, o.sample_rate, o.block_size, o.buffer_frames,
                             o.duplex, o.out_channels);
        const long crossfade_frames =
            std::lround(o.crossfade_ms / 1000.0 * o.sample_rate);
        scosc::OscServer server(engine, o.listen_host, o.listen_port,
                                o.reply_host, o.reply_port, o.phase_period,
                                crossfade_frames, o.resample_on_read);

        if (!o.no_audio) engine.start(o.output_device, o.input_device, o.null_backend);
        server.start();

        std::printf("softcut-osc: listening on %s:%d\n",
                    o.listen_host.c_str(), server.port());
        std::printf("softcut-osc: phase replies to %s:%d\n",
                    o.reply_host.c_str(), o.reply_port);
        std::printf("softcut-osc: %d voices, %.0f Hz, %u-frame buffers%s\n",
                    o.voices, o.sample_rate, engine.buffer_frames(),
                    o.no_audio ? " (no audio device)"
                               : (o.null_backend ? " (null backend)" : ""));
        std::fflush(stdout);

        // Run until /quit, /goodbye, or a signal.
        while (!server.quit_requested() && !g_interrupted.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(50));

        std::printf("softcut-osc: shutting down\n");
        server.stop();
        engine.stop();
    } catch (const std::exception &e) {
        std::fprintf(stderr, "softcut-osc: fatal: %s\n", e.what());
        return 1;
    }
    return 0;
}
