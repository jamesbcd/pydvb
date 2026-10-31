from threading import Event
from types import SimpleNamespace

import pytest

from pydvb.drivers.af9015.bridge import AF9015Bridge, Command, ControlRequest
from pydvb import usb as usb_module
from pydvb.usb import USBSpeed
from pydvb.usb import _LibUSB1Transport as LibUSB1Transport


class FakeAsyncTransfer:
    def __init__(self):
        self.endpoint = None
        self.length = None
        self.callback = None
        self.submitted = False
        self.submit_count = 0
        self.cancel_count = 0
        self.close_count = 0
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
        self.cancel_count += 1
        if self.submitted:
            self.submitted = False
            self.status = 1
            self.callback(self)

    def close(self):
        assert not self.submitted
        self.close_count += 1

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
    class USBErrorInterrupted(Exception):
        pass

    return SimpleNamespace(
        USBContext=lambda: context,
        SPEED_LOW=1,
        SPEED_FULL=2,
        SPEED_HIGH=3,
        SPEED_SUPER=4,
        SPEED_SUPER_PLUS=5,
        TRANSFER_COMPLETED=0,
        TRANSFER_CANCELLED=1,
        TRANSFER_ERROR=2,
        TRANSFER_TIMED_OUT=3,
        TRANSFER_OVERFLOW=4,
        USBErrorInterrupted=USBErrorInterrupted,
    )


def test_open_usb_transport_preserves_manager_context_ownership(monkeypatch):
    device = usb_module.USBDeviceInfo(0x0413, 0x6029, 1, 2, object(), object())
    calls = []
    transport = object()

    def open_device(cls, usb_device, **kwargs):
        calls.append((usb_device, kwargs))
        return transport

    monkeypatch.setattr(
        usb_module._LibUSB1Transport,
        "open_device",
        classmethod(open_device),
    )

    result = usb_module.open_usb_transport(
        device,
        async_endpoints=(0x84, 0x85),
        async_queue_mib=4,
    )

    assert result is transport
    assert calls == [
        (
            device,
            {
                "async_endpoints": (0x84, 0x85),
                "async_queue_mib": 4,
                "owns_context": False,
            },
        )
    ]


def test_load_libusb_library_uses_packaged_binary(monkeypatch, tmp_path):
    library_path = tmp_path / "libusb-1.0.dylib"
    library = object()
    opened = []
    loaded = []
    monkeypatch.setattr(usb_module.libusb_package, "get_library_path", lambda: library_path)

    def open_library(path):
        opened.append(path)
        return library

    monkeypatch.setattr(usb_module.ctypes, "CDLL", open_library)
    monkeypatch.setattr(usb_module.usb1, "loadLibrary", loaded.append)

    usb_module._load_libusb_library()

    assert opened == [str(library_path)]
    assert loaded == [library]


def test_load_libusb_library_keeps_system_fallback_when_no_binary(monkeypatch):
    loaded = []
    monkeypatch.setattr(usb_module.libusb_package, "get_library_path", lambda: None)
    monkeypatch.setattr(usb_module.usb1, "loadLibrary", loaded.append)

    usb_module._load_libusb_library()

    assert loaded == []


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


def test_libusb_find_claims_interface_and_maps_speed(monkeypatch):
    handle = FakeLibUSBHandle(speed=3)
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))

    transport = LibUSB1Transport.find(0x0413, 0x6029, interface=2, async_transfer_count=3)

    assert handle.claimed == [2]
    assert transport.get_speed() is USBSpeed.HIGH_USB2
    transport.close()
    assert handle.closed
    assert context.closed
    assert transport._claimed_interface.exited


def test_libusb_control_io_uses_synchronous_handle_methods(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_endpoints=(0x84, 0x85))

    transport.bulk_write(0x02, b"command", 2000)
    assert transport.bulk_read(0x81, 6, 2000) == b"reply"

    assert handle.writes == [(0x02, b"command", 2000)]
    assert handle.bulk_reads == [(0x81, 6, 2000)]
    transport.close()


def test_libusb_transport_uses_synchronous_reads_without_async_endpoints(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle)

    assert transport.bulk_read(0x84, 4, 100) == b"reply"
    assert handle.bulk_reads == [(0x84, 4, 100)]
    assert handle.transfers == []
    transport.close()


def test_libusb_async_bulk_read_primes_transfer_pool_and_resubmits(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_transfer_count=3, async_endpoints=(0x84, 0x85))

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
    assert all(transfer.close_count == 1 for transfer in handle.transfers)
    transport.close()
    assert all(transfer.cancel_count == 1 for transfer in handle.transfers)
    assert all(transfer.close_count == 1 for transfer in handle.transfers)


