"""AF9013 DVB-T frontend control behind an AF9015 bridge."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep

from .bridge import AF9015Bridge
from .tuner import Tuner, Tuning


class Af9013TunerBus:
    """Expose the tuner bus behind an AF9013 digital I2C gate."""

    GATE_REGISTER = 0xD417
    GATE_MAILBOX = 1
    GATE_MASK = 0x08

    def __init__(self, bus: AF9015Bridge, demodulator_address: int) -> None:
        self.bus = bus
        self.demodulator_address = demodulator_address

    def _gate(self, enabled: bool) -> None:
        current = self.bus.read_i2c(
            self.demodulator_address,
            self.GATE_REGISTER,
            1,
            address_length=3,
            mailbox=self.GATE_MAILBOX,
        )[0]
        value = (current & ~self.GATE_MASK) | (self.GATE_MASK if enabled else 0)
        self.bus.write_i2c(
            self.demodulator_address,
            self.GATE_REGISTER,
            bytes((value,)),
            address_length=3,
            mailbox=self.GATE_MAILBOX,
        )

    def read(self, address: int, register: int, length: int) -> bytes:
        self._gate(True)
        try:
            return self.bus.read_i2c(address, register, length)
        finally:
            self._gate(False)

    def write(self, address: int, register: int, data: bytes) -> None:
        self._gate(True)
        try:
            self.bus.write_i2c(address, register, data)
        finally:
            self._gate(False)


@dataclass(frozen=True)
class FrontendStatus:
    """The lock state exposed by the AF9013 MPEG/TPS status bits."""

    signal: bool
    carrier: bool
    viterbi: bool
    sync: bool
    lock: bool


class AF9013Frontend:
    """Configure one AF9013 demodulator and its attached tuner."""

    API_VERSION = b"\x00\x01\x09\x00"
    TS_MODE_USB = 0
    REG_API_VERSION = 0x9BF2
    REG_ADC_CONTROL = 0xD73A
    REG_RESET = 0xD417
    REG_TS_CONTROL = 0xD500
    REG_FREQUENCY_CONTROL = 0xD140
    REG_FREQUENCY_CONTROL_EXT = 0x9BE7
    REG_TPS_STATUS = 0xD330
    REG_MPEG_STATUS = 0xD507
    REG_TRANSMISSION = 0xD3C0
    REG_EASY_MODE = 0xAEFD
    REG_EASY_MODE_VALUE = 0xAEFE
    REG_PID_FILTER = 0xD503

    def __init__(
        self,
        bus: AF9015Bridge,
        demodulator_address: int,
        tuner: Tuner,
        clock_hz: int,
        intermediate_frequency_hz: int,
        spectrum_inverted: bool,
    ) -> None:
        self.bus = bus
        self.demodulator_address = demodulator_address
        self.tuner = tuner
        self.clock_hz = clock_hz
        self.intermediate_frequency_hz = intermediate_frequency_hz
        self.spectrum_inverted = spectrum_inverted
        self._initialized = False
        self._sleeping = True
        self.current_tuning: Tuning | None = None

    def _read(self, register: int, length: int = 1) -> bytes:
        return self.bus.read_i2c(
            self.demodulator_address,
            register,
            length,
            address_length=3,
            mailbox=0,
        )

    def _write(self, register: int, data: bytes) -> None:
        self.bus.write_i2c(
            self.demodulator_address,
            register,
            data,
            address_length=3,
            mailbox=0,
        )

    def _update_bits(self, register: int, mask: int, value: int) -> None:
        current = self._read(register)[0]
        self._write(register, bytes(((current & ~mask) | (value & mask),)))

    def init(self) -> None:
        self._update_bits(self.REG_ADC_CONTROL, 0x08, 0)
        self._update_bits(self.REG_RESET, 0x02, 0)
        self._update_bits(self.REG_RESET, 0x10, 0)
        self._write(self.REG_API_VERSION, self.API_VERSION)
        self._update_bits(self.REG_TS_CONTROL, 0x0E, self.TS_MODE_USB << 1)
        self._initialized = True
        self._sleeping = False

    def tune(self, tuning: Tuning) -> None:
        if not self._initialized:
            self.init()
        if self.clock_hz <= 0 or self.intermediate_frequency_hz <= 0:
            raise ValueError("AF9013 clock and IF must be configured")

        self.tuner.tune(tuning)
        sampling_frequency = self.intermediate_frequency_hz
        while sampling_frequency > self.clock_hz // 2:
            sampling_frequency -= self.clock_hz
        inverted = self.spectrum_inverted if sampling_frequency < 0 else not self.spectrum_inverted
        sampling_frequency = abs(sampling_frequency)
        frequency_word = round(sampling_frequency * 0x800000 / self.clock_hz)
        if inverted:
            frequency_word = 0x800000 - frequency_word

        frequency_data = bytes(
            (
                frequency_word & 0xFF,
                (frequency_word >> 8) & 0xFF,
                (frequency_word >> 16) & 0x7F,
            )
        )
        complement = 0x800000 - frequency_word
        complement_data = bytes(
            (
                complement & 0xFF,
                (complement >> 8) & 0xFF,
                (complement >> 16) & 0x7F,
            )
        )
        self._write(self.REG_FREQUENCY_CONTROL, frequency_data)
        self._write(self.REG_FREQUENCY_CONTROL_EXT, frequency_data + complement_data)

        bandwidth_code = {6_000_000: 0, 7_000_000: 1, 8_000_000: 2}.get(tuning.bandwidth_hz)
        if bandwidth_code is None:
            raise ValueError("AF9013 DVB-T bandwidth must be 6, 7, or 8 MHz")
        transmission = bytearray(self._read(self.REG_TRANSMISSION, 3))
        transmission[1] = (transmission[1] & 0xF3) | (bandwidth_code << 2)
        self._write(self.REG_TRANSMISSION, bytes(transmission))
        self._write(self.REG_EASY_MODE, b"\x00")
        self._write(0xFFFF, b"\x00")
        self.current_tuning = tuning
        self._sleeping = False

    def read_status(self) -> FrontendStatus:
        mpeg_locked = bool(self._read(self.REG_MPEG_STATUS)[0] & 0x40)
        tps_locked = bool(self._read(self.REG_TPS_STATUS)[0] & 0x08)
        if mpeg_locked:
            return FrontendStatus(True, True, True, True, True)
        return FrontendStatus(tps_locked, tps_locked, tps_locked, False, False)

    def wait_for_lock(self, timeout: float = 4.0, poll_interval: float = 0.05) -> FrontendStatus:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            status = self.read_status()
            if status.lock:
                return status
            sleep(poll_interval)
        return self.read_status()

    def sleep(self) -> None:
        if not self._initialized or self._sleeping:
            return
        self._update_bits(self.REG_RESET, 0x10, 0x10)
        self._write(0xAEFF, b"\x01")
        self._update_bits(self.REG_ADC_CONTROL, 0x08, 0x08)
        self.tuner.sleep()
        self._sleeping = True