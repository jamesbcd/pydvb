from mactv.frontend import Af9013TunerBus


class FakeBridge:
    def __init__(self):
        self.calls = []

    def read_i2c(self, address, register, length, address_length=1, mailbox=0):
        self.calls.append(("read", address, register, length, address_length, mailbox))
        return b"\x00"

    def write_i2c(self, address, register, data, address_length=1, mailbox=0):
        self.calls.append(("write", address, register, bytes(data), address_length, mailbox))


def test_tuner_bus_opens_and_closes_af9013_gate():
    bridge = FakeBridge()
    bus = Af9013TunerBus(bridge, 0x1C)

    bus.write(0x60, 0, b"\x84")

    assert bridge.calls == [
        ("read", 0x1C, 0xD417, 1, 3, 1),
        ("write", 0x1C, 0xD417, b"\x08", 3, 1),
        ("write", 0x60, 0, b"\x84", 1, 0),
        ("read", 0x1C, 0xD417, 1, 3, 1),
        ("write", 0x1C, 0xD417, b"\x00", 3, 1),
    ]