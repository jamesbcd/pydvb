"""High-level AF9015 device composition."""

from pathlib import Path
from threading import RLock
from time import monotonic, sleep

from .usb import PyUSBTransport, USBTransport, USBSpeed
from .bridge import AF9015Bridge
from .adapter import AF9015Adapter
from .frontend import AF9013Frontend, FrontendStatus
from .demux import TSDemuxer
from .tuner import TDA18271Tuner, Tuning

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


class AF9015Device:
    """Represent the physical AF9015 USB receiver and its logical DVB adapters.

    The device owns the USB bridge and composes one or two ``AF9015Adapter``
    instances from the receiver's EEPROM configuration.
    """

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
    def open(cls, vendor_id: int, product_id: int) -> "AF9015Device":
        return cls(PyUSBTransport.find(vendor_id, product_id), vendor_id, product_id)

    def __init__(
        self,
        transport: USBTransport,
        vendor_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.firmware_size = 0
        self.firmware_checksum = 0
        self._hardware_initialized = False
        self._control_lock = RLock() # Lock to protect device control operations
        bridge = AF9015Bridge(transport)
        self._reset_with_bridge(bridge)

    def _reset_with_bridge(self, bridge: AF9015Bridge) -> None:
        self.bridge = bridge
        self.adapters: list[AF9015Adapter] = []
        self.eeprom: bytes | None = None
        self.eeprom_hash: int | None = None
        self._hardware_initialized = False

    def _require_initialized(self) -> None:
        if not self._hardware_initialized:
            raise RuntimeError("call init_hardware() before using configured adapters")
 
    def _get_adapter(self, adapter_index: int) -> AF9015Adapter:
        if not 0 <= adapter_index < len(self.adapters):
            raise ValueError(f"adapter_index {adapter_index} is not configured")
        return self.adapters[adapter_index]

    def __enter__(self) -> "AF9015Device":
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()

    @property
    def adapter_count(self) -> int:
        """Return the number of configured logical adapters.

        Threading:
            Safe to read concurrently with device control operations. The
            result is a snapshot and may change during initialization.
        """
        with self._control_lock:
            self._require_initialized()
            return len(self.adapters) or 1

    @property
    def is_warm(self) -> bool:
        """Return whether the AF9015 firmware is running.

        Threading:
            Safe to call concurrently with other device control operations;
            device control operations are serialized and this may block on
            USB I/O.
        """
        with self._control_lock:
            return self.bridge.get_state() == 0x02

    def close(self) -> None:
        """Stop configured adapters and release the USB transport.

        Threading:
            Safe to call concurrently with device control operations. The
            operation is serialized and waits for active adapter reads to
            stop before disposing the USB handle.
        """
        with self._control_lock:
            try:
                for adapter in self.adapters:
                    adapter.stop()
            finally:
                self.bridge.transport.close()

    def init_hardware(
        self,
        firmware: bytes | None = None,
        firmware_path: str | Path | None = None,
        download_firmware: bool = True,
    ) -> None:
        """Bring the device to the configured state used by the Linux driver.

        Threading:
            Safe to call concurrently with other device control operations;
            the complete initialization and any firmware reconnect are
            serialized. This method may block on USB I/O and device
            re-enumeration.
        """
        with self._control_lock:
            self._hardware_initialized = False
            for adapter in self.adapters:
                # We only reach this point if the adapters have been created previously,
                # which means we are re-initializing the hardware and need to stop them first.
                adapter.stop()
            if not self.is_warm:
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
        """Download firmware bytes, defaulting to the bundled AF9015 image.

        Threading:
            Safe to call concurrently with other device control operations;
            downloading and USB re-enumeration are serialized. This method
            may block on USB I/O and device re-enumeration.
        """
        with self._control_lock:
            for adapter in self.adapters:
                adapter.stop()
            if firmware is not None and firmware_path is not None:
                raise ValueError("provide firmware bytes or firmware_path, not both")
            if firmware is None:
                path = Path(firmware_path) if firmware_path is not None else DEFAULT_FIRMWARE_PATH
                firmware = path.read_bytes()
            self.bridge.download_firmware(firmware)
            self.firmware_size = len(firmware)
            self.firmware_checksum = sum(firmware) & 0xFFFF
            self._reconnect()

    def _reconnect(
        self,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> int:
        """Replace the USB handle after firmware reset and return its state.

        This is intended for the reset/re-enumeration caused by firmware boot.
        The new handle is accepted only after a ``GET_CONFIG`` transaction
        succeeds.
        """
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
            )
            # TDA18271 sits behind the AF9013's I2C gate, so the frontend is its own bus.
            tuner = TDA18271Tuner(frontend) if tuner_id == TDA18271_TUNER_ID else None
            frontend.tuner = tuner
            demuxer = TSDemuxer(
                self.bridge,
                adapter_index=adapter_index,
                dual_mode=adapter_count == 2,
                demodulator_address=demodulator_address,
                full_speed=self.bridge.transport.get_speed() == USBSpeed.FULL,
            )
            self.adapters.append(AF9015Adapter(frontend, demuxer))

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

    def sleep(self) -> None:
        """Put all configured frontends into their low-power state.

        Threading:
            Safe to call concurrently with other device control operations;
            all frontend sleep operations are serialized across adapters and
            may block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            for adapter in self.adapters:
                adapter.sleep()

    def tune(self, tuning: Tuning, adapter_index: int = 0, wait_for_lock: bool = True) -> FrontendStatus:
        """Tune one adapter's AF9013/TDA18271 frontend as a unit.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized. Tuning is
            rejected while the selected adapter is streaming.
        """
        with self._control_lock:
            self._require_initialized()
            adapter = self._get_adapter(adapter_index)
            if adapter.tuner is None:
                raise ValueError("adapter_index has no supported AF9013/TDA18271 frontend")
            return adapter.tune(tuning, wait_for_lock)

    def read_status(self, adapter_index: int = 0) -> FrontendStatus:
        """Read one adapter's frontend status.

        Threading:
            Safe to call concurrently with other device control operations;
            device control operations are serialized and this may block on
            USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            return self._get_adapter(adapter_index).read_status()

    def start(self, adapter_index: int = 0) -> None:
        """Start transport-stream delivery for one adapter.

        Threading:
            Safe to call concurrently with other device control operations;
            lifecycle and control operations across all adapters are
            serialized. This may wait for an in-flight read.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).start()

    def stop(self, adapter_index: int = 0) -> None:
        """Stop transport-stream delivery for one adapter.

        Threading:
            Safe to call concurrently with other device control operations;
            lifecycle and control operations across all adapters are
            serialized. This waits for an in-flight read before returning.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).stop()

    def read_ts(self, timeout_ms: int = 2000, adapter_index: int = 0) -> bytes:
        """Read a transport-stream chunk from one adapter.

        Threading:
            The adapter permits one active read. This method snapshots the
            adapter under the device lock, then releases that lock while USB
            blocks so another device thread can initiate shutdown.
        """
        with self._control_lock:
            self._require_initialized()
            adapter = self._get_adapter(adapter_index)
        return adapter.read_ts(timeout_ms)

    def set_pid_filter(self, pid: int, enabled: bool = True, adapter_index: int = 0) -> None:
        """Enable or disable one adapter's hardware PID filter slot.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).set_pid_filter(pid, enabled)

    def enable_pid_filter(self, enabled: bool = True, adapter_index: int = 0) -> None:
        """Enable or disable one adapter's AF9013 PID filtering.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).enable_pid_filter(enabled)



