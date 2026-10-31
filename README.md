# mactv

`mactv` is a Python framework and command-line tool for using DVB-T receivrs. It
currently only supports a single device and chipset: the the **Leadtek WinFast DTV Dongle Gold** (USB ID `0413:6029`), which uses the Afateach AF9015 bridge, AF9013 demodulator
and TDA18271 HDC1/HDC2 tuner.

This framework has been developed using Github Copilot to translate the Linux device driver
code to Python. Although a single device is currently supported, it should be relatively
easy to use AI to add support for other DVB-T devices, using the abstractions provided here.


## Requirements

- Python 3.10 or later
- A supported AF9015 receiver for live hardware operations
- The default USB transport uses `libusb1` with native libusb supplied by the
    `libusb-package` Python dependency; a separate libusb installation is normally
    not required.

Install `mactv` from a checkout:

```sh
python3 -m pip install -e .
```

To install the test dependencies as well:

```sh
python3 -m pip install -e '.[test]'
```

On Linux, `libusb-package`'s bundled libusb is built without udev support, so
USB device permissions may still need to be configured by the OS.

Run the command-line tool's help with:

```sh
python3 -m mactv --help
```

Live tuning, firmware upload, recording, and streaming require a connected
receiver and usable antenna signal. `discover` only probes the device; commands
that tune or stream initialize the hardware and may upload firmware.

## Quick start

Tune to a channel at 177.5 MHz with 7 MHz bandwidth:

```sh
python3 -m mactv tune --tuning 177.5/7
```

The tuning format is `FREQUENCY_MHZ/BANDWIDTH_MHZ`; supported bandwidths are
6, 7, and 8 MHz. The default adapter index is 0. Tuning exits with status 0
when all requested tunings lock and status 2 if any fails to lock.

Scan a LinuxTV DVB-T table and list its services:

```sh
python3 -m mactv scan --use-table au-Brisbane
```

Capture a selected service or an entire multiplex:

```sh
python3 -m mactv record channel.ts --use-table au-Brisbane --lcn 7
python3 -m mactv record multiplex.ts --use-table au-Brisbane --doublet-id 1.2
```

Serve discovered services and live streams to a local network:

```sh
python3 -m mactv serve --use-table au-Brisbane
```

By default, the HTTP server binds to `127.0.0.1:8080`. Open
`http://127.0.0.1:8080/playlist.m3u8` in VLC or another MPEG-TS client.
To allow other devices on your network to connect, bind to all interfaces:

```sh
python3 -m mactv serve 0.0.0.0:8080 --use-table au-Brisbane
```

Then use the server computer's LAN address in the playlist URL. Stop a running
tune, recording, or server with Ctrl-C where applicable.

## Commands

Command options go after the subcommand. Use `--adapter INDEX` (also
`--adapter-index`) to choose an adapter; it defaults to 0.

| Command | Purpose |
| --- | --- |
| `discover` | Find a supported receiver and report whether its firmware is running; does not initialize it. |
| `init` | Initialize the receiver without tuning. May require network access for firmware retrieval. |
| `status` | Initialize and print USB, adapter, frontend, and tuner status. |
| `sleep` | Initialize, then put the receiver into its low-power state. |
| `tune` | Tune to a frequency or each entry in a scan table and report lock. |
| `known-tables` | List LinuxTV DVB-T scan-table names; requires network access. |
| `parse-table NAME_OR_PATH` | Parse a known scan-table name or a local table file without opening a device. |
| `scan` | Read live transport-stream metadata and list services/channels. |
| `record OUTPUT_PATH` | Record a selected service or multiplex to a transport-stream file. |
| `serve` | Serve discovered services and their live transport streams over HTTP. |

`tune` requires either `--tuning FREQUENCY_MHZ/BANDWIDTH_MHZ` or
`--use-table NAME_OR_PATH`; those options cannot be combined. `scan`, `record`,
and `serve` accept either tuning source or `--load-services PATH`, and these
service-map inputs cannot be combined.

