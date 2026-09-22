from pathlib import Path

import pytest

from mactv.drivers.af9015 import device as device_module
from mactv.drivers.af9015.device import AF9015Device, DEFAULT_FIRMWARE_PATH
from mactv.drivers.af9015.frontend import DVBFrontendStatus
from mactv.driverapi.tuner import DVBTuning


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


class OrderedTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.events = []

    def close(self):
        self.events.append("close")


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
        return DVBFrontendStatus(False, False, False, False, False)

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
    assert DEFAULT_FIRMWARE_PATH == (
        Path(__file__).parents[1]
        / "mactv"
        / "drivers"
        / "af9015"
        / "firmware"
        / "dvb-usb-af9015.fw"
    )
    assert DEFAULT_FIRMWARE_PATH.is_file()


def test_open_uses_default_transport_factory(monkeypatch):
    transport = FakeTransport()
    calls = []

    def find(vendor_id, product_id):
        calls.append((vendor_id, product_id))
        return transport

    monkeypatch.setattr(device_module, "default_transport_factory", find)

    device = AF9015Device.open(0x0413, 0x6029)

    assert calls == [(0x0413, 0x6029)]
    assert device.bridge.transport is transport


def test_open_accepts_alternative_transport_factory():
    transport = FakeTransport()
    calls = []

    def factory(vendor_id, product_id):
        calls.append((vendor_id, product_id))
        return transport

    device = AF9015Device.open(0x0413, 0x6029, transport_factory=factory)

    assert calls == [(0x0413, 0x6029)]
    assert device.bridge.transport is transport


def test_device_sleep_sleeps_all_frontends():
    device = AF9015Device(FakeTransport())
    frontends = [FakeFrontend(), FakeFrontend()]
    device.adapters = frontends
    device._hardware_initialized = True

    device.sleep()

    assert [frontend.sleep_calls for frontend in frontends] == [1, 1]


def test_device_close_stops_adapters_before_closing_transport():
    transport = OrderedTransport()
    device = AF9015Device(transport)
    adapter = FakeAdapter()
    device.adapters = [adapter]

    original_stop = adapter.stop
    adapter.stop = lambda: (adapter.calls.append(("stop",)), transport.events.append("stop"))
    device.close()

    assert transport.events == ["stop", "close"]
    assert adapter.calls == [("stop",)]
    adapter.stop = original_stop


def test_device_wrappers_default_to_first_adapter_and_select_requested_adapter():
    device = AF9015Device(FakeTransport())
    first = FakeAdapter()
    second = FakeAdapter()
    device.adapters = [first, second]
    device._hardware_initialized = True

    assert device.read_status() == DVBFrontendStatus(False, False, False, False, False)
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


@pytest.mark.parametrize(
    "operation",
    [
        lambda device: device.adapter_count,
        lambda device: device.sleep(),
        lambda device: device.tune(DVBTuning(650_000_000)),
        lambda device: device.read_status(),
        lambda device: device.start(),
        lambda device: device.stop(),
        lambda device: device.read_ts(),
        lambda device: device.set_pid_filter(100),
        lambda device: device.enable_pid_filter(),
    ],
)
def test_device_rejects_adapter_operations_before_initialization(operation):
    with pytest.raises(RuntimeError, match="init_hardware"):
        operation(AF9015Device(FakeTransport()))


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
    assert not device._hardware_initialized