from math import log10

import pytest

from mactv.drivers.af9015.frontend import AF9013Frontend, DVBFrontendStatus
from mactv.driver_api.tuner import DVBTuning


class FakeBus:
    def __init__(self):
        self.registers = {0xD330: b"\x08", 0xD507: b"\x40", 0xD3C0: b"\x00\x00\x00"}
        self.writes = []

    def read_i2c(self, address, register, length, address_length=1, mailbox=0):
        return self.registers.get(register, bytes(length))[:length]

    def write_i2c(self, address, register, data, address_length=1, mailbox=0):
        self.writes.append((address, register, bytes(data), address_length, mailbox))
        self.registers[register] = bytes(data)

    def read_memory(self, register, length, address_length=0, mailbox=0, i2c_address=0):
        return self.read_i2c(i2c_address or 0x1C, register, length, address_length, mailbox)

    def write_memory(self, register, data, address_length=0, mailbox=0, i2c_address=0):
        self.write_i2c(i2c_address or 0x1C, register, data, address_length, mailbox)


class FakeTuner:
    def __init__(self):
        self.tunings = []
        self.sleep_calls = 0

    def init_hardware(self):
        pass

    def tune(self, tuning):
        self.tunings.append(tuning)

    def get_if_frequency_hz(self):
        return 4_570_000

    def sleep(self):
        self.sleep_calls += 1


def test_secondary_register_writes_use_single_byte_i2c_transactions():
    bus = FakeBus()
    frontend = AF9013Frontend(
        bus,
        0x1D,
        None,
        28_800_000,
        4_570_000,
        False,
        adapter_index=1,
    )

    frontend._write_register(0xD3C0, b"\x01\x02")

    assert bus.writes == [
        (0x1D, 0xD3C0, b"\x01", 3, 3),
        (0x1D, 0xD3C1, b"\x02", 3, 3),
    ]


def test_frontend_tunes_tuner_and_programs_frequency_and_bandwidth():
    bus = FakeBus()
    tuner = FakeTuner()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    tuning = DVBTuning(650_000_000, 8_000_000)

    status = frontend.tune(tuning, wait_for_lock=False)

    assert tuner.tunings == [tuning]
    assert any(register == frontend.REG_FREQUENCY_CONTROL for _, register, _, _, _ in bus.writes)
    assert any(register == frontend.REG_FREQUENCY_CONTROL_EXT for _, register, data, _, _ in bus.writes if len(data) == 6)
    bus.registers[frontend.REG_MPEG_STATUS] = b"\x40"
    status = frontend.read_status()
    assert (status.signal, status.carrier, status.viterbi, status.sync, status.lock) == (True, True, True, True, True)


def test_frontend_tune_waits_for_lock_by_default(monkeypatch):
    bus = FakeBus()
    tuner = FakeTuner()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    expected_status = DVBFrontendStatus(True, True, True, True, True)
    monkeypatch.setattr(frontend, "wait_for_lock", lambda: expected_status)

    assert frontend.tune(DVBTuning(650_000_000, 8_000_000)) is expected_status


def test_frontend_uses_tuner_selected_if_frequency():
    bus = FakeBus()
    tuner = FakeTuner()
    tuner.get_if_frequency_hz = lambda: 3_500_000
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_300_000, False)

    frontend.tune(DVBTuning(177_500_000, 7_000_000), wait_for_lock=False)

    frequency_data = next(data for _, register, data, _, _ in bus.writes if register == frontend.REG_FREQUENCY_CONTROL)
    expected_word = 0x800000 - round(3_500_000 * 0x800000 / 28_800_000)
    assert frequency_data == expected_word.to_bytes(3, "little")


def test_frontend_programs_serial_ts_mode_on_output_pin_7():
    bus = FakeBus()
    frontend = AF9013Frontend(
        bus,
        0x1C,
        None,
        28_800_000,
        4_570_000,
        False,
        adapter_index=1,
    )

    frontend.init_hardware()

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

    frontend.init_hardware()

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
        adapter_index=1,
    )

    frontend.init_hardware()

    assert bus.registers[frontend.REG_GPIO_01] == b"\x33"
    assert bus.registers[frontend.REG_GPIO_23] == b"\x00"


