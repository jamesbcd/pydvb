"""High-level AF9015 device composition."""

from pathlib import Path
from time import monotonic, sleep

from .adapter import DTVAdapterConfig, DTVAdapter
from .demux import TsDemuxer
from .frontend import AF9013Frontend, FrontendStatus
from .bridge import AF9015Bridge
from .tuner import TDA18271Tuner, Tuning
from .usb import PyUsbTransport, UsbTransport

DEFAULT_FIRMWARE_PATH = Path(__file__).parent / "firmware" / "dvb-usb-af9015.fw"

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


class Af9015Device:
    """Compose the bridge, I2C bus, and one or two DVB adapter chains (demodulator/tuner combinations)."""

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
    def open(cls, vendor_id: int, product_id: int) -> "Af9015Device":
        return cls(PyUsbTransport.find(vendor_id, product_id), vendor_id, product_id)

    def __init__(
        self,
        transport: UsbTransport,
        vendor_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.firmware_size = 0
        self.firmware_checksum = 0
        bridge = AF9015Bridge(transport)
        self._reset_with_bridge(bridge)

    def _reset_with_bridge(self, bridge: AF9015Bridge) -> None:
        self.bridge = bridge
        self.adapters: list[DTVAdapter] = []
        self.eeprom: bytes | None = None
        self.eeprom_hash: int | None = None
 
    def close(self) -> None:
        self.bridge.transport.close()

    def __enter__(self) -> "Af9015Device":
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()

    @property
    def adapter_count(self) -> int:
        return len(self.adapters) or 1

    @property
    def is_warm(self) -> bool:
        return self.bridge.get_state() == 0x02

    def initialize(
        self,
        firmware: bytes | None = None,
        firmware_path: str | Path | None = None,
        download_firmware: bool = True,
    ) -> None:
        """Bring the device to the configured state used by the Linux driver."""
        if not self.is_warm:
            if not download_firmware:
                raise RuntimeError(
                    "AF9015 firmware is not running; call initialize(download_firmware=True) "
                    "to download it explicitly"
                )
            self.download_firmware(firmware, firmware_path)
        self.create_adapters()
        if len(self.adapters) > 1:
            self._start_secondary_demodulator(self.adapters[1].config.demod_address)

    def download_firmware(
        self,
        firmware: bytes | None = None,
        firmware_path: str | Path | None = None,
    ) -> None:
        """Download firmware bytes, defaulting to the bundled AF9015 image."""
        if firmware is not None and firmware_path is not None:
            raise ValueError("provide firmware bytes or firmware_path, not both")
        if firmware is None:
            path = Path(firmware_path) if firmware_path is not None else DEFAULT_FIRMWARE_PATH
            firmware = path.read_bytes()
        self.bridge.download_firmware(firmware)
        self.firmware_size = len(firmware)
        self.firmware_checksum = sum(firmware) & 0xFFFF
        self.reconnect()

    def reconnect(
        self,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> int:
        """Replace the USB handle after firmware reset and return its state.

        This is intended for the reset/re-enumeration caused by firmware boot.
        The new handle is accepted only after a ``GET_CONFIG`` transaction
        succeeds.
        """
        new_transport = self.bridge.transport.reconnect(timeout, poll_interval)
        candidate_bridge = AF9015Bridge(new_transport)
        try:
            state = candidate_bridge.get_state()
        except Exception:
            new_transport.close()
            raise
        self._reset_with_bridge(candidate_bridge)
        return state

    def _start_secondary_demodulator(self, address: int) -> None:
        status = self.bridge.read_i2c_byte(address, 0x98BE, address_length=3)
        if status == 0x0C:
            return
        self.bridge.write_memory(0xD416, b"\x04")
        self.bridge.copy_firmware(self.firmware_size, self.firmware_checksum)
        self.bridge.write_memory(0xD416, b"\x14")
        self.bridge.write_i2c_byte(address, 0xE205, 0x01, address_length=3)

        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            status = self.bridge.read_i2c_byte(address, 0x98BE, address_length=3)
            if status == 0x0C:
                return
            if status == 0x04:
                raise IOError("secondary AF9013 demodulator firmware failed")
            sleep(0.02)
        raise TimeoutError("secondary AF9013 demodulator firmware timed out")

    def read_eeprom(self) -> bytes:
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
        result = 0
        for offset in range(0, EEPROM_SIZE, 4):
            result = ((result * 0x9E370001) + int.from_bytes(data[offset : offset + 4], "little")) & 0xFFFFFFFF
        return result

    def create_adapters(self) -> None:
        """Read EEPROM configuration and create the device's adapter chains."""
        eeprom = self.eeprom if self.eeprom is not None else self.read_eeprom()
        adapter_count = 2 if eeprom[EEPROM_TS_MODE] else 1
        demod_addresses = [DEMODULATOR_ADDRESS]
        if adapter_count == 2:
            demod_addresses.append(eeprom[EEPROM_DEMOD2_I2C] >> 1)

        self.adapters = []
        for adapter in range(adapter_count):
            offset = adapter * EEPROM_ADAPTER_OFFSET
            tuner_id = eeprom[EEPROM_TUNER_ID + offset]
            spectrum_inverted = tuner_id not in {0x04, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F}
            config = DTVAdapterConfig(
                demod_address=demod_addresses[adapter],
                clock_hz=XTAL_CLOCKS.get(eeprom[EEPROM_XTAL_TYPE + offset], 0),
                intermed_freq_hz=(
                    int.from_bytes(eeprom[EEPROM_IF_LOW + offset : EEPROM_IF_HIGH + offset + 1], "little") * 1000
                ),
                mt2060_if=int.from_bytes(
                    eeprom[EEPROM_MT2060_IF_LOW + offset : EEPROM_MT2060_IF_HIGH + offset + 1], "little"
                ),
                tuner_id=tuner_id,
                spectrum_inverted=spectrum_inverted,
            )
            frontend = (
                AF9013Frontend(
                    self.bridge,
                    config.demod_address,
                    None,
                    config.clock_hz,
                    config.intermed_freq_hz,
                    config.spectrum_inverted,
                )
                if config.tuner_id == TDA18271_TUNER_ID
                else None
            )
            # TDA18271 sits behind the AF9013's I2C gate, so the frontend is its own bus.
            tuner = TDA18271Tuner(frontend) if frontend is not None else None
            if frontend is not None:
                frontend.tuner = tuner
            demuxer = TsDemuxer(
                self.bridge,
                adapter=adapter,
                dual_mode=adapter_count == 2,
                demodulator_address=config.demod_address,
                full_speed=bool(getattr(self.bridge.transport, "is_full_speed", False)),
            )
            self.adapters.append(DTVAdapter(config, demuxer, tuner, frontend))

    def tune(self, adapter: int, tuning: Tuning, wait_for_lock: bool = True) -> FrontendStatus:
        """Tune one adapter's AF9013/TDA18271 frontend as a unit."""
        if not 0 <= adapter < len(self.adapters) or self.adapters[adapter].frontend is None:
            raise ValueError("adapter has no supported AF9013/TDA18271 frontend")
        frontend = self.adapters[adapter].frontend
        assert frontend is not None
        frontend.tune(tuning)
        return frontend.wait_for_lock() if wait_for_lock else frontend.read_status()

