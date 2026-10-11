# mactv

`mactv` is a Python framework for AF9015-based USB DVB-T receivers. Its layers
follow the Linux driver: `AF9015Bridge` handles the USB command protocol and
exposes I2C client operations, `DVBTuner` models tuner callbacks, and
`DVBDemuxer` reads the transport stream.

Install the package with:

```sh
python3 -m pip install -e .[test]
```

The CLI provides device inspection, initialization, tuning, scan-table, capture,
and HTTP-streaming commands. Put command options after the subcommand. For a
live Brisbane DVB-T lock test, connect an antenna and run:

```sh
python3 -m mactv tune --frequency 177.5 --bandwidth 7
```

Without `--frequency`, tuning defaults to 226.5 MHz; bandwidth defaults to
7 MHz. The default adapter is 0. `tune` exits with status 0 when every
requested tuning locks, or 2 if any tuning does not lock. Device IDs default to
the Leadtek IDs `0x0413:0x6029`; select another device with `--vendor-id` and
`--product-id`.

For applications that need to use multiple receivers, `DVBDeviceManager`
enumerates all connected USB devices and connects to every available device
supported by its registered drivers. A device that is present but unavailable
is reported separately from unsupported devices:

```python
from mactv import AF9015Device, DVBDeviceManager

with DVBDeviceManager([AF9015Device]) as manager:
    result = manager.connect_all()
    devices = result.connected
```

Available device commands are:

```sh
python3 -m mactv device             # Detect the device without initializing it
python3 -m mactv init               # Initialize without tuning
python3 -m mactv status             # Initialize and print device/frontend status
python3 -m mactv sleep              # Initialize, then put the device to sleep
```

Use `--adapter` (or `--adapter-index`) to select an adapter. Tuning commands
also accept `--use-table` with a known scan-table name or local table path.
`known-tables` lists available LinuxTV table names, while `parse-table
<name-or-path>` prints parsed tunings without opening a device:

```sh
python3 -m mactv known-tables
python3 -m mactv parse-table au-Brisbane
python3 -m mactv tune --use-table au-Brisbane
python3 -m mactv scan --use-table au-Brisbane
```

`scan` scans the live transport stream for service/channel metadata.
For a single frequency it uses the same default tuning parameters as `tune`.

To capture up to 35 MiB of transport-stream data after tuning:

```sh
python3 -m mactv record capture.ts --frequency 177.5 --bandwidth 7
```

Recording requires a frontend lock. The adapter is stopped and put to sleep
after capture completes.

To serve the live transport stream for VLC or another MPEG-TS client:

```sh
python3 -m mactv serve --frequency 177.5 --bandwidth 7
```

The server defaults to `127.0.0.1:8080`; open
`http://127.0.0.1:8080/playlist.m3u8` in VLC to see the available DVB services.
Pass an optional `HOST:PORT` after `serve` to choose another bind address. To
connect from another device, bind to `0.0.0.0:8080` and open the playlist using
the server machine's LAN address. The server requires a frontend lock, and
Ctrl-C stops the server and adapter.

Use `--usb-log` to log USB bulk transfers, `--usb-stats` to log stream-read
timing and byte counts, and `--hardware-diagnostics` with `tune` to print
tuner/demodulator register diagnostics while checking for lock. The CLI
initializes the device for `init`, `status`, `sleep`, tuning, recording, and
streaming, uploading bundled firmware when needed.
When a scan fails, it prints the frontend status before cleanup; add
`--usb-stats` to also print asynchronous USB transfer statistics for that run.

The Leadtek device's USB IDs can be passed to `Af9015Device.open` after
checking them with `system_profiler SPUSBHostDataType`:

```python
from mactv import AF9015Device

device = Af9015Device.open(vendor_id=0x0413, product_id=0x6029)
device.init_hardware()
adapter = device.adapters[0]
adapter.start()
try:
    transport_stream_chunk = adapter.read_stream()
finally:
    adapter.stop()
```

The AF9013 demodulator and individual tuner chips still need chip-specific
drivers. The public `DVBTuner` interface is intentionally ready for those drivers.

The transport uses python-libusb1 and queues asynchronous transfers for smooth
stream buffering. The queued payload is limited to 16 MiB by default; configure
that limit with `AF9015Device.open(async_queue_mib=...)`. When the limit is
reached, completed transfers are retained and resubmission pauses until the
reader consumes enough data. `AF9015Device.open()` uses the bundled libusb
transport by default; tests or alternative backends can provide a callable via
its `transport_factory` argument. The callable receives `vendor_id` and
`product_id` and returns an object implementing `USBTransport`.

python-libusb1 requires a native libusb-1.0 library. On macOS, install one
through Homebrew, MacPorts, or Fink as appropriate for your environment.

The bundled `TDA18271Tuner` implements the AF9015 digital DVB-T path: 6, 7,
and 8 MHz channel bandwidths for the supported HDC1 and HDC2 tuner variants,
including Linux-compatible PLL, image-rejection, RF tracking-filter, and
temperature calibration. It intentionally does not expose the full Linux
TDA18271 driver's analog TV/FM, ATSC, QAM, or DVB-T2 modes, slave-tuner and
output-option configuration, selectable I2C gates, or external AGC callbacks.
Those features are outside the AF9015 DVB-T integration currently provided by
this package.

By default, `download_firmware()` loads
`mactv/drivers/af9015/firmware/dvb-usb-af9015.fw`. An alternate image can be selected with
`device.download_firmware(firmware_path="/path/to/image.fw")`.

`init_hardware()` follows the Linux AF9015 lifecycle: it detects warm firmware,
uploads firmware when needed (unless explicitly disabled), reads the EEPROM
configuration, and creates a second demuxer when the device reports dual-tuner
mode. Each entry in `device.adapters` is an `AF9015Adapter` containing a
frontend, tuner, and demuxer. Use `adapter.frontend` for tuning and status, or
the adapter's thin stream methods for PID filtering and transport-stream reads.