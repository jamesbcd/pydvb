"""AF9013 DVB-T frontend control behind an AF9015 bridge."""

from __future__ import annotations

from math import log10
from time import monotonic, sleep

from .bridge import AF9015Bridge
from ...driver_api.frontend import DVBFrontend, DVBFrontendStatus
from ...driver_api.tuner import CodeRate, DVBTuning, DVBTuner, GuardInterval, Hierarchy, Modulation, TransmissionMode


DEFAULT_LOCK_TIMEOUT: float | None = 60.0


class AF9013Frontend(DVBFrontend):
    """Control one AF9013 DVB-T demodulator and its attached tuner.

    The frontend is the tuner's I2C gate and owns tuner configuration and
    tuning state; transport-stream demultiplexing belongs to the adapter.
    """

    API_VERSION = b"\x00\x01\x09\x00"

    # Tuner I2C gate: same register as REG_RESET, gated via a separate mailbox.
    GATE_REGISTER = 0xD417
    GATE_MASK = 0x08

    TS_MODE_USB = 0
    TS_MODE_PARALLEL = 1
    TS_MODE_SERIAL = 2

    TS_OUTPUT_PIN_DEFAULT = 0
    TS_OUTPUT_PIN_7 = 7
    GPIO_ON = 1 << 0
    GPIO_EN = 1 << 1
    GPIO_OUTPUT = 1 << 2
    GPIO_INPUT = 1 << 3
    GPIO_LO = GPIO_ON | GPIO_EN
    GPIO_HI = GPIO_ON | GPIO_EN | GPIO_OUTPUT
    GPIO_TUNER_ON = GPIO_ON | GPIO_EN
    GPIO_TUNER_OFF = GPIO_ON | GPIO_EN | GPIO_OUTPUT

    REG_API_VERSION = 0x9BF2
    REG_ADC_CONTROL = 0xD73A
    REG_RESET = 0xD417
    REG_TS_CONTROL = 0xD500
    REG_GPIO_01 = 0xD735
    REG_GPIO_23 = 0xD736
    REG_FREQUENCY_CONTROL = 0xD140
    REG_FREQUENCY_CONTROL_EXT = 0x9BE7
    REG_TPS_STATUS = 0xD330
    REG_MPEG_STATUS = 0xD507
    REG_TRANSMISSION = 0xD3C0
    REG_EASY_MODE = 0xAEFD
    REG_EASY_MODE_VALUE = 0xAEFE
    REG_PID_FILTER = 0xD503
    REG_ADC_CLOCK = 0x9BD2
    REG_SAMPLING_CLOCK = 0xD180
    REG_CFOE = 0xAE00
    REG_EMPTY_CHANNEL = 0x9BFE
    REG_EMPTY_DVBT_CHANNEL = 0x9BC2
    REG_LOCK_LED = 0xD730
    REG_RESET_EXECUTION = 0xAEFF
    REG_SIGNAL_AGC = 0xD07C
    REG_CNR_READY = 0xD2E1
    REG_CNR = 0xD2E3
    REG_MODULATION = 0xD3C1
    REG_BER_READY = 0xD391
    REG_BER_COUNTER = 0xD385

    CLOCK_CODES = {28_800_000: 0,20_480_000: 1, 28_000_000: 2, 25_000_000: 3}

    # Carrier Frequency Offset Estimation (CFOE) coefficients are hardcoded look-Up table (LUT) configuration
    # values used by the demodulator to compensate for frequency drifts and offsets. They depend on the
    # demodulator clock frequency and the signal bandwidth.
    COEFFICIENTS = {
        (28_800_000, 8_000_000): bytes.fromhex("028a28a30514511100a28f3d00a28a2900a2851401451414"),
        (28_800_000, 7_000_000): bytes.fromhex("0238e38e0471c707008e3d55008e38e4008e3472011c7132"),
        (28_800_000, 6_000_000): bytes.fromhex("01e79e7a03cf3c3d0079eb6e0079e79e0079e3cf00f3cf0f"),
        (20_480_000, 8_000_000): bytes.fromhex("039249260724921300e4996e00e4924900e48b2501c92425"),
        (20_480_000, 7_000_000): bytes.fromhex("032000010640000000c8064000c8000000c7f9c001900000"),
        (20_480_000, 6_000_000): bytes.fromhex("02adb6dc055b6d2e00ab731300ab6db700ab685c0156db1c"),
        (28_000_000, 8_000_000): bytes.fromhex("029cbc150539780a00a7343f00a72f0500a729cc014e5e03"),
        (28_000_000, 7_000_000): bytes.fromhex("024924920492490900924db7009249250092449201249212"),
        (28_000_000, 6_000_000): bytes.fromhex("01f58d1003eb1a08007d672f007d6344007d5f5900fac622"),
        (25_000_000, 8_000_000): bytes.fromhex("02ecfb9d05d9f70e00bb44c100bb3ee700bb390d01767d34"),
        (25_000_000, 7_000_000): bytes.fromhex("028f5c29051eb81400a3dc2900a3d70a00a3d1ec0147ae05"),
        (25_000_000, 6_000_000): bytes.fromhex("0231bcb50463791b008c7391008c6f2d008c6aca0118de17"),
    }
    DEMOD_INIT = (
        (0xD73A, 0xFF, 0xA1), (0xD73B, 0xFF, 0x1F), (0xD73C, 0xF0, 0xA0),
        (0xD732, 0x08, 0), (0xD731, 0x30, 0x30), (0xD73D, 0x80, 0x80),
        (0xD740, 0x01, 0), (0xD740, 0x02, 0), (0xD740, 0x04, 0), (0xD740, 0x08, 0x08),
        (0xD3C1, 0x10, 0x10), (0x9124, 0xFF, 0x58), (0x9125, 0x03, 0x02),
        (0xD3A2, 0xFF, 0), (0xD3A3, 0xFF, 4), (0xD305, 0xFF, 0x32),
        (0xD306, 0xFF, 0x10), (0xD304, 0xFF, 4), (0x9112, 1, 1), (0x911D, 1, 1),
        (0x911A, 1, 1), (0x911B, 1, 1), (0x9BCE, 0x0F, 2), (0x9116, 1, 1),
        (0x9122, 0xFF, 0xD0), (0xD2E0, 0xFF, 0xD0), (0xD2E9, 0x0F, 0x0D),
        (0xD38C, 0xFF, 0xFC), (0xD38D, 0xFF, 0), (0xD38E, 0xFF, 0x7E),
        (0xD38F, 0xFF, 0), (0xD390, 0xFF, 0x2F), (0xD145, 0x10, 0x10),
        (0xD1A9, 0x10, 0x10), (0xD158, 0xE0, 0x20), (0xD159, 0x3F, 6),
        (0xD167, 0xFF, 0), (0xD168, 0x0F, 7), (0xD1C3, 0xE0, 0), (0xD1C4, 0x3F, 0),
        (0xD1C5, 0x7F, 0x10), (0xD1C6, 7, 2), (0xD080, 0x7C, 0x0C),
        (0xD081, 0xF0, 0x90), (0xD098, 0xF0, 0xF0), (0xD098, 0x0F, 3),
        (0xDBC0, 0x10, 0x10), (0xDBC7, 0xFF, 8), (0xDBC8, 0xF0, 0), (0xDBC9, 0x1F, 1),
        (0xD280, 0xFF, 0xE0), (0xD281, 0xFF, 0xFF), (0xD282, 0xFF, 0xFF),
        (0xD283, 0xFF, 0xC3), (0xD284, 0xFF, 0xFF), (0xD285, 0x0F, 1),
        (0xD0F0, 0x7F, 0x1A), (0xD0F1, 0x10, 0x10), (0xD0F2, 0xFF, 0x0C),
        (0xD101, 0xE0, 0xC0), (0xD103, 0x0F, 8), (0xD0F8, 0x7F, 0x20),
        (0xD111, 0x20, 0), (0xD111, 0x40, 0), (0x910B, 0xFF, 0x0A),
        (0x9115, 0xFF, 2), (0x910C, 0xFF, 2), (0x910D, 0xFF, 8), (0x910E, 0xFF, 0x0A),
        (0x9BF6, 0xFF, 6), (0x9BF8, 0xFF, 2), (0x9BF7, 0xFF, 5),
        (0x9BF9, 0xFF, 0x0F), (0x9BFC, 0xFF, 0x13), (0x9BD3, 0xFF, 0xFF),
        (0x9BBE, 1, 1), (0x9BCC, 1, 1),
    )
    TDA18271_INIT = (
        (0x9BD5, 0xFF, 1), (0x9BD6, 0xFF, 4), (0xD1A0, 2, 2),
        (0xD000, 1, 1), (0xD000, 2, 0), (0xD001, 2, 2), (0xD001, 1, 0),
        (0xD001, 0x20, 0), (0xD002, 0x1F, 0x19), (0xD003, 0x1F, 0x1A),
        (0xD004, 0x1F, 0x19), (0xD005, 0x1F, 0x1A), (0xD00E, 0x1F, 0x10),
        (0xD00F, 7, 4), (0xD00F, 0x38, 0x28), (0xD010, 7, 4), (0xD010, 0x38, 0x28),
        (0xD016, 0xF0, 0x30), (0xD01F, 0x3F, 0x0A), (0xD020, 0x3F, 0x0A),
        (0x9BDA, 0xFF, 1), (0x9BE3, 0xFF, 1), (0xD1A0, 2, 0),
        (0x9BBE, 1, 1), (0x9BCC, 1, 1), (0x9BB9, 0xFF, 0), (0x9BCD, 0xFF, 0x18),
        (0x9BFF, 0xFF, 0x2C), (0xD015, 0xFF, 0x46), (0xD016, 1, 0),
        (0xD044, 0xFF, 0x46), (0xD045, 1, 0), (0xD008, 0xFF, 0xDF),
        (0xD009, 3, 2), (0xD006, 0xFF, 0x44), (0xD007, 3, 1),
        (0xD00C, 0xFF, 0), (0xD00D, 3, 2), (0xD00A, 0xFF, 0xF6), (0xD00B, 3, 1),
        (0x9BBA, 0xFF, 0xF9), (0x9BC8, 0xFF, 0xAA), (0x9BC3, 0xFF, 0xDF),
        (0x9BC4, 0xFF, 2), (0x9BC5, 0xFF, 0), (0x9BC6, 0xFF, 2), (0x9BC9, 0xFF, 0xF0),
        (0xD011, 0xFF, 0x3C), (0xD012, 3, 1), (0xD013, 0xFF, 0xF7), (0xD014, 3, 2),
        (0xD040, 0xFF, 0x0B), (0xD041, 3, 2), (0xD042, 0xFF, 0x4D), (0xD043, 3, 0),
        (0xD045, 2, 0), (0x9BCF, 1, 1), (0xD045, 4, 4), (0xD04F, 0xFF, 0x9A),
        (0xD050, 1, 1), (0xD051, 0xFF, 0x5A), (0xD052, 1, 1), (0xD053, 0xFF, 0x50),
        (0xD054, 0xFF, 0x46), (0x9BD7, 0xFF, 0x0A), (0x9BD8, 0xFF, 0x14),
        (0x9BD9, 0xFF, 8), (0x9BD0, 0xFF, 0xA8), (0x9BE4, 0xFF, 0x7F),
        (0x9BBD, 0xFF, 0xA8), (0x9BE2, 0xFF, 0x20), (0x9BEE, 1, 1),
    )

    def __init__(
        self,
        bridge: AF9015Bridge,
        demodulator_address: int,
        tuner: DVBTuner | None,
        clock_hz: int,
        intermediate_frequency_hz: int,
        spectrum_inverted: bool,
        tuner_id: int = 0x9C,
        adapter_index: int = 0,
        mt2060_if: int = 0,
    ) -> None:
        if adapter_index not in (0, 1):
            raise ValueError("AF9013 adapter_index must be 0 or 1")
        self.bridge = bridge
        self.demodulator_address = demodulator_address
        self.tuner = tuner
        self.clock_hz = clock_hz
        self.intermediate_frequency_hz = intermediate_frequency_hz
        self.spectrum_inverted = spectrum_inverted
        self.tuner_id = tuner_id
        self.adapter_index = adapter_index
        self.mt2060_if = mt2060_if
        self.ts_mode = self.TS_MODE_SERIAL if adapter_index == 1 else self.TS_MODE_USB
        self.ts_output_pin = self.TS_OUTPUT_PIN_7 if adapter_index == 1 else self.TS_OUTPUT_PIN_DEFAULT
        self.gpio = (
            (self.GPIO_HI, 0, 0, self.GPIO_TUNER_ON)
            if adapter_index == 0
            else (self.GPIO_TUNER_ON, self.GPIO_LO, 0, 0)
        )
        self._initialized = False
        self._sleeping = True
        self.current_tuning: DVBTuning | None = None
        self._last_bandwidth_hz: int | None = None
        self._block_errors = 0
        self._bit_count = 0
        self._block_count = 0

    def get_config_dict(self) -> dict[str, object]:
        """Return frontend configuration and current tuning state for diagnostics."""
        return {
            "demodulator_address": self.demodulator_address,
            "tuner_id": self.tuner_id,
            "adapter_index": self.adapter_index,
            "clock_hz": self.clock_hz,
            "intermediate_frequency_hz": self.intermediate_frequency_hz,
            "spectrum_inverted": self.spectrum_inverted,
            "mt2060_if": self.mt2060_if,
            "ts_mode": self.ts_mode,
            "ts_output_pin": self.ts_output_pin,
            "gpio": self.gpio,
            "initialized": self._initialized,
            "sleeping": self._sleeping,
            "current_tuning": self.current_tuning,
            "last_bandwidth_hz": self._last_bandwidth_hz,
        }

    def _read_register(self, register: int, length: int = 1) -> bytes:
        command = ((length - 1) << 2) | 0x02
        if self.adapter_index == 1:
            return b"".join(
                self.bridge.read_i2c(
                    self.demodulator_address,
                    register + offset,
                    1,
                    address_length=3,
                    mailbox=0x02,
                )
                for offset in range(length)
            )
        if register & 0xFF00 == 0xAE00:
            return b"".join(
                self.bridge.read_memory(
                    register + offset,
                    1,
                    address_length=3,
                    mailbox=0x02,
                    i2c_address=self.demodulator_address,
                )
                for offset in range(length)
            )
        return self.bridge.read_memory(
            register,
            length,
            address_length=3,
            mailbox=command,
            i2c_address=self.demodulator_address,
        )

    def _write_register(self, register: int, data: bytes) -> None:
        command = ((len(data) - 1) << 2) | 0x03
        if self.adapter_index == 1:
            for offset, value in enumerate(data):
                self.bridge.write_i2c(
                    self.demodulator_address,
                    register + offset,
                    bytes((value,)),
                    address_length=3,
                    mailbox=0x03,
                )
            return
        if register & 0xFF00 == 0xAE00:
            for offset, value in enumerate(data):
                self.bridge.write_memory(
                    register + offset,
                    bytes((value,)),
                    address_length=3,
                    mailbox=0x03,
                    i2c_address=self.demodulator_address,
                )
            return
        self.bridge.write_memory(
            register,
            data,
            address_length=3,
            mailbox=command,
            i2c_address=self.demodulator_address,
        )

    def _set_gate(self, enabled: bool) -> None:
        current = self._read_register(self.GATE_REGISTER)[0]
        value = (current & ~self.GATE_MASK) | (self.GATE_MASK if enabled else 0)
        self._write_register(self.GATE_REGISTER, bytes((value,)))

    def gated_read_register(self, address: int, register: int, length: int) -> bytes:
        """Read from the tuner behind this demodulator's I2C gate."""
        self._set_gate(True)
        try:
            return self.bridge.read_i2c(address, register, length)
        finally:
            self._set_gate(False)

    def gated_write_register(self, address: int, register: int, data: bytes) -> None:
        """Write to the tuner behind this demodulator's I2C gate."""
        self._set_gate(True)
        try:
            self.bridge.write_i2c(address, register, data)
        finally:
            self._set_gate(False)

    def _update_bits(self, register: int, mask: int, value: int) -> None:
        current = self._read_register(register)[0]
        self._write_register(register, bytes(((current & ~mask) | (value & mask),)))

    def _apply_table(self, table: tuple[tuple[int, int, int], ...]) -> None:
        for register, mask, value in table:
            self._update_bits(register, mask, value)

    def _set_gpio(self, gpio: int, value: int) -> None:
        if gpio in (0, 1):
            register = self.REG_GPIO_01
        elif gpio in (2, 3):
            register = self.REG_GPIO_23
        else:
            raise ValueError("AF9013 GPIO index must be between 0 and 3")
        position = 0 if gpio in (0, 2) else 4
        self._update_bits(register, 0x0F << position, value << position)

    def init_hardware(self) -> None:
        clock_code = self.CLOCK_CODES.get(self.clock_hz)
        if clock_code is None:
            raise ValueError("AF9013 clock must be 20.48, 25, 28, or 28.8 MHz")
        self._update_bits(self.REG_ADC_CONTROL, 0x08, 0)
        self._update_bits(self.REG_RESET, 0x02, 0)
        self._update_bits(self.REG_RESET, 0x10, 0)
        self._write_register(self.REG_API_VERSION, self.API_VERSION)
        self._update_bits(self.REG_ADC_CLOCK, 0x0F, clock_code)
        sampling_clock = (self.clock_hz * 0x80000) // 1_000_000
        self._write_register(self.REG_SAMPLING_CLOCK, sampling_clock.to_bytes(3, "little"))
        for gpio, value in enumerate(self.gpio):
            self._set_gpio(gpio, value)
        self._apply_table(self.DEMOD_INIT)
        if self.tuner_id == 0x9C:
            self._apply_table(self.TDA18271_INIT)
        ts_control = self.ts_mode << 1
        if self.ts_output_pin == self.TS_OUTPUT_PIN_7:
            ts_control |= 0x08
        self._update_bits(self.REG_TS_CONTROL, 0x0E, ts_control)
        self._update_bits(self.REG_LOCK_LED, 0x01, 0x01)
        self.tuner.init_hardware() if self.tuner is not None else None
        self._initialized = True
        self._sleeping = False

    def _transmission_parameters(self, tuning: DVBTuning) -> tuple[bytes, bool]:
        """Pack tuning parameters into AF9013 transmission bytes and report auto mode.

        The returned bytes contain the transmission mode, guard interval,
        hierarchy, modulation, and HP/LP code rates in the bit fields expected
        by the AF9013. For example, a fully specified 8k, 1/32, QAM64 tuning
        produces ``(b"\\x01\\x90\\x12", False)``; if any parameter is
        an ``AUTO`` enum member, the flag is ``True`` instead.

        For example::

            frontend._transmission_parameters(
                  DVBTuning(650_000_000, transmission_mode=TransmissionMode.K8,
                      guard_interval=GuardInterval.GI_1_32, hierarchy=Hierarchy.NONE,
                      modulation=Modulation.QAM64, code_rate_hp=CodeRate.RATE_3_4,
                      code_rate_lp=CodeRate.RATE_3_4)
            )
            # (b"\\x01\\x90\\x12", False)
        """
        transmission_mode = {
            TransmissionMode.AUTO:  0,
            TransmissionMode.K2:    0,
            TransmissionMode.K8:    1
        }[tuning.transmission_mode]
        guard_interval = {
            GuardInterval.AUTO:     0,
            GuardInterval.GI_1_32:  0,
            GuardInterval.GI_1_16:  1,
            GuardInterval.GI_1_8:   2,
            GuardInterval.GI_1_4:   3,
        }[tuning.guard_interval]
        hierarchy = {
            Hierarchy.AUTO:         0,
            Hierarchy.NONE:         0,
            Hierarchy.LEVEL_1:      1,
            Hierarchy.LEVEL_2:      2,
            Hierarchy.LEVEL_4:      3,
        }[tuning.hierarchy]
        modulation = {
            Modulation.AUTO:        0,
            Modulation.QPSK:        0,
            Modulation.QAM16:       1,
            Modulation.QAM64:       2,
        }[tuning.modulation]
        code_rate_hp = {
            CodeRate.AUTO:          0,
            CodeRate.RATE_1_2:      0,
            CodeRate.RATE_2_3:      1,
            CodeRate.RATE_3_4:      2,
            CodeRate.RATE_5_6:      3,
            CodeRate.RATE_7_8:      4,
        }[tuning.code_rate_hp]
        code_rate_lp = {
            CodeRate.AUTO:          0,
            CodeRate.RATE_1_2:      0,
            CodeRate.RATE_2_3:      1,
            CodeRate.RATE_3_4:      2,
            CodeRate.RATE_5_6:      3,
            CodeRate.RATE_7_8:      4,
            CodeRate.NONE:          0,
        }[tuning.code_rate_lp]
        auto_mode = (
            tuning.transmission_mode is TransmissionMode.AUTO
            or tuning.guard_interval is GuardInterval.AUTO
            or tuning.hierarchy is Hierarchy.AUTO
            or tuning.modulation is Modulation.AUTO
            or tuning.code_rate_hp is CodeRate.AUTO
            or tuning.code_rate_lp is CodeRate.AUTO
        )
        return bytes(
            (
                transmission_mode | guard_interval << 2 | hierarchy << 4,
                modulation << 6 | 0x10,
                code_rate_hp | code_rate_lp << 3,
            )
        ), auto_mode

    def tune(self, tuning: DVBTuning, wait_for_lock: bool = True) -> DVBFrontendStatus:
        if self.tuner is None:
            raise ValueError("AF9013 frontend has no tuner attached")
        if not self._initialized:
            self.init_hardware()
        if self.clock_hz <= 0 or self.intermediate_frequency_hz <= 0:
            raise ValueError("AF9013 clock and IF must be configured")
        if not 174_000_000 <= tuning.frequency_hz <= 862_000_000:
            raise ValueError("AF9013 frequency must be between 174 and 862 MHz")
        if tuning.bandwidth_hz not in (6_000_000, 7_000_000, 8_000_000):
            raise ValueError("AF9013 DVB-T bandwidth must be 6, 7, or 8 MHz")

        # Program the tuner with the desired tuning parameters
        self.tuner.tune(tuning)

        # Configure the AF9013 demodulator with the appropriate CFOE coefficients, based on the
        # demodulator clock frequency and the signal bandwidth.
        coefficients = self.COEFFICIENTS.get((self.clock_hz, tuning.bandwidth_hz))
        if coefficients is None:
            raise ValueError("unsupported AF9013 clock and bandwidth combination")
        if tuning.bandwidth_hz != self._last_bandwidth_hz:
            self._write_register(self.REG_CFOE, coefficients)

        # Configure the AF9013 demodulator with the appropriate frequency control settings, based on the
        # intermediate frequency and the demodulator clock frequency.
        get_if_frequency = getattr(self.tuner, "get_if_frequency_hz", None)
        sampling_frequency = (
            get_if_frequency() if get_if_frequency is not None else self.intermediate_frequency_hz
        )
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
        self._write_register(self.REG_FREQUENCY_CONTROL, frequency_data)
        self._write_register(self.REG_FREQUENCY_CONTROL_EXT, frequency_data + complement_data)

        # Clear status flags
        self._update_bits(self.REG_TPS_STATUS, 0x08, 0x08)  # Clear TPS lock flag
        self._update_bits(self.REG_MPEG_STATUS, 0x40, 0)    # Clear MPEG2 lock flag
        self._update_bits(self.REG_EMPTY_CHANNEL, 0x01, 0)  # Clear empty channel flag
        self._update_bits(self.REG_EMPTY_DVBT_CHANNEL, 0x01, 0)  # Clear empty DVB-T channel flag

        # Configure transmission parameters and easy mode settings based on the current tuning
        transmission, auto_mode = self._transmission_parameters(tuning)
        transmission = bytearray(transmission)
        transmission[1] |= {6_000_000: 0, 7_000_000: 1 << 2, 8_000_000: 2 << 2}[tuning.bandwidth_hz]
        self._write_register(self.REG_TRANSMISSION, bytes(transmission))
        self._write_register(self.REG_EASY_MODE, bytes((0 if auto_mode else 1,)))
        if not auto_mode:
            self._write_register(self.REG_EASY_MODE_VALUE, b"\x00")
        self._write_register(0xFFFF, b"\x00")
        self._last_bandwidth_hz = tuning.bandwidth_hz
        self.current_tuning = tuning
        self._sleeping = False
        return self.wait_for_lock() if wait_for_lock else self.read_status()

    def read_status(self) -> DVBFrontendStatus:
        mpeg_locked = bool(self._read_register(self.REG_MPEG_STATUS)[0] & 0x40)
        tps_locked = bool(self._read_register(self.REG_TPS_STATUS)[0] & 0x08)
        if mpeg_locked:
            signal = carrier = viterbi = sync = lock = True
        else:
            signal = carrier = viterbi = tps_locked
            sync = lock = False

        strength = snr = bit_errors = block_errors = None
        if viterbi and self._read_register(0x9BEE)[0] & 1:
            rf = self._read_register(0xD07C, 2)
            limits = self._read_register(0x9BBD)[0] + self._read_register(0x9BD0)[0], self._read_register(0x9BE2)[0] + self._read_register(0x9BE4)[0]
            if limits[0] != limits[1]:
                strength = int(30000 * (sum(rf) - limits[1]) / (limits[0] - limits[1]) - 80000)
        if viterbi and self._read_register(self.REG_CNR_READY)[0] & 0x08:
            value = int.from_bytes(self._read_register(self.REG_CNR, 3), "little")
            modulation = (self._read_register(self.REG_MODULATION)[0] >> 6) & 3
            bounds = {
                0: (653799, 1689999, 0, 1690000, 13, 3355443),
                1: (371105, 827999, 370000, 828000, 6, 43900382),
                2: (193246, 424999, 193000, 425000, 8, 49912218),
            }.get(modulation)
            if bounds:
                low, high, lower_offset, top, factor, offset = bounds
                value = max(low, min(high, value))
                snr = (log10(value - lower_offset) - log10(top - value) + offset / 2**24) * factor
        if sync and self._read_register(self.REG_BER_READY)[0] & 0x10:
            counter = self._read_register(self.REG_BER_COUNTER, 7)
            packet_count = int.from_bytes(counter[0:2], "little")
            bit_errors = int.from_bytes(counter[2:5], "little")
            block_errors = int.from_bytes(counter[5:7], "little")
            bit_count = packet_count * 204 * 8
            self._bit_count += bit_count
            self._block_count += packet_count
            self._block_errors += block_errors
            if packet_count != 10_000:
                self._write_register(self.REG_BER_COUNTER, (10_000).to_bytes(2, "little"))
            self._update_bits(self.REG_BER_READY, 0x20, 0x20)
            block_errors = self._block_errors
            bit_count = self._bit_count
            block_count = self._block_count
        else:
            bit_count = block_count = None
        return DVBFrontendStatus(
            signal,
            carrier,
            viterbi,
            sync,
            lock,
            strength,
            snr,
            bit_errors,
            block_errors,
            bit_count,
            block_count,
        )

    def read_tuning(self) -> DVBTuning | None:
        if self.current_tuning is None:
            return None
        transmission = self._read_register(self.REG_TRANSMISSION, 3)
        modulation = {
            0: Modulation.QPSK,
            1: Modulation.QAM16,
            2: Modulation.QAM64,
        }.get((transmission[1] >> 6) & 3, Modulation.AUTO)
        mode = {0: TransmissionMode.K2, 1: TransmissionMode.K8}.get(
            transmission[0] & 3, TransmissionMode.AUTO
        )
        guard = {
            0: GuardInterval.GI_1_32,
            1: GuardInterval.GI_1_16,
            2: GuardInterval.GI_1_8,
            3: GuardInterval.GI_1_4,
        }[(transmission[0] >> 2) & 3]
        hierarchy = {
            0: Hierarchy.NONE,
            1: Hierarchy.LEVEL_1,
            2: Hierarchy.LEVEL_2,
            3: Hierarchy.LEVEL_4,
        }.get((transmission[0] >> 4) & 7, Hierarchy.AUTO)
        hp = {
            0: CodeRate.RATE_1_2,
            1: CodeRate.RATE_2_3,
            2: CodeRate.RATE_3_4,
            3: CodeRate.RATE_5_6,
            4: CodeRate.RATE_7_8,
        }.get(transmission[2] & 7, CodeRate.AUTO)
        lp = {
            0: CodeRate.RATE_1_2,
            1: CodeRate.RATE_2_3,
            2: CodeRate.RATE_3_4,
            3: CodeRate.RATE_5_6,
            4: CodeRate.RATE_7_8,
        }.get((transmission[2] >> 3) & 7, CodeRate.AUTO)
        return DVBTuning(self.current_tuning.frequency_hz, {0: 6_000_000, 1: 7_000_000, 2: 8_000_000}[(transmission[1] >> 2) & 3], mode, guard, hierarchy, modulation, hp, lp)

    def wait_for_lock(
        self,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        poll_interval: float = 0.05,
    ) -> DVBFrontendStatus:
        """Poll AF9013 status until it locks or the timeout expires."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = monotonic() + timeout if timeout is not None else None
        while deadline is None or monotonic() < deadline:
            status = self.read_status()
            if status.lock:
                return status
            sleep(poll_interval)
        return self.read_status()

    def sleep(self) -> None:
        if not self._initialized or self._sleeping:
            return
        self._update_bits(self.REG_LOCK_LED, 0x01, 0)
        self._update_bits(self.REG_RESET, 0x10, 0x10)
        self._write_register(self.REG_RESET_EXECUTION, b"\x01")
        deadline = monotonic() + 1.0
        while monotonic() < deadline and not self._read_register(self.REG_RESET)[0] & 0x02:
            sleep(0.005)
        if not self._read_register(self.REG_RESET)[0] & 0x02:
            raise TimeoutError("AF9013 reset timed out")
        self._update_bits(self.REG_ADC_CONTROL, 0x08, 0x08)
        if self.tuner is not None:
            self.tuner.sleep()
        self._sleeping = True