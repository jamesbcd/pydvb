from hashlib import sha256
from pathlib import Path

import pytest
import requests

from pydvb.driver_api.device import DVBDevice
from pydvb.driver_api.tuner import DVBTuning


class FakeTransport:
    def close(self):
        pass


class OrderedTransport(FakeTransport):
    def __init__(self):
        self.events = []

    def close(self):
        self.events.append("close")


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.tuner = object()

    def bind_to_control_lock(self, control_lock):
        self.control_lock = control_lock

    def read_status(self):
        self.calls.append(("read_status",))
        return STATUS

    def tune(self, tuning, wait_for_lock=True):
        self.calls.append(("tune", tuning, wait_for_lock))

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def sleep(self):
        self.calls.append(("sleep",))

    def read_stream(self, timeout_ms=2000):
        self.calls.append(("read_stream", timeout_ms))
        return b"ts"

    def configure_pid_filter(self, pids, enabled=True):
        self.calls.append(("configure_pid_filter", pids, enabled))

    def set_pid_filter_enabled(self, enabled=True):
        self.calls.append(("set_pid_filter_enabled", enabled))


class FakeDevice(DVBDevice):
    FIRMWARE_CACHE_DIR = Path(".")

    @classmethod
    def open_usb_device(cls, device):
        raise NotImplementedError

    def is_warm(self):
        return False

    def log_transport_stats(self):
        pass

    def init_hardware(self):
        self._hardware_initialized = True


class FirmwareResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        pass

    def raise_for_status(self):
        pass


STATUS = object()


def configure_test_firmware(monkeypatch, tmp_path, urls, firmware):
    monkeypatch.setattr(FakeDevice, "FIRMWARE_URLS", urls)
    monkeypatch.setattr(FakeDevice, "FIRMWARE_FILENAME", "test-firmware.fw")
    monkeypatch.setattr(FakeDevice, "FIRMWARE_SHA256", sha256(firmware).hexdigest())
    monkeypatch.setattr(FakeDevice, "FIRMWARE_CACHE_DIR", tmp_path)


def test_device_shares_control_lock_with_adapters():
    device = FakeDevice(FakeTransport())
    first = FakeAdapter()
    second = FakeAdapter()
    device.adapters = [first, second]

    device._share_control_lock_with_all_adapters()

    assert first.control_lock is device._control_lock
    assert second.control_lock is device._control_lock


def test_device_sleep_sleeps_all_adapters():
    device = FakeDevice(FakeTransport())
    adapters = [FakeAdapter(), FakeAdapter()]
    device.adapters = adapters
    device._hardware_initialized = True

    device.sleep()

    assert [adapter.calls for adapter in adapters] == [[("sleep",)], [("sleep",)]]


def test_device_close_stops_adapters_before_closing_transport():
    transport = OrderedTransport()
    device = FakeDevice(transport)
    adapter = FakeAdapter()
    device.adapters = [adapter]
    adapter.stop = lambda: (adapter.calls.append(("stop",)), transport.events.append("stop"))

    device.close()

    assert transport.events == ["stop", "close"]
    assert adapter.calls == [("stop",)]


def test_device_close_stops_then_sleeps_before_closing_transport():
    transport = OrderedTransport()
    device = FakeDevice(transport)
    adapter = FakeAdapter()
    device.adapters = [adapter]
    device._hardware_initialized = True
    adapter.stop = lambda: transport.events.append("stop")
    adapter.sleep = lambda: transport.events.append("sleep")

    device.close()

    assert transport.events == ["stop", "sleep", "close"]


def test_device_close_closes_transport_if_sleep_fails():
    transport = OrderedTransport()
    device = FakeDevice(transport)
    adapter = FakeAdapter()
    device.adapters = [adapter]
    device._hardware_initialized = True
    adapter.stop = lambda: transport.events.append("stop")

    def fail_sleep():
        transport.events.append("sleep")
        raise RuntimeError("sleep failed")

    adapter.sleep = fail_sleep

    with pytest.raises(RuntimeError, match="sleep failed"):
        device.close()

    assert transport.events == ["stop", "sleep", "close"]


def test_device_wrappers_default_to_first_adapter_and_select_requested_adapter():
    device = FakeDevice(FakeTransport())
    first = FakeAdapter()
    second = FakeAdapter()
    device.adapters = [first, second]
    device._hardware_initialized = True

    assert device.read_status() is STATUS
    device.start()
    device.configure_pid_filter({100})
    device.set_pid_filter_enabled()
    assert device.read_stream(10) == b"ts"
    device.stop()
    device.read_status(adapter_index=1)
    device.start(adapter_index=1)
    device.configure_pid_filter({200}, adapter_index=1)
    device.set_pid_filter_enabled(False, adapter_index=1)
    assert device.read_stream(adapter_index=1) == b"ts"
    device.stop(adapter_index=1)

    assert first.calls == [
        ("read_status",),
        ("start",),
        ("configure_pid_filter", {100}, True),
        ("set_pid_filter_enabled", True),
        ("read_stream", 10),
        ("stop",),
    ]
    assert second.calls == [
        ("read_status",),
        ("start",),
        ("configure_pid_filter", {200}, True),
        ("set_pid_filter_enabled", False),
        ("read_stream", 2000),
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
        lambda device: device.read_stream(),
        lambda device: device.configure_pid_filter({100}),
        lambda device: device.set_pid_filter_enabled(),
    ],
)
def test_device_rejects_adapter_operations_before_initialization(operation):
    with pytest.raises(RuntimeError, match="init_hardware"):
        operation(FakeDevice(FakeTransport()))


def test_firmware_resolver_downloads_then_reuses_verified_cache(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    requests_made = []

    def get(url, timeout):
        requests_made.append((url, timeout))
        return FirmwareResponse(firmware)

    monkeypatch.setattr(requests, "get", get)
    device = FakeDevice(FakeTransport())

    cached_path = device.resolve_firmware_path()
    assert cached_path.read_bytes() == firmware
    assert device.resolve_firmware_path() == cached_path
    assert requests_made == [("https://example.test/firmware", 15)]


def test_firmware_resolver_replaces_cache_with_invalid_digest(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    cached_path = tmp_path / "test-firmware.fw"
    cached_path.write_bytes(b"stale firmware")
    monkeypatch.setattr(requests, "get", lambda url, timeout: FirmwareResponse(firmware))
    device = FakeDevice(FakeTransport())

    assert device.resolve_firmware_path() == cached_path
    assert cached_path.read_bytes() == firmware


def test_firmware_resolver_tries_next_url_after_digest_mismatch(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    urls = ("https://example.test/bad", "https://example.test/good")
    configure_test_firmware(monkeypatch, tmp_path, urls, firmware)
    responses = iter((FirmwareResponse(b"wrong firmware"), FirmwareResponse(firmware)))
    requests_made = []

    def get(url, timeout):
        requests_made.append(url)
        return next(responses)

    monkeypatch.setattr(requests, "get", get)
    device = FakeDevice(FakeTransport())

    cached_path = device.resolve_firmware_path()

    assert cached_path.read_bytes() == firmware
    assert requests_made == list(urls)


def test_firmware_resolver_fails_without_cached_file_when_download_fails(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    cache_path = tmp_path / "cache"
    configure_test_firmware(monkeypatch, cache_path, ("https://example.test/firmware",), firmware)

    def fail_request(url, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", fail_request)
    device = FakeDevice(FakeTransport())

    with pytest.raises(IOError, match="could not obtain verified firmware"):
        device.resolve_firmware_path()
    assert not (cache_path / "test-firmware.fw").exists()