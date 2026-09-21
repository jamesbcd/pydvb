import sys
from threading import Event
from types import SimpleNamespace

import pytest

from mactv.bridge import AF9015Bridge, Command, ControlRequest
from mactv import usb as usb_module
from mactv.usb import LibUSB1Transport, PyUSBTransport, USBSpeed


class FakeAsyncTransfer:
    def __init__(self):
        self.endpoint = None
        self.length = None
        self.callback = None
        self.submitted = False
        self.submit_count = 0
        self.cancel_count = 0
        self.buffer = bytearray()
        self.status = 0

    def setBulk(self, endpoint, length, callback):
        self.endpoint = endpoint
        self.length = length
        self.callback = callback
        self.buffer = bytearray(length)

    def submit(self):
        self.submitted = True
        self.submit_count += 1

    def cancel(self):
        self.submitted = False
        self.cancel_count += 1

    def isSubmitted(self):
        return self.submitted

    def getStatus(self):
        return self.status

    def getBuffer(self):
        return self.buffer

    def getActualLength(self):
        return len(self.buffer)


class FakeLibUSBHandle:
    def __init__(self, speed=3):
        self.speed = speed
        self.transfers = []
        self.writes = []
        self.bulk_reads = []
        self.closed = False
        self.claimed = []

    def getTransfer(self):
        transfer = FakeAsyncTransfer()
        self.transfers.append(transfer)
        return transfer

    def claimInterface(self, interface):
        self.claimed.append(interface)
        return FakeClaim()

    def bulkWrite(self, endpoint, data, timeout):
        self.writes.append((endpoint, bytes(data), timeout))

    def bulkRead(self, endpoint, length, timeout):
        self.bulk_reads.append((endpoint, length, timeout))
        return b"reply"

    def getDevice(self):
        return SimpleNamespace(getDeviceSpeed=lambda: self.speed)

    def close(self):
        self.closed = True


class FakeClaim:
    def __init__(self):
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args):
        self.exited = True


class FakeUSBContext:
    def __init__(self, handle=None):
        self.handle = handle or FakeLibUSBHandle()
        self.closed = False
        self.event_wakeup = Event()

    def openByVendorIDAndProductID(self, vendor_id, product_id, skip_on_error):
        return self.handle

    def handleEvents(self):
        self.event_wakeup.wait(0.01)

    def close(self):
        self.closed = True


def fake_usb1(context):
    return SimpleNamespace(
        USBContext=lambda: context,
        SPEED_LOW=1,
        SPEED_FULL=2,
        SPEED_HIGH=3,
        SPEED_SUPER=4,
        SPEED_SUPER_PLUS=5,
        TRANSFER_COMPLETED=0,
        TRANSFER_CANCELLED=1,
    )


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


def test_get_speed_maps_pyusb_speed_value_to_enum():
    assert PyUSBTransport(type("Device", (), {"speed": 2})()).get_speed() is USBSpeed.FULL
    assert PyUSBTransport(type("Device", (), {"speed": 1})()).get_speed() is USBSpeed.LOW
    assert PyUSBTransport(type("Device", (), {"speed": 3})()).get_speed() is USBSpeed.HIGH_USB2


def test_get_speed_returns_unknown_for_missing_or_invalid_value():
    assert PyUSBTransport(type("Device", (), {})()).get_speed() is USBSpeed.UNKNOWN
    assert PyUSBTransport(type("Device", (), {"speed": 99})()).get_speed() is USBSpeed.UNKNOWN


def test_libusb_find_claims_interface_and_maps_speed(monkeypatch):
    handle = FakeLibUSBHandle(speed=3)
    context = FakeUSBContext(handle)
    monkeypatch.setitem(sys.modules, "usb1", fake_usb1(context))

    transport = LibUSB1Transport.find(0x0413, 0x6029, interface=2, async_transfer_count=3)

    assert handle.claimed == [2]
    assert transport.get_speed() is USBSpeed.HIGH_USB2
    transport.close()
    assert handle.closed
    assert context.closed
    assert transport._claimed_interface.exited


def test_libusb_control_io_uses_synchronous_handle_methods():
    handle = FakeLibUSBHandle()
    transport = LibUSB1Transport(FakeUSBContext(handle), handle)
    transport._usb1 = fake_usb1(transport.context)

    transport.bulk_write(0x02, b"command", 2000)
    assert transport.bulk_read(0x81, 6, 2000) == b"reply"

    assert handle.writes == [(0x02, b"command", 2000)]
    assert handle.bulk_reads == [(0x81, 6, 2000)]
    transport.close()


