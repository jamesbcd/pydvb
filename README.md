# mactv

`mactv` is a Python framework for AF9015-based USB DVB-T receivers. Its layers
follow the Linux driver: `AF9015Bridge` handles the USB command protocol and
exposes I2C client operations, `Tuner` models tuner callbacks, and
`Demuxer` reads the transport stream.

Install the package and PyUSB with:

```sh
python3 -m pip install -e .[test]
```

For a live Brisbane DVB-T lock test, connect an antenna and run:

```sh
python3 -m mactv --frequency 177.5 --bandwidth 7
```

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
from mactv import Af9015Device

device = Af9015Device.open(vendor_id=0x0413, product_id=0x6029)
device.initialize()
adapter = device.adapters[0]
demux = adapter.demuxer
demux.start()
try:
    transport_stream_chunk = demux.read_ts()
finally:
    demux.stop()
```

The AF9013 demodulator and individual tuner chips still need chip-specific
drivers. The public `Tuner` interface is intentionally ready for those drivers
without coupling them to PyUSB.

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

`initialize()` follows the Linux AF9015 lifecycle: it detects warm firmware,
reads the EEPROM configuration, and creates a second demuxer when the device
reports dual-tuner mode. Firmware upload is disabled by default; enable it
explicitly with `device.initialize(download_firmware=True)`. Adapter
configuration is available through each `AF9013Frontend` in
`device.adapters`; its transport-stream demuxer is available as `.demuxer`.