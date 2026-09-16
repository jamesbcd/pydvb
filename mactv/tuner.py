"""Tuner interfaces and the AF9015-supported TDA18271 implementation."""

from dataclasses import dataclass
from time import sleep
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .frontend import AF9013Frontend


@dataclass(frozen=True)
class Tuning:
    frequency_hz: int
    bandwidth_hz: int = 8_000_000
    transmission_mode: str = "auto"
    guard_interval: str = "auto"
    hierarchy: str = "auto"
    modulation: str = "auto"
    code_rate_hp: str = "auto"
    code_rate_lp: str = "auto"


class Tuner(Protocol):
    def init(self) -> None: ...

    def tune(self, tuning: Tuning) -> None: ...

    def sleep(self) -> None: ...


class I2CTuner:
    """Base class for chip-specific tuner drivers."""

    def __init__(self, frontend: "AF9013Frontend", i2c_address: int) -> None:
        self.frontend = frontend
        self.i2c_address = i2c_address

    def init(self) -> None:
        pass

    def tune(self, tuning: Tuning) -> None:
        raise NotImplementedError("a chip-specific tuner must implement tune()")

    def sleep(self) -> None:
        pass


class TDA18271Tuner(I2CTuner):
    """Digital DVB-T driver for the NXP TDA18271 silicon tuner.

    This follows the Linux ``tda18271`` driver's register-cache model. The
    tuner is addressed at 0x60 on AF9015 boards and accepts a one-byte
    register followed by register data. The AF9015 I2C bridge limits writes
    to 21 bytes including that register byte, so Linux's 16-byte init mode is
    used here.
    """

    I2C_ADDRESS = 0x60
    REGISTER_COUNT = 39
    INIT_CHUNK_SIZE = 16
    MIN_FREQUENCY_HZ = 45_000_000
    MAX_FREQUENCY_HZ = 1_000_000_000

    REG_ID = 0
    REG_TM = 1
    REG_PL = 2
    REG_EP1 = 3
    REG_EP2 = 4
    REG_EP3 = 5
    REG_EP4 = 6
    REG_EP5 = 7
    REG_CPD = 8
    REG_CD1 = 9
    REG_CD2 = 10
    REG_CD3 = 11
    REG_MPD = 12
    REG_MD1 = 13
    REG_MD2 = 14
    REG_MD3 = 15
    REG_EB1 = 16
    REG_EB2 = 17
    REG_EB3 = 18
    REG_EB4 = 19
    REG_EB5 = 20
    REG_EB6 = 21
    REG_EB7 = 22
    REG_EB8 = 23
    REG_EB9 = 24
    REG_EB10 = 25
    REG_EB11 = 26
    REG_EB12 = 27
    REG_EB13 = 28
    REG_EB14 = 29
    REG_EB15 = 30
    REG_EB16 = 31
    REG_EB17 = 32
    REG_EB18 = 33
    REG_EB19 = 34
    REG_EB20 = 35
    REG_EB21 = 36
    REG_EB22 = 37
    REG_EB23 = 38

    # HDC2 defaults from Linux tda18271_init_regs().
    DEFAULT_REGISTERS = bytes(
        (
            0x84, 0x08, 0x80, 0xC6, 0xDF, 0x16, 0x60, 0x80,
            0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0xFC, 0x01, 0x84, 0x41, 0x01, 0x84, 0x40, 0x07,
            0x00, 0x00, 0x96, 0x33, 0xC1, 0x00, 0x8F, 0x00,
            0x00, 0x8C, 0x00, 0x20, 0xB3, 0x48, 0xB0,
        )
    )

    # DVB-T standard map: IF, AGC mode, standard code, IF level, RF top.
    STANDARD_MAP = {
        6_000_000: (4_570_000, 0x02, 0x04, 0x01, 0x48),
        7_000_000: (4_570_000, 0x02, 0x04, 0x01, 0x48),
        8_000_000: (4_570_000, 0x02, 0x04, 0x01, 0x48),
    }

    RF_BAND_MAP = (
        (61_100_000, 0x00),
        (87_500_000, 0x20),
        (162_500_000, 0x40),
        (230_000_000, 0x60),
        (425_000_000, 0x80),
        (625_000_000, 0xA0),
        (850_000_000, 0xC0),
        (1_000_000_000, 0xE0),
    )
    GAIN_TAPER_MAP = (
        (61_100_000, 0x00),
        (110_000_000, 0x04),
        (220_000_000, 0x08),
        (440_000_000, 0x10),
        (700_000_000, 0x18),
        (1_000_000_000, 0x1F),
    )
    RF_CAL_MAP = (
        (110_000_000, 0x10),
        (220_000_000, 0x20),
        (440_000_000, 0x30),
        (700_000_000, 0x40),
        (1_000_000_000, 0x50),
    )

    def __init__(self, frontend: "AF9013Frontend", i2c_address: int = I2C_ADDRESS) -> None:
        super().__init__(frontend, i2c_address)
        if i2c_address != self.I2C_ADDRESS:
            raise ValueError("TDA18271 must use I2C address 0x60")
        self._registers = bytearray(self.DEFAULT_REGISTERS)
        self._initialized = False
        self._sleeping = True
        self.current_tuning: Tuning | None = None

    def _read(self, register: int, length: int) -> bytes:
        return self.frontend.gated_read_register(self.i2c_address, register, length)

    def _write(self, register: int, data: bytes) -> None:
        if not 0 <= register < self.REGISTER_COUNT:
            raise ValueError("TDA18271 register is out of range")
        if register + len(data) > self.REGISTER_COUNT:
            raise ValueError("TDA18271 register write exceeds register map")
        self.frontend.gated_write_register(self.i2c_address, register, data)
        self._registers[register : register + len(data)] = data

    def _write_cached(self, register: int, length: int) -> None:
        self._write(register, bytes(self._registers[register : register + length]))

    def init(self) -> None:
        chip_id = self._read(self.REG_ID, 1)[0]
        if chip_id not in (0x83, 0x84):
            raise IOError(f"unexpected TDA18271 ID 0x{chip_id:02x}")
        self._registers[:] = self.DEFAULT_REGISTERS
        for register in range(0, self.REGISTER_COUNT, self.INIT_CHUNK_SIZE):
            length = min(self.INIT_CHUNK_SIZE, self.REGISTER_COUNT - register)
            self._write_cached(register, length)
        self._initialized = True
        self._sleeping = False

    @staticmethod
    def _pll_divider(frequency_hz: int) -> tuple[int, int, int]:
        """Return Linux-compatible post-divider, multiplier, and divider."""
        frequency_khz = frequency_hz // 1000
        for limit_khz, post_divider, multiplier in (
            (55_000, 0, 1),
            (110_000, 1, 2),
            (220_000, 2, 4),
            (440_000, 3, 8),
            (880_000, 4, 16),
            (1_760_000, 5, 32),
        ):
            if frequency_khz <= limit_khz:
                divider = ((multiplier * frequency_khz) << 7) // 125
                return post_divider, multiplier, divider
        raise ValueError("frequency is outside the TDA18271 PLL range")

    def _set_main_pll(self, frequency_hz: int) -> None:
        post_divider, _multiplier, divider = self._pll_divider(frequency_hz)
        self._registers[self.REG_MPD] = post_divider
        self._registers[self.REG_MD1] = (divider >> 16) & 0x7F
        self._registers[self.REG_MD2] = (divider >> 8) & 0xFF
        self._registers[self.REG_MD3] = divider & 0xFF

    @staticmethod
    def _map_value(frequency_hz: int, table: tuple[tuple[int, int], ...]) -> int:
        for limit, value in table:
            if frequency_hz <= limit:
                return value
        raise ValueError("frequency is outside the TDA18271 lookup maps")

    def _apply_rf_maps(self, frequency_hz: int) -> None:
        self._registers[self.REG_EP1] = (self._registers[self.REG_EP1] & 0xF8) | 0x04
        self._registers[self.REG_EP2] = self._map_value(frequency_hz, self.RF_BAND_MAP)
        self._registers[self.REG_EP2] |= self._map_value(frequency_hz, self.GAIN_TAPER_MAP) & 0x1F
        self._registers[self.REG_EB13] = (self._registers[self.REG_EB13] & 0x83) | 0x18
        self._registers[self.REG_EP5] = (self._registers[self.REG_EP5] & 0xF8) | 0x04
        if frequency_hz <= 61_100_000:
            self._registers[self.REG_EB14] = self._map_value(frequency_hz, self.RF_CAL_MAP)

    def tune(self, tuning: Tuning) -> None:
        if not self._initialized:
            self.init()
        if not self.MIN_FREQUENCY_HZ <= tuning.frequency_hz <= self.MAX_FREQUENCY_HZ:
            raise ValueError("frequency is outside the TDA18271 range")
        if tuning.bandwidth_hz not in self.STANDARD_MAP:
            raise ValueError("TDA18271 DVB-T bandwidth must be 6, 7, or 8 MHz")

        if_frequency, agc_mode, standard, if_level, rfagc_top = self.STANDARD_MAP[tuning.bandwidth_hz]
        self._apply_rf_maps(tuning.frequency_hz)
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0xE0) | (agc_mode << 3) | standard
        self._registers[self.REG_EP4] = (self._registers[self.REG_EP4] & 0xE3) | (if_level << 2)
        self._registers[self.REG_EB22] = (self._registers[self.REG_EB22] & 0x80) | rfagc_top
        self._registers[self.REG_EP3] &= 0x1F
        self._set_main_pll(tuning.frequency_hz + if_frequency)

        self._write_cached(self.REG_EP1, self.REG_MD3 - self.REG_EP1 + 1)
        self._write_cached(self.REG_EB13, 1)
        self._write_cached(self.REG_EB22, 1)
        self._registers[self.REG_EP1] |= 0x80
        self._write_cached(self.REG_EP1, 1)
        sleep(0.005)
        self.current_tuning = tuning
        self._sleeping = False

    def sleep(self) -> None:
        if not self._initialized or self._sleeping:
            return
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0x1F) | 0xE0
        self._write_cached(self.REG_EP3, 1)
        self._sleeping = True
