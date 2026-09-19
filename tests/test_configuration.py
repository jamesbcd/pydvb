from mactv.device import (
    DEMODULATOR_ADDRESS,
    EEPROM_ADDRESS,
    AF9015Device,
)
from mactv.usb import USBSpeed


class FakeTransport:
    def __init__(self, eeprom):
        self.eeprom = eeprom
        self.writes = []

    def get_speed(self):
        return USBSpeed.UNKNOWN

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append(bytes(data))

    def bulk_read(self, endpoint, length, timeout):
        packet = self.writes[-1]
        if packet[0] == 0x22 and packet[2] == (EEPROM_ADDRESS << 1) | 1:
            offset = (packet[3] << 8) | packet[4]
            return b"\x00\x00" + self.eeprom[offset : offset + 1]
        return b"\x00\x00" + bytes(max(0, length - 2))

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


def test_read_configuration_creates_adapters_from_dual_eeprom():
    eeprom = bytearray(256)
    eeprom[0x31] = 1
    eeprom[0x32] = 0x3A
    eeprom[0x36] = 0
    eeprom[0x38:0x3A] = (4570).to_bytes(2, "little")
    eeprom[0x3C] = 0x04
    eeprom[0x46] = 1
    eeprom[0x48:0x4A] = (4570).to_bytes(2, "little")
    eeprom[0x4C] = 0x0B

    device = AF9015Device(FakeTransport(bytes(eeprom)))
    device.create_adapters()
    adapters = tuple(device.adapters)

    assert len(adapters) == 2
    assert device.adapter_count == 2
    assert adapters[0].demuxer.endpoint == 0x84
    assert adapters[1].demuxer.endpoint == 0x85
    assert adapters[0].frontend.demodulator_address == DEMODULATOR_ADDRESS
    assert adapters[1].frontend.demodulator_address == 0x1D
    assert device.eeprom_hash == device._eeprom_hash(bytes(eeprom))


def test_tda18271_configuration_creates_coordinated_frontend():
    eeprom = bytearray(256)
    eeprom[0x36] = 0
    eeprom[0x38:0x3A] = (4570).to_bytes(2, "little")
    eeprom[0x3C] = 0x9C

    device = AF9015Device(FakeTransport(bytes(eeprom)))
    device.create_adapters()
    adapter = device.adapters[0]

    assert adapter.tuner is not None
    assert adapter.demuxer is not None
    assert adapter.frontend.mt2060_if == 0