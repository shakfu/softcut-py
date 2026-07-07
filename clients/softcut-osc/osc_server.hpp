// Standalone OSC server: a UDP receive thread that parses with tinyosc and
// dispatches the full softcut namespace directly to the engine (no Python, no
// GIL), plus a phase-poll sender thread. The namespace mirrors the reference
// softcut_jack_osc client and softcut-py's SoftcutOSC: the wire is 0-based
// (voices 0-5, buffers 0-1).
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "tinyosc.h"
#include "shared/osc_socket.hpp"  // UDP socket helpers + platform network headers
#include "buffer_io.hpp"
#include "disk_worker.hpp"
#include "engine.hpp"
#include "softcut/Voice.h"

namespace scosc {

using scsh::close_socket;
using scsh::ensure_sockets;
using scsh::kInvalidSocket;
using scsh::make_sockaddr;
using scsh::set_recv_timeout_ms;
using scsh::socket_t;

// address -> softcut::Voice setter, for the 1:1 `/set/param/cut/*` params.
struct FloatParam { void (softcut::Voice::*setter)(float); };
struct BoolParam { void (softcut::Voice::*setter)(bool); };

inline const std::unordered_map<std::string, FloatParam> &float_params() {
    static const std::unordered_map<std::string, FloatParam> t = {
        {"/set/param/cut/rate", {&softcut::Voice::setRate}},
        {"/set/param/cut/loop_start", {&softcut::Voice::setLoopStart}},
        {"/set/param/cut/loop_end", {&softcut::Voice::setLoopEnd}},
        {"/set/param/cut/fade_time", {&softcut::Voice::setFadeTime}},
        {"/set/param/cut/rec_level", {&softcut::Voice::setRecLevel}},
        {"/set/param/cut/pre_level", {&softcut::Voice::setPreLevel}},
        {"/set/param/cut/rec_offset", {&softcut::Voice::setRecOffset}},
        {"/set/param/cut/recpre_slew_time", {&softcut::Voice::setRecPreSlewTime}},
        {"/set/param/cut/rate_slew_time", {&softcut::Voice::setRateSlewTime}},
        {"/set/param/cut/phase_quant", {&softcut::Voice::setPhaseQuant}},
        {"/set/param/cut/phase_offset", {&softcut::Voice::setPhaseOffset}},
        {"/set/param/cut/pre_filter_fc", {&softcut::Voice::setPreFilterFc}},
        {"/set/param/cut/pre_filter_fc_mod", {&softcut::Voice::setPreFilterFcMod}},
        {"/set/param/cut/pre_filter_rq", {&softcut::Voice::setPreFilterRq}},
        {"/set/param/cut/pre_filter_lp", {&softcut::Voice::setPreFilterLp}},
        {"/set/param/cut/pre_filter_hp", {&softcut::Voice::setPreFilterHp}},
        {"/set/param/cut/pre_filter_bp", {&softcut::Voice::setPreFilterBp}},
        {"/set/param/cut/pre_filter_br", {&softcut::Voice::setPreFilterBr}},
        {"/set/param/cut/pre_filter_dry", {&softcut::Voice::setPreFilterDry}},
        {"/set/param/cut/post_filter_fc", {&softcut::Voice::setPostFilterFc}},
        {"/set/param/cut/post_filter_rq", {&softcut::Voice::setPostFilterRq}},
        {"/set/param/cut/post_filter_lp", {&softcut::Voice::setPostFilterLp}},
        {"/set/param/cut/post_filter_hp", {&softcut::Voice::setPostFilterHp}},
        {"/set/param/cut/post_filter_bp", {&softcut::Voice::setPostFilterBp}},
        {"/set/param/cut/post_filter_br", {&softcut::Voice::setPostFilterBr}},
        {"/set/param/cut/post_filter_dry", {&softcut::Voice::setPostFilterDry}},
    };
    return t;
}

inline const std::unordered_map<std::string, BoolParam> &bool_params() {
    static const std::unordered_map<std::string, BoolParam> t = {
        {"/set/param/cut/loop_flag", {&softcut::Voice::setLoopFlag}},
        {"/set/param/cut/rec_flag", {&softcut::Voice::setRecFlag}},
        {"/set/param/cut/rec_once", {&softcut::Voice::setRecOnceFlag}},
        {"/set/param/cut/play_flag", {&softcut::Voice::setPlayFlag}},
    };
    return t;
}

class OscServer {
public:
    OscServer(Engine &engine, const std::string &listen_host, int listen_port,
              const std::string &reply_host, int reply_port, double phase_period,
              long crossfade_frames, bool resample_on_read)
        : engine_(engine), phase_period_(phase_period),
          crossfade_frames_(crossfade_frames), resample_on_read_(resample_on_read),
          last_phase_(static_cast<size_t>(engine.n_voices()),
                      std::numeric_limits<double>::quiet_NaN()) {
        ensure_sockets();
        // receive socket
        rsock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (rsock_ == kInvalidSocket) throw std::runtime_error("recv socket() failed");
        int one = 1;
        setsockopt(rsock_, SOL_SOCKET, SO_REUSEADDR,
                   reinterpret_cast<const char *>(&one), sizeof(one));
        sockaddr_in addr;
        make_sockaddr(listen_host, listen_port, addr);
        if (::bind(rsock_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
            close_socket(rsock_);
            throw std::runtime_error("bind() failed on port " + std::to_string(listen_port));
        }
        set_recv_timeout_ms(rsock_, 100);
        sockaddr_in bound; socklen_t bl = sizeof(bound);
        port_ = (getsockname(rsock_, reinterpret_cast<sockaddr *>(&bound), &bl) == 0)
                    ? ntohs(bound.sin_port) : listen_port;
        // reply socket
        ssock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (ssock_ == kInvalidSocket) throw std::runtime_error("reply socket() failed");
        make_sockaddr(reply_host, reply_port, dest_);
    }

    ~OscServer() {
        stop();
        if (rsock_ != kInvalidSocket) close_socket(rsock_);
        if (ssock_ != kInvalidSocket) close_socket(ssock_);
    }

    int port() const { return port_; }
    bool quit_requested() const { return quit_.load(std::memory_order_acquire); }

    void start() {
        if (recv_running_.exchange(true)) return;
        recv_thread_ = std::thread([this] { recv_loop(); });
    }

    void stop() {
        recv_running_.store(false);
        if (recv_thread_.joinable()) recv_thread_.join();
        stop_phase_poll();
        disk_.stop();  // finish queued reads/writes, then join (no new jobs now)
    }

    // --- phase poll ---------------------------------------------------------

    void start_phase_poll() {
        if (poll_running_.exchange(true)) return;
        poll_thread_ = std::thread([this] { poll_loop(); });
    }

    void stop_phase_poll() {
        if (!poll_running_.exchange(false)) return;
        poll_cv_.notify_all();
        if (poll_thread_.joinable()) poll_thread_.join();
    }

private:
    // --- receive loop -------------------------------------------------------

    void recv_loop() {
        std::vector<char> buf(4096);
        while (recv_running_.load()) {
            int n = static_cast<int>(::recvfrom(rsock_, buf.data(),
                static_cast<int>(buf.size()), 0, nullptr, nullptr));
            if (n <= 0) continue;  // timeout or error
            if (tosc_isBundle(buf.data())) {
                tosc_bundle b; tosc_parseBundle(&b, buf.data(), n);
                tosc_message m;
                while (tosc_getNextMessage(&b, &m)) dispatch(&m);
            } else {
                tosc_message m;
                if (tosc_parseMessage(&m, buf.data(), n) == 0) dispatch(&m);
            }
        }
    }

    bool voice_ok(int v) const { return v >= 0 && v < engine_.n_voices(); }

    void dispatch(tosc_message *m) {
        const char *addr = tosc_getAddress(m);
        const char *fmt = tosc_getFormat(m);
        if (addr == nullptr || fmt == nullptr) return;

        // lifecycle
        if (std::strcmp(addr, "/quit") == 0 || std::strcmp(addr, "/goodbye") == 0) {
            quit_.store(true, std::memory_order_release); return;
        }
        if (std::strcmp(addr, "/hello") == 0) { std::printf("hello\n"); return; }
        if (std::strcmp(addr, "/poll/start/cut/phase") == 0) { start_phase_poll(); return; }
        if (std::strcmp(addr, "/poll/stop/cut/phase") == 0) { stop_phase_poll(); return; }
        if (std::strncmp(addr, "/poll/", 6) == 0) return;  // vu etc.: accepted, ignored
        if (std::strcmp(addr, "/softcut/reset") == 0) { do_reset(); return; }

        // /set/param/cut/* fast params (voice:int, value:int|float)
        if (fmt[0] == 'i' && (fmt[1] == 'f' || fmt[1] == 'i') && fmt[2] == '\0') {
            const auto &fmap = float_params();
            auto fit = fmap.find(addr);
            const auto &bmap = bool_params();
            auto bit = (fit == fmap.end()) ? bmap.find(addr) : bmap.end();
            if (fit != fmap.end() || bit != bmap.end()) {
                int v = tosc_getNextInt32(m);
                float val = (fmt[1] == 'f') ? tosc_getNextFloat(m)
                                            : static_cast<float>(tosc_getNextInt32(m));
                if (!voice_ok(v)) return;
                Voice &vv = engine_.voice(v);
                if (fit != fmap.end()) {
                    auto s = fit->second.setter;
                    engine_.apply([&vv, s, val] { (vv.sc.*s)(val); });
                } else {
                    auto s = bit->second.setter;
                    bool bv = val != 0.0f;
                    engine_.apply([&vv, s, bv] { (vv.sc.*s)(bv); });
                }
                return;
            }
        }

        dispatch_slow(addr, fmt, m);
    }

    // Non-fast-path addresses: mix, routing, position, buffer ops, disk I/O.
    void dispatch_slow(const char *addr, const char *fmt, tosc_message *m) {
        // (voice:int, value:float) mix params
        if (fmt[0] == 'i' && fmt[1] == 'f' && fmt[2] == '\0') {
            int v = tosc_getNextInt32(m);
            float x = tosc_getNextFloat(m);
            if (std::strcmp(addr, "/set/level/cut") == 0) {
                if (voice_ok(v)) { Voice &vv = engine_.voice(v); engine_.apply([&vv, x]{ vv.level = x; }); }
            } else if (std::strcmp(addr, "/set/pan/cut") == 0) {
                if (voice_ok(v)) { Voice &vv = engine_.voice(v); engine_.apply([&vv, x]{ vv.pan = x; }); }
            } else if (std::strcmp(addr, "/set/enabled/cut") == 0) {
                if (voice_ok(v)) { Voice &vv = engine_.voice(v); bool on = x != 0.0f;
                    engine_.apply([&vv, on]{ vv.sc.setPlayFlag(on); }); }
            } else if (std::strcmp(addr, "/set/param/cut/position") == 0) {
                if (voice_ok(v)) { Voice &vv = engine_.voice(v);
                    engine_.apply([&vv, x]{ vv.sc.cutToPos(x); }); }
            }
            return;
        }
        // (i, i, f): feedback, voice_sync
        if (fmt[0] == 'i' && fmt[1] == 'i' && fmt[2] == 'f' && fmt[3] == '\0') {
            int a = tosc_getNextInt32(m), b = tosc_getNextInt32(m);
            float x = tosc_getNextFloat(m);
            if (std::strcmp(addr, "/set/level/cut_cut") == 0) {
                Engine &e = engine_;
                engine_.apply([&e, a, b, x]{ e.set_feedback(a, b, x); });  // src, dst
            } else if (std::strcmp(addr, "/set/level/in_cut") == 0) {
                // (in_channel ignored, voice, level) -- wait: this is i,i,f but
                // means (in_ch, voice, level); handled here.
                if (voice_ok(b)) { Voice &vv = engine_.voice(b);
                    engine_.apply([&vv, x]{ vv.input_gain = x; }); }
            } else if (std::strcmp(addr, "/set/param/cut/voice_sync") == 0) {
                if (voice_ok(a) && voice_ok(b)) {
                    Engine &e = engine_;
                    engine_.apply([&e, a, b, x]{
                        double p = e.voice(b).sc.getActivePosition();
                        e.voice(a).sc.cutToPos(p + x);  // dst=a, src=b
                    });
                }
            }
            return;
        }
        // (i, i): buffer routing
        if (fmt[0] == 'i' && fmt[1] == 'i' && fmt[2] == '\0' &&
            std::strcmp(addr, "/set/param/cut/buffer") == 0) {
            int v = tosc_getNextInt32(m), buf = tosc_getNextInt32(m);
            if (voice_ok(v) && (buf == 0 || buf == 1)) {
                Voice &vv = engine_.voice(v);
                float *data = engine_.buffer_data(buf);
                uint32_t frames = engine_.buffer_frames();
                engine_.apply([&vv, data, frames, buf]{
                    vv.sc.setBuffer(data, frames); vv.buffer_index = buf; });
            }
            return;
        }
        // buffer clears (on the disk worker, with an edge crossfade)
        if (std::strcmp(addr, "/softcut/buffer/clear") == 0) {
            post_clear(-1, 0.0, -1.0); return;
        }
        if (std::strcmp(addr, "/softcut/buffer/clear_channel") == 0 && fmt[0] == 'i') {
            post_clear(tosc_getNextInt32(m), 0.0, -1.0); return;
        }
        if (std::strcmp(addr, "/softcut/buffer/clear_region") == 0 &&
            fmt[0] == 'f' && fmt[1] == 'f') {
            double s = tosc_getNextFloat(m), d = tosc_getNextFloat(m);
            post_clear(-1, s, d); return;
        }
        if (std::strcmp(addr, "/softcut/buffer/clear_region_channel") == 0 &&
            fmt[0] == 'i') {
            int ch = tosc_getNextInt32(m);
            double s = tosc_getNextFloat(m), d = tosc_getNextFloat(m);
            post_clear(ch, s, d); return;
        }
        // disk I/O (dr_wav): read WAV -> buffer, or buffer -> WAV. All four take
        // a path string first, then numeric args guided by the format string
        // (so trailing optional args may be omitted).
        if (std::strncmp(addr, "/softcut/buffer/read", 20) == 0 ||
            std::strncmp(addr, "/softcut/buffer/write", 21) == 0) {
            if (fmt[0] != 's') return;
            std::string path = tosc_getNextString(m);
            int fi = 1;
            auto num = [&](double def) -> double {
                char c = fmt[fi];
                if (c == 'f') { ++fi; return tosc_getNextFloat(m); }
                if (c == 'i') { ++fi; return static_cast<double>(tosc_getNextInt32(m)); }
                return def;  // absent -> default
            };
            Engine &e = engine_;
            const long fade = crossfade_frames_;
            const bool rsmp = resample_on_read_;
            if (std::strcmp(addr, "/softcut/buffer/read_mono") == 0) {
                // path, start_src, start_dst, dur, ch_src, ch_dst, [preserve], [mix]
                double ss = num(0.0), sd = num(0.0), du = num(-1.0);
                int cs = static_cast<int>(num(0.0)), cd = static_cast<int>(num(0.0));
                float pr = static_cast<float>(num(0.0)), mx = static_cast<float>(num(1.0));
                disk_.post([&e, path, ss, sd, du, cs, cd, pr, mx, fade, rsmp] {
                    read_mono(e, path, ss, sd, du, cs, cd, pr, mx, fade, rsmp); });
            } else if (std::strcmp(addr, "/softcut/buffer/read_stereo") == 0) {
                // path, start_src, start_dst, dur, [preserve], [mix]
                double ss = num(0.0), sd = num(0.0), du = num(-1.0);
                float pr = static_cast<float>(num(0.0)), mx = static_cast<float>(num(1.0));
                disk_.post([&e, path, ss, sd, du, pr, mx, fade, rsmp] {
                    read_stereo(e, path, ss, sd, du, pr, mx, fade, rsmp); });
            } else if (std::strcmp(addr, "/softcut/buffer/write_mono") == 0) {
                double st = num(0.0), du = num(-1.0);
                int ch = static_cast<int>(num(0.0));
                disk_.post([&e, path, st, du, ch] { write_mono(e, path, st, du, ch); });
            } else if (std::strcmp(addr, "/softcut/buffer/write_stereo") == 0) {
                double st = num(0.0), du = num(-1.0);
                disk_.post([&e, path, st, du] { write_stereo(e, path, st, du); });
            }
            return;
        }
        // slew params we accept and ignore (parity with SoftcutOSC)
        // everything else: silently ignored.
    }

    void post_clear(int channel, double start, double dur) {
        Engine &e = engine_;
        const long fade = crossfade_frames_;
        disk_.post([&e, channel, start, dur, fade] {
            clear_region(e, channel, start, dur, fade); });
    }

    void do_reset() {
        for (int i = 0; i < engine_.n_voices(); ++i) {
            Voice &vv = engine_.voice(i);
            float *data = engine_.buffer_data(0);
            uint32_t frames = engine_.buffer_frames();
            engine_.apply([&vv, data, frames]{
                vv.sc.reset(); vv.sc.setBuffer(data, frames); vv.buffer_index = 0;
                vv.level = 1.0f; vv.pan = 0.0f; vv.input_gain = 1.0f;
            });
        }
    }

    // --- phase poll loop ----------------------------------------------------

    void poll_loop() {
        std::unique_lock<std::mutex> lk(poll_mtx_);
        std::fill(last_phase_.begin(), last_phase_.end(),
                  std::numeric_limits<double>::quiet_NaN());
        while (poll_running_.load()) {
            for (int i = 0; i < engine_.n_voices(); ++i) {
                const double phase = engine_.voice(i).sc.getQuantPhase();
                if (phase != last_phase_[static_cast<size_t>(i)]) {
                    last_phase_[static_cast<size_t>(i)] = phase;
                    send_phase(i, static_cast<float>(phase));
                }
            }
            poll_cv_.wait_for(lk, std::chrono::duration<double>(phase_period_),
                              [this] { return !poll_running_.load(); });
        }
    }

    void send_phase(int voice, float phase) {
        char buf[64];
        uint32_t n = tosc_writeMessage(buf, sizeof(buf), "/poll/softcut/phase",
                                       "if", voice, phase);
        ::sendto(ssock_, buf, static_cast<int>(n), 0,
                 reinterpret_cast<sockaddr *>(&dest_), sizeof(dest_));
    }

    Engine &engine_;
    double phase_period_;
    long crossfade_frames_;
    bool resample_on_read_;
    DiskWorker disk_;

    socket_t rsock_ = kInvalidSocket;
    socket_t ssock_ = kInvalidSocket;
    sockaddr_in dest_;
    int port_ = 0;

    std::atomic<bool> quit_{false};
    std::atomic<bool> recv_running_{false};
    std::thread recv_thread_;

    std::atomic<bool> poll_running_{false};
    std::thread poll_thread_;
    std::mutex poll_mtx_;
    std::condition_variable poll_cv_;
    std::vector<double> last_phase_;
};

}  // namespace scosc
