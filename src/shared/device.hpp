// miniaudio device configuration shared by the Python extension and the
// standalone server. Only the mechanical, host-agnostic parts live here: explicit
// device-id resolution and building the f32 mono-in / interleaved-out config.
// Each host keeps its own device lifecycle (the extension is lazy and restartable
// and exposes the device list to Python; the standalone re-inits and adds a null
// backend), which is why start/stop/list stay per-host.
#pragma once

#include <stdexcept>

#include "miniaudio.h"

namespace scsh {

// Resolve explicit playback/capture device ids from a context. For a non-negative
// index, points the corresponding p_* at the resolved id; a negative index leaves
// it null (system default). Throws std::invalid_argument if an index is out of
// range, std::runtime_error if enumeration fails.
inline void resolve_device_ids(ma_context &ctx, int output_index, int input_index,
                               bool duplex, ma_device_id &playback_id,
                               ma_device_id &capture_id, ma_device_id *&p_playback,
                               ma_device_id *&p_capture) {
    ma_device_info *pb, *cap;
    ma_uint32 npb, ncap;
    if (ma_context_get_devices(&ctx, &pb, &npb, &cap, &ncap) != MA_SUCCESS)
        throw std::runtime_error("failed to enumerate audio devices");
    if (output_index >= 0) {
        if (output_index >= static_cast<int>(npb))
            throw std::invalid_argument("output device index out of range");
        playback_id = pb[output_index].id;
        p_playback = &playback_id;
    }
    if (duplex && input_index >= 0) {
        if (input_index >= static_cast<int>(ncap))
            throw std::invalid_argument("input device index out of range");
        capture_id = cap[input_index].id;
        p_capture = &capture_id;
    }
}

// Build the device config: f32, mono capture (miniaudio sums device channels to
// mono), interleaved playback of out_channels. p_playback/p_capture may be null
// for the default device.
inline ma_device_config make_device_config(float sample_rate, int block_size,
                                           bool duplex, int out_channels,
                                           ma_device_id *p_playback,
                                           ma_device_id *p_capture,
                                           ma_device_data_proc callback,
                                           void *user_data) {
    ma_device_config cfg = ma_device_config_init(
        duplex ? ma_device_type_duplex : ma_device_type_playback);
    cfg.sampleRate = static_cast<ma_uint32>(sample_rate);
    cfg.periodSizeInFrames = static_cast<ma_uint32>(block_size);
    cfg.playback.format = ma_format_f32;
    cfg.playback.channels = static_cast<ma_uint32>(out_channels);
    cfg.playback.pDeviceID = p_playback;
    if (duplex) {
        cfg.capture.format = ma_format_f32;
        cfg.capture.channels = 1;
        cfg.capture.pDeviceID = p_capture;
    }
    cfg.dataCallback = callback;
    cfg.pUserData = user_data;
    return cfg;
}

}  // namespace scsh
