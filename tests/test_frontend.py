from math import log10

import pytest

from mactv.frontend import AF9013Frontend, FrontendStatus
from mactv.tuner import Tuning


class FakeBus:
    def __init__(self):
        self.registers = {0xD330: b"\x08", 0xD507: b"\x40", 0xD3C0: b"\x00\x00\x00"}
        self.writes = []

    def read_i2c(self, address, register, length, address_length=1, mailbox=0):
        return self.registers.get(register, bytes(length))[:length]

    def write_i2c(self, address, register, data, address_length=1, mailbox=0):
        self.writes.append((address, register, bytes(data), address_length, mailbox))
        self.registers[register] = bytes(data)


class FakeTuner:
    def __init__(self):
        self.tunings = []
        self.sleep_calls = 0

    def init(self):
        pass

    def tune(self, tuning):
        self.tunings.append(tuning)

    def sleep(self):
        self.sleep_calls += 1


def test_frontend_tunes_tuner_and_programs_frequency_and_bandwidth():
    bus = FakeBus()
    tuner = FakeTuner()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    tuning = Tuning(650_000_000, 8_000_000)

    frontend.tune(tuning)

    assert tuner.tunings == [tuning]
    assert any(register == frontend.REG_FREQUENCY_CONTROL for _, register, _, _, _ in bus.writes)
    assert any(register == frontend.REG_FREQUENCY_CONTROL_EXT for _, register, data, _, _ in bus.writes if len(data) == 6)
    bus.registers[frontend.REG_MPEG_STATUS] = b"\x40"
    assert frontend.read_status() == FrontendStatus(True, True, True, True, True)


def test_frontend_programs_serial_ts_mode_on_output_pin_7():
    bus = FakeBus()
    frontend = AF9013Frontend(
        bus,
        0x1C,
        None,
        28_800_000,
        4_570_000,
        False,
        adapter=1,
    )

    frontend.init()

    assert bus.registers[frontend.REG_TS_CONTROL] == b"\x0c"


def test_frontend_programs_adapter_zero_gpio_nibbles():
    bus = FakeBus()
    frontend = AF9013Frontend(
        bus,
        0x1C,
        None,
        28_800_000,
        4_570_000,
        False,
    )

    frontend.init()

    assert bus.registers[frontend.REG_GPIO_01] == b"\x07"
    assert bus.registers[frontend.REG_GPIO_23] == b"\x30"


def test_frontend_programs_adapter_one_gpio_nibbles():
    bus = FakeBus()
    frontend = AF9013Frontend(
        bus,
        0x1D,
        None,
        28_800_000,
        4_570_000,
        False,
        adapter=1,
    )

    frontend.init()

    assert bus.registers[frontend.REG_GPIO_01] == b"\x33"
    assert bus.registers[frontend.REG_GPIO_23] == b"\x00"


def test_frontend_rejects_unknown_adapter():
    with pytest.raises(ValueError, match="adapter must be 0 or 1"):
        AF9013Frontend(FakeBus(), 0x1C, None, 28_800_000, 4_570_000, False, adapter=2)


def test_frontend_sleep_stops_tuner():
    tuner = FakeTuner()
    frontend = AF9013Frontend(FakeBus(), 0x1C, tuner, 28_800_000, 4_570_000, False)

    frontend.init()
    frontend.bridge.registers[frontend.REG_RESET] = b"\x02"
    frontend.sleep()

    assert tuner.sleep_calls == 1


def test_frontend_opens_and_closes_af9013_gate_for_tuner_access():
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)
    bus.registers[frontend.GATE_REGISTER] = b"\x00"

    frontend.gated_write_register(0x60, 0, b"\x84")

    assert bus.writes == [
        (0x1C, frontend.GATE_REGISTER, b"\x08", 3, 1),
        (0x60, 0, b"\x84", 1, 0),
        (0x1C, frontend.GATE_REGISTER, b"\x00", 3, 1),
    ]


def test_frontend_cnr_uses_kernel_fixed_point_scale():
    bus = FakeBus()
    bus.registers[AF9013Frontend.REG_CNR_READY] = b"\x08"
    bus.registers[AF9013Frontend.REG_CNR] = (1_000_000).to_bytes(3, "little")
    bus.registers[AF9013Frontend.REG_MODULATION] = b"\x00"
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)

    metrics = frontend.read_metrics()

    expected = (log10(1_000_000) - log10(1_690_000 - 1_000_000) + 3_355_443 / 2**24) * 13
    assert metrics.snr_db == pytest.approx(expected)


def test_frontend_ber_reads_counts_and_resets_measurement_window():
    bus = FakeBus()
    bus.registers[AF9013Frontend.REG_BER_READY] = b"\x10"
    bus.registers[AF9013Frontend.REG_BER_COUNTER] = bytes((100, 0, 3, 2, 1, 6, 5))
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)

    metrics = frontend.read_metrics()

    assert metrics.bit_errors == 0x010203
    assert metrics.block_errors == 0x0506
    assert metrics.bit_count == 100 * 204 * 8
    assert metrics.block_count == 100
    assert (frontend.REG_BER_COUNTER, b"\x10\x27") in [
        (register, data) for _, register, data, _, _ in bus.writes
    ]
    assert (frontend.REG_BER_READY, b"\x30") in [
        (register, data) for _, register, data, _, _ in bus.writes
    ]


def test_frontend_sleep_raises_when_reset_does_not_complete():
    tuner = FakeTuner()
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    frontend.init()

    with pytest.raises(TimeoutError):
        frontend.sleep()

    assert tuner.sleep_calls == 0