def test_libusb_async_read_stop_drains_cancels_and_releases_endpoint_state(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(
        context, handle, async_transfer_count=3, async_endpoints=(0x84,)
    )

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    first_pool = tuple(handle.transfers)

    transport.pause_async_reads(0x84)
    completed_transfer = first_pool[0]
    completed_transfer.buffer[:] = b"data"
    completed_transfer.submitted = False
    completed_transfer.callback(completed_transfer)
    assert completed_transfer.submit_count == 1

    transport.stop_async_reads(0x84, drain_timeout=0)

    assert all(transfer.close_count == 1 for transfer in first_pool)
    assert completed_transfer.cancel_count == 0
    assert all(transfer.cancel_count == 1 for transfer in first_pool[1:])
    assert not transport._async_read_queues
    assert not transport._async_read_queue_bytes
    assert not transport._paused_async_reads
    assert not transport._async_read_errors
    assert not transport._async_read_lengths
    assert not transport._async_read_transfers
    assert not transport._async_read_inflight
    assert not transport._async_read_callbacks
    assert not transport._async_read_stopping

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    second_pool = tuple(handle.transfers[3:])
    assert len(second_pool) == 3
    assert all(transfer.close_count == 0 for transfer in second_pool)

    transport.stop_async_reads(0x84, drain_timeout=0)
    assert all(transfer.close_count == 1 for transfer in second_pool)
    transport.close()


def test_libusb_async_read_pauses_and_resumes_transfers_at_queue_byte_limit(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(
        context,
        handle,
        async_transfer_count=1,
        async_queue_mib=1 / (1024 * 1024),
        async_endpoints=(0x84,),
    )

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    transfer = handle.transfers[0]
    transfer.buffer[:] = b"data"
    transfer.callback(transfer)

    assert transfer.submit_count == 1
    assert transport.bulk_read(0x84, 4, 100) == b"data"
    assert transfer.submit_count == 2
    transport.close()


@pytest.mark.parametrize("queue_mib", [0, -1, 1 / (1024 * 1024 * 2)])
def test_libusb_rejects_invalid_async_queue_limit(queue_mib):
    with pytest.raises(ValueError, match="async_queue_mib"):
        LibUSB1Transport(FakeUSBContext(), FakeLibUSBHandle(), async_queue_mib=queue_mib)


def test_libusb_none_async_queue_limit_uses_default(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))

    transport = LibUSB1Transport(context, handle, async_queue_mib=None)

    assert transport.async_queue_mib == usb_module.DEFAULT_MAX_QUEUE_SIZE
    assert transport.async_queue_bytes == usb_module.DEFAULT_MAX_QUEUE_SIZE * 1024 * 1024


def test_libusb_async_summary_reports_callback_statistics(capsys, monkeypatch):
    monkeypatch.setattr(usb_module, "LOG", False)
    monkeypatch.setattr(usb_module, "RECORD_STATS", True)
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_endpoints=(0x84, 0x85))

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    transfer = handle.transfers[0]
    transfer.buffer[:] = b"data"
    transfer.callback(transfer)
    assert transport.bulk_read(0x84, 4, 100) == b"data"
    transfer.callback(transfer)
    transport.log_stats()

    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert output[0].startswith("USB async summary: completions=2 bytes=8")
    assert "completion_intervals_ms(avg/min/max)=" in output[0]
    assert "queue_depth(min/max)=1/1" in output[0]
    assert "queue_size_mib(min/max)=0.000/0.000" in output[0]
    assert "completed_by_endpoint=0x84=2" in output[0]
    assert "zero_length=0 cancelled=0" in output[0]
    assert "transfer_error_statuses=none" in output[0]
    assert "transfer_errors=0" in output[0]
    assert "resubmit_errors=0" in output[0]
    transport.close()


def test_libusb_async_read_endpoint_length_is_stable(monkeypatch):
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_endpoints=(0x84, 0x85))

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    with pytest.raises(ValueError, match="cannot change"):
        transport.bulk_read(0x84, 8, 0)

    transport.close()


def test_libusb_async_transfer_error_is_returned_to_reader(capsys, monkeypatch):
    monkeypatch.setattr(usb_module, "RECORD_STATS", True)
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_endpoints=(0x84, 0x85))

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    transfer = handle.transfers[0]
    transfer.status = 99
    transfer.callback(transfer)

    with pytest.raises(RuntimeError, match="status 99"):
        transport.bulk_read(0x84, 4, 100)
    assert all(transfer.cancel_count == 1 for transfer in handle.transfers)
    with pytest.raises(RuntimeError, match="status 99"):
        transport.bulk_read(0x84, 4, 100)
    transport.log_stats()

    output = capsys.readouterr().out
    assert "completions=0" in output
    assert "transfer_errors=1" in output
    assert "transfer_error_statuses=0x84:99=1" in output

    transport.close()


def test_libusb_recoverable_async_error_is_recorded_and_resubmitted(capsys, monkeypatch):
    monkeypatch.setattr(usb_module, "RECORD_STATS", True)
    handle = FakeLibUSBHandle()
    context = FakeUSBContext(handle)
    monkeypatch.setattr(usb_module, "usb1", fake_usb1(context))
    transport = LibUSB1Transport(context, handle, async_endpoints=(0x84, 0x85))

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 0)
    transfer = handle.transfers[0]
    transfer.status = usb_module.usb1.TRANSFER_TIMED_OUT
    transfer.callback(transfer)

    with pytest.raises(TimeoutError):
        transport.bulk_read(0x84, 4, 100)
    assert transfer.submit_count == 2
    assert transport._async_read_errors == {}

    transfer.status = usb_module.usb1.TRANSFER_COMPLETED
    transfer.buffer[:] = b"data"
    transfer.callback(transfer)
    assert transport.bulk_read(0x84, 4, 100) == b"data"
    transport.log_stats()
    output = capsys.readouterr().out
    assert "transfer_errors=1" in output
    assert "transfer_error_statuses=0x84:3=1" in output
    transport.close()


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


def test_frequency_control_write_uses_linux_af9013_frame():
    transport = FakeTransport()
    bridge = AF9015Bridge(transport)

    bridge.write_memory(
        0xD140,
        bytes.fromhex("39 8e 0f"),
        address_length=3,
        mailbox=0x0B,
        i2c_address=0x1C,
    )

    assert transport.writes[0][1] == bytes.fromhex("21 00 38 d1 40 0b 03 03 39 8e 0f")


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
