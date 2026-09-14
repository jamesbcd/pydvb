from mactv.bridge import AF9015Bridge, Command


class FakeTransport:
    def __init__(self, reply=b"\x00\x00\xaa"):
        self.writes = []
        self.reply = reply

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append((endpoint, bytes(data), timeout))

    def bulk_read(self, endpoint, length, timeout):
        assert endpoint == 0x81
        return self.reply[:length]

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


def test_read_i2c_uses_linux_af9015_frame():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    result = bridge.read_i2c(0x50, 0x18, 1)

    assert result == b"\xaa"
    endpoint, packet, _ = transport.writes[0]
    assert endpoint == 0x02
    assert packet == bytes((Command.READ_WRITE_I2C, 0, 0xA1, 0, 0x18, 0, 1, 1))


def test_firmware_is_chunked_to_control_buffer_size():
    transport = FakeTransport(reply=b"\x00\x00")
    bridge = AF9015Bridge(transport)

    bridge.download_firmware(bytes(range(60)))

    assert len(transport.writes) == 3
    assert transport.writes[0][1][7] == 55
    assert transport.writes[-1][1][0] == Command.BOOT


def test_i2c_helpers_encode_af9013_mailbox():
    transport = FakeTransport(reply=b"\x00\x00")
    bridge = AF9015Bridge(transport)

    bridge.write_i2c(0x1C, 0xD417, b"\x08", address_length=3, mailbox=1)

    assert transport.writes[0][1] == bytes((Command.READ_WRITE_I2C, 0, 0x38, 0xD4, 0x17, 1, 3, 1, 0x08))


def test_i2c_helpers_use_bridge_protocol():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    assert bridge.read_i2c(0x50, 0x18, 1, address_length=2, mailbox=1) == b"\xaa"
    bridge.write_i2c_byte(0x1C, 0xD417, 0x108, address_length=3)

    assert transport.writes[0][1] == bytes((Command.READ_WRITE_I2C, 0, 0xA1, 0, 0x18, 1, 2, 1))
    assert transport.writes[1][1] == bytes((Command.READ_WRITE_I2C, 1, 0x38, 0xD4, 0x17, 0, 3, 1, 0x08))
