import pytest

from pydvb.driver_api import RegisterMap


class FakeRegisterIO:
    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.writes = []

    def read(self, address, length):
        return self.registers.get(address, bytes(length))[:length]

    def write(self, address, data):
        self.writes.append((address, bytes(data)))
        self.registers[address] = bytes(data)


def test_register_map_reads_and_writes_consecutive_bytes():
    io = FakeRegisterIO({0x10: b"\x01\x02"})
    registers = RegisterMap(io)

    assert registers.read(0x10, 2) == b"\x01\x02"
    registers.write(0x20, b"\x03\x04")

    assert io.writes == [(0x20, b"\x03\x04")]


def test_update_bits_preserves_bits_outside_mask():
    io = FakeRegisterIO({0x10: b"\xa0"})
    registers = RegisterMap(io)

    registers.update_bits(0x10, 0x0F, 0x05)

    assert io.registers[0x10] == b"\xa5"


def test_update_bits_supports_multi_byte_little_endian_registers():
    io = FakeRegisterIO({0x10: b"\x34\x12"})
    registers = RegisterMap(io, value_bits=16, byteorder="little")

    registers.update_bits(0x10, 0xFF00, 0xAB00)

    assert io.registers[0x10] == b"\x34\xab"


def test_register_map_rejects_invalid_lengths_and_short_reads():
    registers = RegisterMap(FakeRegisterIO({0x10: b"\x00"}))

    with pytest.raises(ValueError, match="length must be positive"):
        registers.read(0x10, 0)
    with pytest.raises(IOError, match="short register read"):
        registers.read(0x10, 2)


def test_register_map_validates_value_width_and_masks():
    with pytest.raises(ValueError, match="value_bits"):
        RegisterMap(FakeRegisterIO(), value_bits=7)

    with pytest.raises(ValueError, match="fit in 8 bits"):
        RegisterMap(FakeRegisterIO()).update_bits(0x10, 0x100, 0)