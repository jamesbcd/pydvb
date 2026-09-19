from pathlib import Path

import pytest

from mactv.device import AF9015Device, DEFAULT_FIRMWARE_PATH
from mactv.frontend import FrontendStatus


class FakeTransport:
    def __init__(self):
        self.writes = []

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append(bytes(data))

    def bulk_read(self, endpoint, length, timeout):
        return b"\x00\x00\x02" if length >= 3 else b"\x00\x00"

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


class FakeFrontend:
    def __init__(self):
        self.sleep_calls = 0

    def sleep(self):
        self.sleep_calls += 1


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.tuner = object()

    def read_status(self):
        self.calls.append(("read_status",))
        return FrontendStatus(False, False, False, False, False)

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def read_ts(self, timeout_ms=2000):
        self.calls.append(("read_ts", timeout_ms))
        return b"ts"

    def set_pid_filter(self, pid, enabled=True):
        self.calls.append(("set_pid_filter", pid, enabled))

    def enable_pid_filter(self, enabled=True):
        self.calls.append(("enable_pid_filter", enabled))


def test_default_firmware_path_exists():
    assert DEFAULT_FIRMWARE_PATH == Path(__file__).parents[1] / "mactv" / "firmware" / "dvb-usb-af9015.fw"
    assert DEFAULT_FIRMWARE_PATH.is_file()


def test_device_sleep_sleeps_all_frontends():
    device = AF9015Device(FakeTransport())
    frontends = [FakeFrontend(), FakeFrontend()]
    device.adapters = frontends

    device.sleep()

    assert [frontend.sleep_calls for frontend in frontends] == [1, 1]


def test_device_wrappers_default_to_first_adapter_and_select_requested_adapter():
    device = AF9015Device(FakeTransport())
    first = FakeAdapter()
    second = FakeAdapter()
    device.adapters = [first, second]

    assert device.read_status() == FrontendStatus(False, False, False, False, False)
    device.start()
    device.set_pid_filter(100)
    device.enable_pid_filter()
    assert device.read_ts(10) == b"ts"
    device.stop()
    device.read_status(adapter_index=1)
    device.start(adapter_index=1)
    device.set_pid_filter(200, adapter_index=1)
    device.enable_pid_filter(False, adapter_index=1)
    assert device.read_ts(adapter_index=1) == b"ts"
    device.stop(adapter_index=1)

    assert first.calls == [
        ("read_status",),
        ("start",),
        ("set_pid_filter", 100, True),
        ("enable_pid_filter", True),
        ("read_ts", 10),
        ("stop",),
    ]
    assert second.calls == [
        ("read_status",),
        ("start",),
        ("set_pid_filter", 200, True),
        ("enable_pid_filter", False),
        ("read_ts", 2000),
        ("stop",),
    ]


def test_device_loads_firmware_from_path(tmp_path):
    firmware_path = tmp_path / "test.fw"
    firmware_path.write_bytes(b"firmware")
    transport = FakeTransport()
    device = AF9015Device(transport)

    device.download_firmware(firmware_path=firmware_path)

    assert transport.writes[0][8:] == b"firmware"


def test_firmware_bytes_and_path_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError):
        AF9015Device(FakeTransport()).download_firmware(b"firmware", tmp_path / "test.fw")


class ReconnectTransport:
    def __init__(self, reply, candidates=()):
        self.reply = reply
        self.candidates = iter(candidates)
        self.closed = False

    def bulk_write(self, endpoint, data, timeout):
        pass

    def bulk_read(self, endpoint, length, timeout):
        return self.reply

    def close(self):
        self.closed = True

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        candidate = next(self.candidates)
        if isinstance(candidate, Exception):
            raise candidate
        self.close()
        return candidate


def test_reconnect_replaces_handle_after_get_config_succeeds():
    candidate = ReconnectTransport(b"\x00\x00\x02")
    original = ReconnectTransport(b"\x00\x00\x00", (candidate,))
    device = AF9015Device(original, vendor_id=0x0413, product_id=0x6029)

    assert device._reconnect(timeout=1, poll_interval=0.001) == 0x02
    assert original.closed
    assert device.bridge.transport is candidate