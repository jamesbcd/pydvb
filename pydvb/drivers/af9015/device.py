"""High-level AF9015 device composition.

The Linux counterparts are ``af9015_identify_state`` (``linux/af9015.c:268``),
``af9015_eeprom_hash`` (339), ``af9015_read_config`` (372),
``af9015_get_adapter_count`` (659), ``af9015_copy_firmware`` (763),
``af9015_af9013_frontend_attach`` (844), and ``af9015_init`` (1102).
The shared USB firmware upload path is ``af9015_download_firmware`` at line
289 of that file.
"""

from pathlib import Path
from time import monotonic, sleep
from .adapter import AF9015Adapter
from .bridge import AF9015Bridge
from .demux import AF9015Demuxer
from ...driver_api.device import DVBDevice
from .frontend import AF9013Frontend
from .tuner import TDA18271Tuner
from ...usb import (
    USBDeviceID,
    USBDeviceInfo,
    USBTransport,
    USBSpeed,
    open_usb_transport,
)

LEADTEK_WINFAST_DTV_DONGLE_GOLD_ID = USBDeviceID(0x0413, 0x6029)
LEADTEK_WINFAST_DTV_DONGLE_GOLD_NAME = "Leadtek WinFast DTV Dongle Gold"

EEPROM_ADDRESS = 0x50
EEPROM_SIZE = 256
EEPROM_TS_MODE = 0x31
EEPROM_DEMOD2_I2C = 0x32
EEPROM_XTAL_TYPE = 0x36
EEPROM_IF_LOW = 0x38
EEPROM_IF_HIGH = 0x39
EEPROM_MT2060_IF_LOW = 0x3A
EEPROM_MT2060_IF_HIGH = 0x3B
EEPROM_TUNER_ID = 0x3C
EEPROM_ADAPTER_OFFSET = 0x10

DEMODULATOR_ADDRESS = 0x1C
TDA18271_TUNER_ID = 0x9C
XTAL_CLOCKS = {0: 28_800_000, 1: 20_480_000, 2: 28_000_000, 3: 25_000_000}


