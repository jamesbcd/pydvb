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
    assert frontend.read_status() == FrontendStatus(True, True, True, True, True)


def test_frontend_sleep_stops_tuner():
    tuner = FakeTuner()
    frontend = AF9013Frontend(FakeBus(), 0x1C, tuner, 28_800_000, 4_570_000, False)

    frontend.init()
    frontend.sleep()

    assert tuner.sleep_calls == 1


def test_frontend_opens_and_closes_af9013_gate_for_tuner_access():
    bus = FakeBus()
    frontend = AF9013Frontend(bus, 0x1C, None, 28_800_000, 4_570_000, False)
    bus.registers[frontend.GATE_REGISTER] = b"\x00"

    frontend.gated_write(0x60, 0, b"\x84")

    assert bus.writes == [
        (0x1C, frontend.GATE_REGISTER, b"\x08", 3, 1),
        (0x60, 0, b"\x84", 1, 0),
        (0x1C, frontend.GATE_REGISTER, b"\x00", 3, 1),
    ]