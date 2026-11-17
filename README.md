# pydvb

`pydvb` is a Python framework and command-line tool for using DVB-T receivers. It
currently only supports a single device and chipset: the the **Leadtek WinFast DTV Dongle Gold** (USB ID `0413:6029`), which uses the Afateach AF9015 bridge, AF9013 demodulator and TDA18271 HDC1/HDC2 tuner.

This framework has been developed using Github Copilot to translate the Linux device driver code to user-space Python. Although a single device is currently supported, it should be relatively easy to use AI to add support for other DVB-T devices, using the abstractions provided here.

This project started has an experiment in using AI to port a DVB device driver from Linux to user-space on macOS, which has no native DVB support. I've used Python partly because of my own familiarity, partly for ease of prototyping, and partly for portability, in case the framework is useful on other platforms.

## Requirements

- Python 3.10 or later.
- A supported AF9015 receiver for live hardware operations.
- The default USB transport uses `libusb1` and the native libusb supplied by
  `libusb-package`. A separate libusb installation is normally unnecessary.

Install from a checkout:

```sh
python3 -m pip install -e .
```

Install the test dependencies when developing:

```sh
python3 -m pip install -e '.[test]'
```

On Linux, the bundled libusb is built without udev support. The operating
system may therefore require additional USB permission configuration.

Live tuning, firmware upload, recording, and streaming require a connected
receiver and usable antenna signal. Device discovery only probes the receiver;
commands that tune or stream initialize the hardware and may upload firmware.

## Quick start

Show command-line help:

```sh
python3 -m pydvb --help
```

Tune to 177.5 MHz with a 7 MHz bandwidth:

```sh
python3 -m pydvb tune --tuning 177.5/7
```

Tuning values use `FREQUENCY_MHZ/BANDWIDTH_MHZ`. Supported bandwidths are 6,
7, and 8 MHz. The default adapter is 0. Tuning exits with status 0 when every
requested tuning locks, or status 2 if any fails to lock.

Scan a LinuxTV DVB-T table and list its services:

```sh
python3 -m pydvb scan --use-table au-Brisbane
```

Record one service or a full multiplex:

```sh
python3 -m pydvb record channel.ts --use-table au-Brisbane --lcn 7
python3 -m pydvb record multiplex.ts --use-table au-Brisbane --doublet-id 1.2
```

Start a local streaming server:

```sh
python3 -m pydvb serve --use-table au-Brisbane
```

The server listens on `127.0.0.1:8080` by default. Open
`http://127.0.0.1:8080/playlist.m3u8` in VLC or another MPEG-TS client.
Commands that run continuously can be stopped with Ctrl-C.

## Desktop GUI

Install and launch the Qt Quick application:

```sh
python3 -m pip install -e '.[gui]'
pydvb-gui
```

The app checks for supported receivers every two seconds, initializes newly
discovered devices, removes disconnected devices, and displays signal status
for each adapter. On **Devices**, turn off **Auto-discover and initialize
devices** to pause automatic discovery, or select **Discover** to check
immediately.

On **Tuning**, refresh the known LinuxTV tables (requires network access), select
a table, or edit individual frequencies. Select **Scan Tuning for Channels** to
build the channel map. The status text shows the current tuning number and
frequency, along with counts for channels discovered and channels mapped.
Select **Cancel Scan** to stop the scan. Cancellation is cooperative: it takes
effect after the current read or tuning operation returns. Results from
completed tunings are kept; incomplete results from the current tuning are
discarded. Choosing **Quit** during a scan requests cancellation and waits for
the scan and device cleanup to finish before exiting.

The **Server** page starts the HTTP server. Its default address is
`127.0.0.1:8080`; use the displayed playlist URL in VLC or another MPEG-TS
client. Binding to a non-loopback address makes the stream available on the
network without authentication. By default, the app starts the server when a
device adapter is initialized and a service map with channels is available.
Turn off **Configure > Auto-start server when available** to disable this.

The **Logs** window shows live Python logging output. Closing or hiding it
leaves logging active in the background. The **Help > About PyDVB** dialog
links to third-party library homepages and credits the icon sources.