### Scan tables and service maps

Use the LinuxTV DVB-T table name for your location or pass a local table path:

```sh
python3 -m mactv known-tables
python3 -m mactv parse-table au-Brisbane
python3 -m mactv parse-table ./my-scan-table
python3 -m mactv scan --use-table ./my-scan-table
```

`scan`, `record`, and `serve` can save a scanned service map as JSON with
`--save-services PATH`. They can also load a previously saved map with
`--load-services PATH`, without scanning a table or supplying a tuning.

### Recording

Recording runs for 15 seconds by default. Change the duration with
`--duration SECONDS`; Ctrl-C stops early and preserves the partial file. Choose
exactly one selector:

- `--lcn LCN` records the service with that logical channel number.
- `--triplet-id ONID.TSID.SID` records the service with that DVB service ID.
- `--doublet-id ONID.TSID` records the full multiplex.

Service recordings configure PID filtering for the selected service. Recording
requires a frontend lock; the adapter is stopped and put to sleep when the
capture ends.

### Diagnostics

Use these options on device commands:

- `--usb-log` logs USB bulk transfers.
- `--usb-stats` logs stream-read timing and byte counts.
- `--hardware-diagnostics` prints tuner and demodulator register diagnostics
  while checking tuning lock.

## Python API

Register a device driver with `DVBDeviceManager` to enumerate and connect
supported receivers. The result separates connected devices, supported
devices that could not be opened, and unsupported USB devices:

```python
from mactv import AF9015Device, DVBDeviceManager, DVBTuning

with DVBDeviceManager([AF9015Device]) as manager:
    result = manager.connect_all()
    if not result.connected:
        raise RuntimeError("No supported DVB receiver could be opened")

    device = result.connected[0]
    device.init_hardware()
    status = device.tune(
        DVBTuning(frequency_hz=177_500_000, bandwidth_hz=7_000_000)
    )
    print(f"Frontend status: {status}")
```

The manager closes connected devices and its libusb context when leaving the
`with` block. `device.adapters` contains the configured adapter objects.
`DVBDeviceManager.connect_all()` returns `connected`, `unavailable`, and
`unsupported` collections; use `device.model_name` to inspect a registered
model name.

For lower-level streaming, start an adapter after initializing and tuning its
frontend, then stop it when finished:

```python
adapter = device.adapters[0]
adapter.start()
try:
    transport_stream_chunk = adapter.read_stream()
finally:
    adapter.stop()
```

The default transport uses python-libusb1 asynchronous USB transfers and limits
queued stream data to 16 MiB. Applications that construct or inject transports
can configure the queue limit through the transport API. Alternative USB
transports can implement the public `USBTransport` interface.

## Firmware

Initialization detects whether AF9015 firmware is already running. If needed,
the device uses the verified firmware cache; if the cache is missing or
invalid, it downloads firmware from configured upstream URLs and checks its
SHA-256 before use. Firmware acquisition therefore needs network access when no
valid cached image is available. You can also supply firmware bytes or a local
firmware path to `AF9015Device.init_hardware()` or
`AF9015Device.download_firmware()`.

## Supported scope

The TDA18271 implementation covers digital DVB-T with 6, 7, and 8 MHz channel
bandwidths for HDC1 and HDC2 tuner variants. It does not expose the full Linux
TDA18271 driver's analog TV/FM, ATSC, QAM, or DVB-T2 modes, nor its slave-tuner,
output-option, selectable I2C-gate, or external-AGC features. Other tuner
variants are not implemented by this integration.

For the Linux compatibility reference and implementation details, see
[the AF9015 driver guide](docs/af9015_driver_explained.md) and
[the AF9015 header guide](docs/af9015_header_explained.md).

## Development

The test suite uses fake or injected USB transports and does not require
physical receiver hardware:

```sh
./.venv/bin/python -m pip install -e '.[test]'
./.venv/bin/python -m pytest -q
```