def test_frontend_rejects_unknown_adapter():
    with pytest.raises(ValueError, match="adapter_index must be 0 or 1"):
        AF9013Frontend(FakeBus(), 0x1C, None, 28_800_000, 4_570_000, False, adapter_index=2)


def test_frontend_get_config_dict_contains_configuration_and_is_a_snapshot():
    frontend = AF9013Frontend(FakeBus(), 0x1C, None, 28_800_000, 4_570_000, False)

    config = frontend.get_config_dict()

    assert config == {
        "demodulator_address": 0x1C,
        "tuner_id": 0x9C,
        "adapter_index": 0,
        "clock_hz": 28_800_000,
        "intermediate_frequency_hz": 4_570_000,
        "spectrum_inverted": False,
        "mt2060_if": 0,
        "ts_mode": frontend.TS_MODE_USB,
        "ts_output_pin": frontend.TS_OUTPUT_PIN_DEFAULT,
        "gpio": frontend.gpio,
        "initialized": False,
        "sleeping": True,
        "current_tuning": None,
        "last_bandwidth_hz": None,
    }

    config["clock_hz"] = 1
    assert frontend.get_config_dict()["clock_hz"] == 28_800_000


def test_frontend_sleep_stops_tuner():
    tuner = FakeTuner()
    frontend = AF9013Frontend(FakeBus(), 0x1C, tuner, 28_800_000, 4_570_000, False)

    frontend.init_hardware()
    frontend.bridge.registers[frontend.REG_RESET] = b"\x02"
    frontend.sleep()

    assert tuner.sleep_calls == 1


def test_frontend_reinitializes_hardware_when_tuning_after_sleep():
    tuner = FakeTuner()
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    tuning = DVBTuning(177_500_000, 7_000_000)

    frontend.init_hardware()
    bus.registers[frontend.REG_RESET] = b"\x02"
    frontend.sleep()
    bus.writes.clear()

    frontend.tune(tuning, wait_for_lock=False)

    assert any(register == frontend.REG_API_VERSION for _, register, _, _, _ in bus.writes)
    assert tuner.tunings == [tuning]


def test_frontend_opens_and_closes_af9013_gate_for_tuner_access():
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)
    bus.registers[frontend.GATE_REGISTER] = b"\x00"

    frontend.gated_write_register(0x60, 0, b"\x84")

    assert bus.writes == [
        (0x1C, frontend.GATE_REGISTER, b"\x08", 3, 3),
        (0x60, 0, b"\x84", 1, 0),
        (0x1C, frontend.GATE_REGISTER, b"\x00", 3, 3),
    ]


def test_frontend_cnr_uses_kernel_fixed_point_scale():
    bus = FakeBus()
    bus.registers[AF9013Frontend.REG_CNR_READY] = b"\x08"
    bus.registers[AF9013Frontend.REG_CNR] = (1_000_000).to_bytes(3, "little")
    bus.registers[AF9013Frontend.REG_MODULATION] = b"\x00"
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)

    metrics = frontend.read_status()

    expected = (log10(1_000_000) - log10(1_690_000 - 1_000_000) + 3_355_443 / 2**24) * 13
    assert metrics.snr_db == pytest.approx(expected)


def test_frontend_ber_reads_counts_and_resets_measurement_window():
    bus = FakeBus()
    bus.registers[AF9013Frontend.REG_BER_READY] = b"\x10"
    bus.registers[AF9013Frontend.REG_BER_COUNTER] = bytes((100, 0, 3, 2, 1, 6, 5))
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)

    metrics = frontend.read_status()

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


def test_frontend_wait_for_lock_can_use_a_finite_timeout():
    bus = FakeBus()
    bus.registers[AF9013Frontend.REG_MPEG_STATUS] = b"\x00"
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)

    status = frontend.wait_for_lock(timeout=0, poll_interval=0.01)

    assert not status.lock


def test_frontend_sleep_raises_when_reset_does_not_complete():
    tuner = FakeTuner()
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, tuner, 28_800_000, 4_570_000, False)
    frontend.init_hardware()

    with pytest.raises(TimeoutError):
        frontend.sleep()

    assert tuner.sleep_calls == 0