### In-app video player

Install the player extra to enable video playback:

```sh
python3 -m pip install -e '.[player]'
```

This extra includes the GUI and multimedia dependencies. The **Player** page
lists loaded channels; start the server and select a channel to play it. The
app uses Qt's FFmpeg multimedia backend and leaves decoder and hardware
acceleration selection to Qt. Without the extra, the other GUI pages remain
available and the Player page displays the install command.

## Standalone builds

The PyInstaller spec files create standalone, one-folder applications. Build
each app on its target operating system; PyInstaller does not cross-compile.

Build the CLI:

```sh
python3 -m pip install -e '.[app]'
python3 -m PyInstaller --clean pydvb.spec
```

The CLI is written to `dist/pydvb/` (`pydvb` on macOS and Linux,
`pydvb.exe` on Windows).

Build the GUI:

```sh
python3 -m pip install -e '.[gui,app]'
python3 -m PyInstaller --clean pydvb-gui.spec
```

The GUI build produces `dist/PyDVB.app` on macOS and
`dist/PyDVB/PyDVB` (`PyDVB.exe` on Windows) elsewhere. To include video
playback, install `.[player,app]` instead of `.[gui,app]` before building.
Both apps include the AF9015 firmware. Hardware and USB permission
requirements are unchanged. The macOS bundle is unsigned; sign and notarize it
before distribution.

The Windows app uses `pydvb/gui/icons/app.ico`; macOS uses
`pydvb/gui/icons/app.icns`. After changing `app.svg`, regenerate the icons with
the GUI dependencies installed:

```sh
python3 scripts/generate_app_icon.py
```

The script generates the ICO on any platform and the ICNS on macOS.

The GUI restores settings from its per-user configuration directory and saves
them on orderly exit. Settings include tunings, server preferences, the
service map, and Configure menu options. Use **Open Settings** and **Save
Settings** to load or save a separate `.pydvb` profile. Profiles contain JSON;
existing `.json` files can also be opened. Device state, window state, active
streams, and clients are not saved.

## Command-line reference

Put options after the subcommand. Device commands accept `--adapter INDEX`
(alias `--adapter-index`), which defaults to 0.

| Command | Purpose |
| --- | --- |
| `discover` | Find supported receivers and report firmware state without initializing them. |
| `init` | Initialize a receiver without tuning; firmware retrieval may require network access. |
| `status` | Initialize and show USB, adapter, frontend, and tuner status. |
| `sleep` | Initialize a receiver and put it into its low-power state. |
| `tune` | Tune to a frequency or scan-table entries and report lock status. |
| `known-tables` | List LinuxTV DVB-T table names; requires network access. |
| `parse-table NAME_OR_PATH` | Parse a known table or local file without opening a device. |
| `scan` | Read live transport-stream metadata and list services. |
| `record OUTPUT_PATH` | Record one service or an entire multiplex. |
| `serve` | Serve service listings and live streams over HTTP. |

`tune` requires one of `--tuning FREQUENCY_MHZ/BANDWIDTH_MHZ` or
`--use-table NAME_OR_PATH`. `scan`, `record`, and `serve` accept either tuning
source or `--load-services PATH`; these inputs cannot be combined.

### Scan tables and service maps

Use a LinuxTV table name or a local table path:

```sh
python3 -m pydvb known-tables
python3 -m pydvb parse-table au-Brisbane
python3 -m pydvb parse-table ./my-scan-table
python3 -m pydvb scan --use-table ./my-scan-table
```

Use `--save-services PATH` with `scan`, `record`, or `serve` to save a service
map as JSON. Use `--load-services PATH` to load a saved map instead of
scanning a table or providing a tuning.

### Recording

Recording lasts 15 seconds by default. Set `--duration SECONDS` to change the
duration. Ctrl-C stops recording early and preserves the partial file. Choose
exactly one selector:

- `--lcn LCN`: record the service with that logical channel number.
- `--triplet-id ONID.TSID.SID`: record the service with that DVB ID.
- `--doublet-id ONID.TSID`: record the full multiplex.

