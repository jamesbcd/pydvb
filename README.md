# mactv

`mactv` is a Python framework for AF9015-based USB DVB-T receivers. Its layers
follow the Linux driver: `AF9015Bridge` handles the USB command protocol and
exposes I2C client operations, `Tuner` models tuner callbacks, and
`Demuxer` reads the transport stream.

Install the package with:

```sh
python3 -m pip install -e .[test]
```

For a live Brisbane DVB-T lock test, connect an antenna and run:

```sh
python3 -m mactv --frequency 177.5 --bandwidth 7
```

To capture up to 5 MiB of transport-stream data after tuning:

```sh
python3 -m mactv --frequency 177.5 --bandwidth 7 --record capture.ts
```

To serve the live transport stream for VLC or another MPEG-TS client, bind
the local HTTP server to a host and port:

```sh
python3 -m mactv --frequency 177.5 --bandwidth 7 --serve
```

Open `http://127.0.0.1:8080/stream.ts` in VLC. The server binds to loopback
only when that address is specified, and it stops the adapter when interrupted
with Ctrl-C. `--record` and `--serve` are mutually exclusive.

The adapter is stopped and put to sleep after capture completes.

The command uploads the bundled firmware when needed, initializes the tuner,
waits for MPEG lock, and prints frontend reception metrics. It exits with
status 0 when locked and status 2 when tuning completes without lock. Use
another multiplex frequency, such as `184.5`, with `--frequency` when testing
local reception. `--safe` only checks the USB device state and does not tune.
Use `--sleep` to initialize the device and put it into its low-power state
without tuning.

The Leadtek device's USB IDs can be passed to `Af9015Device.open` after
checking them with `system_profiler SPUSBHostDataType`:

```python
from mactv import AF9015Device

device = Af9015Device.open(vendor_id=0x0413, product_id=0x6029)
device.init_hardware()
adapter = device.adapters[0]
adapter.start()
try:
    transport_stream_chunk = adapter.read_ts()
finally:
    adapter.stop()
```

The AF9013 demodulator and individual tuner chips still need chip-specific
drivers. The public `Tuner` interface is intentionally ready for those drivers.

The transport uses python-libusb1 and queues asynchronous transfers for smooth
stream buffering:

```python
from mactv import AF9015Device, LibUSB1Transport

transport = LibUSB1Transport.find(0x0413, 0x6029, async_transfer_count=8)
device = AF9015Device(transport, vendor_id=0x0413, product_id=0x6029)
```

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
`mactv/firmware/dvb-usb-af9015.fw`. An alternate image can be selected with
`device.download_firmware(firmware_path="/path/to/image.fw")`.

`init_hardware()` follows the Linux AF9015 lifecycle: it detects warm firmware,
reads the EEPROM configuration, and creates a second demuxer when the device
reports dual-tuner mode. Firmware upload is disabled by default; enable it
explicitly with `device.init_hardware(download_firmware=True)`. Each entry in
`device.adapters` is an `AF9015Adapter` containing a frontend, tuner, and
demuxer. Use `adapter.frontend` for tuning and status, or the adapter's thin
stream methods for PID filtering and transport-stream reads.