class AF9015Device(DVBDevice):
    """Represent the physical AF9015 USB receiver and its logical DVB adapters.

    The device owns the USB bridge and composes one or two ``AF9015Adapter``
    instances from the receiver's EEPROM configuration.

    Locking:
        Unlike the standalone base-object default, this device owns one
        control lock and shares it with every configured adapter, frontend,
        and tuner. Stream-read synchronization remains in each demuxer.

    Attributes:
        bridge: Current USB-to-AF9015 command bridge; replaced after reboot.
        transport: USB transport used by the bridge and adapter stream readers.
        eeprom: Cached full EEPROM image, or ``None`` before configuration.
        eeprom_hash: Linux-compatible hash of the cached EEPROM, if read.
        firmware_size: Size of the last uploaded firmware image in bytes.
        firmware_checksum: Additive 16-bit checksum used for the second demod.
        adapters: Logical DVB adapters created from receiver EEPROM data.
        _hardware_initialized: Whether device and composed adapters finished
            their latest hardware initialization.
    """

    STREAM_ENDPOINTS = (0x84, 0x85)
    FIRMWARE_FILENAME = "dvb-usb-af9015.fw"
    FIRMWARE_CACHE_DIR = Path(__file__).resolve().parent / "firmware"
    FIRMWARE_URLS = (
        "https://github.com/OpenELEC/dvb-firmware/raw/refs/heads/master/firmware/dvb-usb-af9015.fw",
        "http://palosaari.fi/linux/v4l-dvb/firmware/af9015/5.24.0.0/dvb-usb-af9015.fw"
    )
    FIRMWARE_SHA256 = "2e734fa15da07ac75a9ae8aabd766c2b1e36cf10441077597b37f98e50188a71"
    SUPPORTED_USB_MODELS = {
        LEADTEK_WINFAST_DTV_DONGLE_GOLD_ID: LEADTEK_WINFAST_DTV_DONGLE_GOLD_NAME,
    }

    # From Linux driver:
    #
    # I2C multiplexing:
    # There could be two tuners, both using same I2C address. Demodulator
    # I2C-gate is only possibility to select correct tuner.
    #
    # ...........................................
    # . AF9015 integrates AF9013 demodulator    .
    # . ____________               ____________ .             ____________
    # .|   USB IF   |             |   demod    |.            |   tuner    |
    # .|------------|             |------------|.            |------------|
    # .|   AF9015   |             |   AF9013   |.            |   MXL5003  |
    # .|            |--+--I2C-----|-----/ -----|.----I2C-----|            |
    # .|            |  |          | addr 0x1c  |.            |  addr 0x63 |
    # .|____________|  |          |____________|.            |____________|
    # .................|.........................
    #                  |           ____________               ____________
    #                  |          |   demod    |             |   tuner    |
    #                  |          |------------|             |------------|
    #                  |          |   AF9013   |             |   MXL5003  |
    #                  +--I2C-----|-----/ -----|-----I2C-----|            |
    #                             | addr 0x1d  |             |  addr 0x63 |
    #                             |____________|             |____________|
    #

    @classmethod
    def open_usb_device(cls, device: USBDeviceInfo) -> "AF9015Device":
        """Open one enumerated AF9015 USB device."""
        transport = open_usb_transport(
            device,
            async_endpoints=cls.STREAM_ENDPOINTS,
        )
        return cls(transport, device.device_id)

    def __init__(
        self,
        transport: USBTransport,
        device_id: USBDeviceID | None = None,
    ) -> None:
        """Initialize device state and attach a bridge to the USB transport."""
        super().__init__(transport, device_id)
        self.firmware_size = 0
        self.firmware_checksum = 0
        self._reset_with_bridge(AF9015Bridge(transport))

    def _reset_with_bridge(self, bridge: AF9015Bridge) -> None:
        """Replace the bridge after USB reconnect and clear cached device state."""
        self.bridge = bridge
        self.transport = bridge.transport
        self.eeprom: bytes | None = None
        self.eeprom_hash: int | None = None
        self._reset_device_state()

    def is_warm(self) -> bool:
        """Return whether AF9015 firmware is running."""
        with self._control_lock:
            return self.bridge.get_state() == 0x02

    def log_transport_stats(self) -> None:
        """Log transfer statistics collected by the underlying USB transport."""
        self.bridge.transport.log_stats()

    def init_hardware(
        self,
        firmware: bytes | None = None,
        firmware_path: str | Path | None = None,
        download_firmware: bool = True,
    ) -> None:
        """Ensure firmware is running, compose adapters, and initialize hardware.

        Existing streams are stopped first. If firmware is cold, downloading
        can be explicitly disabled to make initialization fail rather than
        perform a firmware upload.
        """
        with self._control_lock:
            self._hardware_initialized = False
            for adapter in self.adapters:
                adapter.stop()
            if not self.is_warm():
                if not download_firmware:
                    raise RuntimeError(
                        "AF9015 firmware is not running; call init_hardware(download_firmware=True) "
                        "to download it explicitly"
                    )
                self.download_firmware(firmware, firmware_path)
            self._create_adapters()
            if len(self.adapters) > 1:
                self._start_secondary_demodulator(self.adapters[1].frontend.demodulator_address)
            for adapter in self.adapters:
                adapter.init_hardware()
            self._hardware_initialized = True

    def download_firmware(
        self,
        firmware: bytes | None = None,
        firmware_path: str | Path | None = None,
    ) -> None:
        """Upload firmware from bytes or a file, then reconnect the receiver."""
        with self._control_lock:
            for adapter in self.adapters:
                adapter.stop()
            if firmware is not None and firmware_path is not None:
                raise ValueError("provide firmware bytes or firmware_path, not both")
            if firmware is None:
                path = (
                    Path(firmware_path)
                    if firmware_path is not None
                    else self.resolve_firmware_path()
                )
                firmware = path.read_bytes()
            self.bridge.download_firmware(firmware)
            self.firmware_size = len(firmware)
            self.firmware_checksum = sum(firmware) & 0xFFFF
            self._reconnect()

    def _reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> int:
        """Reconnect USB and atomically replace the bridge after state probing."""
        with self._control_lock:
            new_transport = self.bridge.transport.reconnect(timeout, poll_interval)
            candidate_bridge = AF9015Bridge(new_transport)
            try:
                state = candidate_bridge.get_state()
            except Exception:
                new_transport.close()
                raise
            self._reset_with_bridge(candidate_bridge)
            return state

    def _create_adapters(self) -> None:
        """Read EEPROM configuration and create the device's adapter chains."""
        eeprom = self.eeprom if self.eeprom is not None else self._read_eeprom()
        adapter_count = 2 if eeprom[EEPROM_TS_MODE] else 1
        demod_addresses = [DEMODULATOR_ADDRESS]
        if adapter_count == 2:
            demod_addresses.append(eeprom[EEPROM_DEMOD2_I2C] >> 1)

        self.adapters = []
        for adapter_index in range(adapter_count):
            offset = adapter_index * EEPROM_ADAPTER_OFFSET
            tuner_id = eeprom[EEPROM_TUNER_ID + offset]
            spectrum_inverted = tuner_id not in {0x04, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F}
            demodulator_address = demod_addresses[adapter_index]
            clock_hz = XTAL_CLOCKS.get(eeprom[EEPROM_XTAL_TYPE + offset], 0)
            intermediate_frequency_hz = (
                int.from_bytes(eeprom[EEPROM_IF_LOW + offset : EEPROM_IF_HIGH + offset + 1], "little") * 1000
            )
            mt2060_if = int.from_bytes(
                eeprom[EEPROM_MT2060_IF_LOW + offset : EEPROM_MT2060_IF_HIGH + offset + 1], "little"
            )
            frontend = AF9013Frontend(
                self.bridge,
                demodulator_address,
                None,
                clock_hz,
                intermediate_frequency_hz,
                spectrum_inverted,
                tuner_id,
                adapter_index=adapter_index,
                mt2060_if=mt2060_if,
                control_lock=self._control_lock,
            )
            # TDA18271 sits behind the AF9013's I2C gate, so the frontend is its own bus.
            tuner = TDA18271Tuner(frontend) if tuner_id == TDA18271_TUNER_ID else None
            frontend.tuner = tuner
            demuxer = AF9015Demuxer(
                self.bridge,
                endpoint=self.STREAM_ENDPOINTS[adapter_index],
                adapter_index=adapter_index,
                dual_mode=adapter_count == 2,
                demodulator_address=demodulator_address,
                full_speed=self.bridge.transport.get_speed() == USBSpeed.FULL,
            )
            adapter = AF9015Adapter(frontend, demuxer)
            self._share_control_lock_with_adapter(adapter)
            self.adapters.append(adapter)

    def _read_eeprom(self) -> bytes:
        """Read and cache the complete 256-byte AF9015 EEPROM."""
        self.eeprom = bytes(
            self.bridge.read_i2c(EEPROM_ADDRESS, offset, 1)[0]
            for offset in range(EEPROM_SIZE)
        )
        self.eeprom_hash = self._eeprom_hash(self.eeprom)
        return self.eeprom

    def _eeprom_hash(self, data: bytes) -> int:
        """Match Linux's AF9015 golden-ratio EEPROM hash."""
        if len(data) != EEPROM_SIZE:
            raise ValueError("AF9015 EEPROM must contain 256 bytes")

        GOLDEN_RATIO_PRIME = 0x9E370001 # 2^31 + 2^29 - 2^25 + 2^22 - 2^19 - 2^16 + 1
        result = 0
        for offset in range(0, EEPROM_SIZE, 4):
            result = ((result * GOLDEN_RATIO_PRIME) + int.from_bytes(data[offset : offset + 4], "little")) & 0xFFFFFFFF
        return result

    def _start_secondary_demodulator(self, address: int) -> None:
        """Copy the staged firmware into a cold second AF9013 and await startup.

        A running demodulator is left untouched. Firmware failure and timeout
        are reported distinctly so callers can distinguish device errors from
        a demodulator that never reached its running state.
        """
        status = self.bridge.read_i2c_byte(address, 0x98BE, address_length=3)
        if status == 0x0C:
            return
        self.bridge.write_memory(0xD416, b"\x04")
        self.bridge.copy_firmware(self.firmware_size, self.firmware_checksum)
        self.bridge.write_memory(0xD416, b"\x14")
        self.bridge.write_i2c_byte(address, 0xE205, 0x01, address_length=3, mailbox=1)

        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            status = self.bridge.read_i2c_byte(address, 0x98BE, address_length=3)
            if status == 0x0C:
                return
            if status == 0x04:
                raise IOError("secondary AF9013 demodulator firmware failed")
            sleep(0.02)
        raise TimeoutError("secondary AF9013 demodulator firmware timed out")