Service recordings filter by the selected service's PIDs. A frontend lock is
required. The adapter is stopped and put to sleep when recording ends.

### Streaming

The server binds to `127.0.0.1:8080` by default. To accept connections from
other devices on the network, bind to all interfaces:

```sh
python3 -m pydvb serve 0.0.0.0:8080 --use-table au-Brisbane
```

Use the server computer's LAN address in the playlist URL. Network-bound
streams are not authenticated.

Services on one multiplex share an adapter stream and are filtered
independently by service PID. Client output queues are bounded; if a queue
fills, its oldest filtered block is discarded. A request for a different
multiplex waits for an adapter and may receive HTTP 503 if the wait times out.
After the last client disconnects, a multiplex session keeps its adapter for
about one second so a quick reconnect to the same multiplex can avoid retuning.
If another multiplex needs that adapter, the idle session is released early.
Forced eviction is disabled by default; when enabled with
`evict_oldest_on_exhaustion=True`, it disconnects all clients using the evicted
multiplex.

While a multiplex is active, the server scans present/following DVB events in
separate bursts. A burst ends when every service has event data or after 30
seconds. An incomplete burst logs services still pending, waits five minutes,
and then retries. Completed burst durations and a rolling average are logged.
Applications embedding the server can set `event_scan_max_duration_seconds`
and `event_scan_pause_seconds`. Event scanning uses a bounded, non-blocking
queue; stale event chunks are dropped if processing falls behind, without
delaying stream delivery.

The CLI server decodes events by default. Set `--event-decode false` to disable
decoding or `--event-decode true` to enable it explicitly. In the GUI, turn off
**Configure > Show program guide info** to disable event scanning and guide
refreshes and hide the guide.

### Diagnostics

Use these options with device commands:

- `--usb-log`: log USB bulk transfers.
- `--usb-stats`: log stream-read timing and byte counts.
- `--hardware-diagnostics`: print tuner and demodulator register diagnostics
  while checking tuning lock.

## Python API

Use `DVBDeviceManager` to enumerate and connect supported receivers. Its result
separates connected devices, supported devices that could not be opened, and
unsupported USB devices:

```python
from pydvb import AF9015Device, DVBDeviceManager, DVBTuning

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

The manager closes connected devices and its libusb context when the `with`
block exits. `device.adapters` contains the configured adapters.
`connect_all()` returns `connected`, `unavailable`, and `unsupported`
collections. Call `connect_new()` repeatedly to connect only devices that
manager has not already opened. Use `device.model_name` to inspect the
registered model name.

For lower-level streaming, initialize and tune the frontend, then start the
adapter. Stop it when finished:

```python
adapter = device.adapters[0]
adapter.start()
try:
    transport_stream_chunk = adapter.read_stream()
finally:
    adapter.stop()
```

The default transport uses python-libusb1 asynchronous USB transfers and caps
queued stream data at 16 MiB. Applications that construct or inject a
transport can configure its queue limit. Alternative transports can implement
the public `USBTransport` interface.

## Firmware

Initialization checks whether AF9015 firmware is already running. If not, the
device uses its verified firmware cache. If the cache is missing or invalid,
firmware is downloaded from configured upstream URLs and checked against its
SHA-256. Network access is needed only when there is no valid cached image.
Firmware bytes or a local firmware path can also be passed to
`AF9015Device.init_hardware()` or `AF9015Device.download_firmware()`.

## Supported scope

The TDA18271 integration supports digital DVB-T at 6, 7, and 8 MHz bandwidths
on HDC1 and HDC2 tuner variants. It does not implement the Linux driver's
analog TV/FM, ATSC, QAM, or DVB-T2 modes, or its slave-tuner, output-option,
selectable I2C-gate, or external-AGC features. Other tuner variants are not
supported by this integration.

## Development

Tests use fake or injected USB transports and do not require a physical
receiver. From the repository root, install the test dependencies and run the
suite with:

```sh
./.venv/bin/python -m pip install -e '.[test]'
./.venv/bin/python -m pytest -q
```
