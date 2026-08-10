# Devices

By default an `Engine` uses the system default audio device(s). To pick a specific device, list what's available and pass its index.

## Listing devices

`softcut.list_devices()` returns a list of dicts:

```python
>>> import softcut
>>> softcut.list_devices()
[{'index': 0, 'name': 'MacBook Pro Speakers', 'type': 'playback', 'is_default': True},
 {'index': 1, 'name': 'External Headphones',  'type': 'playback', 'is_default': False},
 {'index': 0, 'name': 'MacBook Pro Microphone','type': 'capture', 'is_default': True}]
```

The `index` is per-type: playback devices are indexed separately from capture devices.

## Selecting a device

Pass the index of the matching type to the engine; `-1` (the default) means the system default device:

```python
import softcut

eng = softcut.Engine(
    mode="duplex",
    output_device=1,   # second playback device
    input_device=0,    # first capture device
)
```

`input_device` is only used in `"duplex"` mode. Selection is resolved when the device is opened (on `start()`), so an out-of-range index raises there.

!!! note `block_size`, `sample_rate` and `out_channels` are also set at construction. miniaudio resamples and remixes the hardware device to the format you ask for, and capture is summed to mono.