def test_libusb_stream_read_primes_async_transfer_pool_and_resubmits():
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    transport = LibUSB1Transport(context, handle, async_transfer_count=3)
    transport._usb1 = fake_usb1(context)

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)

    assert len(handle.transfers) == 3
    assert all(transfer.endpoint == 0x84 for transfer in handle.transfers)
    assert all(transfer.submit_count == 1 for transfer in handle.transfers)

    transfer = handle.transfers[0]
    transfer.buffer[:] = b"data"
    transfer.callback(transfer)

    assert transport.bulk_read(0x84, 4, 100) == b"data"
    assert transfer.submit_count == 2

    transport.close()
    assert all(transfer.cancel_count == 1 for transfer in handle.transfers)
    transport.close()
    assert all(transfer.cancel_count == 1 for transfer in handle.transfers)


def test_libusb_stream_endpoint_length_is_stable():
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    transport = LibUSB1Transport(context, handle)
    transport._usb1 = fake_usb1(context)

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    with pytest.raises(ValueError, match="cannot change"):
        transport.bulk_read(0x84, 8, 0)

    transport.close()


def test_libusb_async_transfer_error_is_returned_to_reader():
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    transport = LibUSB1Transport(context, handle)
    transport._usb1 = fake_usb1(context)

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    transfer = handle.transfers[0]
    transfer.status = 99
    transfer.callback(transfer)

    with pytest.raises(RuntimeError, match="status 99"):
        transport.bulk_read(0x84, 4, 100)

    transport.close()


def test_stream_read_cadence_logging_reports_aggregate_timing(capsys, monkeypatch):
    class Device:
        speed = 3

        def read(self, endpoint, length, timeout):
            return bytes(length)

    monkeypatch.setattr(usb_module, "CADENCE_LOG", True)
    transport = PyUSBTransport(Device())

    transport.bulk_read(0x84, 188, 1000)
    transport.bulk_read(0x84, 188, 1000)
    transport.bulk_read(0x81, 2, 1000)
    transport.log_cadence_summary()

    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert output[0].startswith("USB cadence: reads=2 errors=0 bytes=376")
    assert "read_ms(avg/min/max)=" in output[0]
    assert "gap_ms(min/max)=" in output[0]


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


def test_reconnect_sends_only_header_without_ack():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    bridge.transact(ControlRequest(Command.RECONNECT_USB, data=b"ignored"))

    assert transport.writes[0][1] == bytes((Command.RECONNECT_USB, 0, 0, 0, 0, 0, 0, 0))


def test_i2c_helpers_encode_af9013_mailbox():
    transport = FakeTransport(reply=b"\x00\x00")
    bridge = AF9015Bridge(transport)

    bridge.write_i2c(0x1C, 0xD417, b"\x08", address_length=3, mailbox=1)

    assert transport.writes[0][1] == bytes((Command.READ_WRITE_I2C, 0, 0x38, 0xD4, 0x17, 1, 3, 1, 0x08))


def test_memory_helpers_encode_af9013_register_access():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    bridge.read_memory(0xD417, 1, address_length=3, mailbox=1)

    assert transport.writes[0][1] == bytes((Command.READ_MEMORY, 0, 0, 0xD4, 0x17, 1, 3, 1))


def test_memory_helpers_encode_af9013_address_and_command_byte():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    bridge.read_memory(0xD330, 3, address_length=3, mailbox=0x0A, i2c_address=0x1C)
    bridge.write_memory(0xD3C0, b"\x00\x14\x00", address_length=3, mailbox=0x0B, i2c_address=0x1C)

    assert transport.writes[0][1] == bytes((Command.READ_MEMORY, 0, 0x38, 0xD3, 0x30, 0x0A, 3, 3))
    assert transport.writes[1][1] == bytes((Command.WRITE_MEMORY, 1, 0x38, 0xD3, 0xC0, 0x0B, 3, 3, 0, 0x14, 0))


def test_i2c_helpers_use_bridge_protocol():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    assert bridge.read_i2c(0x50, 0x18, 1, address_length=2, mailbox=1) == b"\xaa"
    bridge.write_i2c_byte(0x1C, 0xD417, 0x108, address_length=3)

    assert transport.writes[0][1] == bytes((Command.READ_WRITE_I2C, 0, 0xA1, 0, 0x18, 1, 2, 1))
    assert transport.writes[1][1] == bytes((Command.READ_WRITE_I2C, 1, 0x38, 0xD4, 0x17, 0, 3, 1, 0x08))


def test_read_and_write_limits_match_linux_buffer_layout():
    transport = FakeTransport(reply=b"\x00\x00" + bytes(range(61)))
    bridge = AF9015Bridge(transport)

    assert bridge.read_i2c(0x50, 0x18, 61) == bytes(range(61))

    try:
        bridge.write_i2c(0x50, 0x18, bytes(56))
    except ValueError as error:
        assert str(error) == "AF9015 control payload is limited to 55 bytes"
    else:
        raise AssertionError("56-byte write should exceed the AF9015 request buffer")
