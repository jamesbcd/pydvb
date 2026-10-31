from hashlib import sha256
from pathlib import Path

import pytest
import requests

from mactv.drivers.af9015 import device as device_module
from mactv.drivers.af9015.device import AF9015Device
from mactv.drivers.af9015.frontend import DVBFrontendStatus
from mactv.driver_api import device as driver_device_module
from mactv.driver_api.tuner import DVBTuning
from mactv.usb import USBDeviceID, USBDeviceInfo


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

    def read_stream(self, timeout_ms=2000):
        self.calls.append(("read_stream", timeout_ms))
        return b"ts"

    def configure_pid_filter(self, pids, enabled=True):
        self.calls.append(("configure_pid_filter", pids, enabled))

    def set_pid_filter_enabled(self, enabled=True):
        self.calls.append(("set_pid_filter_enabled", enabled))


class LockAwareAdapter(FakeAdapter):
    def bind_to_control_lock(self, control_lock):
        self.control_lock = control_lock


def test_device_shares_control_lock_with_adapters():
    device = AF9015Device(FakeTransport())
    first = LockAwareAdapter()
    second = LockAwareAdapter()
    device.adapters = [first, second]

    device._share_control_lock_with_all_adapters()

    assert first.control_lock is device._control_lock
    assert second.control_lock is device._control_lock


def test_firmware_cache_is_inside_concrete_driver_package():
    assert AF9015Device.FIRMWARE_CACHE_DIR == Path(device_module.__file__).resolve().parent / "firmware"


def test_open_usb_device_uses_manager_transport_opener(monkeypatch):
    transport = FakeTransport()
    calls = []

    device_info = USBDeviceInfo(0x0413, 0x6029, 1, 2, object(), object())

    def open_transport(device, **kwargs):
        calls.append((device, kwargs))
        return transport

    monkeypatch.setattr(device_module, "open_usb_transport", open_transport)

    device = AF9015Device.open_usb_device(device_info)

    assert calls == [(device_info, {"async_endpoints": AF9015Device.STREAM_ENDPOINTS})]
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


def test_device_close_stops_then_sleeps_before_closing_transport():
    transport = OrderedTransport()
    device = AF9015Device(transport)
    adapter = FakeAdapter()
    device.adapters = [adapter]
    device._hardware_initialized = True
    adapter.stop = lambda: transport.events.append("stop")
    adapter.sleep = lambda: transport.events.append("sleep")

    device.close()

    assert transport.events == ["stop", "sleep", "close"]


def test_device_close_closes_transport_if_sleep_fails():
    transport = OrderedTransport()
    device = AF9015Device(transport)
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
    device = AF9015Device(FakeTransport())
    first = FakeAdapter()
    second = FakeAdapter()
    device.adapters = [first, second]
    device._hardware_initialized = True

    assert device.read_status() == DVBFrontendStatus(False, False, False, False, False)
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
    device = AF9015Device(original, USBDeviceID(0x0413, 0x6029))

    assert device._reconnect(timeout=1, poll_interval=0.001) == 0x02
    assert original.closed
    assert device.bridge.transport is candidate
    assert not device._hardware_initialized


class FirmwareResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        pass

    def raise_for_status(self):
        pass


def configure_test_firmware(monkeypatch, tmp_path, urls, firmware):
    monkeypatch.setattr(AF9015Device, "FIRMWARE_URLS", urls)
    monkeypatch.setattr(AF9015Device, "FIRMWARE_FILENAME", "test-firmware.fw")
    monkeypatch.setattr(AF9015Device, "FIRMWARE_SHA256", sha256(firmware).hexdigest())
    monkeypatch.setattr(AF9015Device, "FIRMWARE_CACHE_DIR", tmp_path)


def test_firmware_resolver_downloads_then_reuses_verified_cache(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    requests_made = []

    def get(url, timeout):
        requests_made.append((url, timeout))
        return FirmwareResponse(firmware)

    monkeypatch.setattr(driver_device_module.requests, "get", get)
    device = AF9015Device(FakeTransport())

    cached_path = device.resolve_firmware_path()
    assert cached_path.read_bytes() == firmware
    assert device.resolve_firmware_path() == cached_path
    assert requests_made == [("https://example.test/firmware", 15)]


def test_firmware_resolver_replaces_cache_with_invalid_digest(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    cached_path = tmp_path / "test-firmware.fw"
    cached_path.write_bytes(b"stale firmware")
    monkeypatch.setattr(
        driver_device_module.requests,
        "get",
        lambda url, timeout: FirmwareResponse(firmware),
    )
    device = AF9015Device(FakeTransport())

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

    monkeypatch.setattr(driver_device_module.requests, "get", get)
    device = AF9015Device(FakeTransport())

    cached_path = device.resolve_firmware_path()

    assert cached_path.read_bytes() == firmware
    assert requests_made == list(urls)


def test_firmware_resolver_fails_without_cached_file_when_download_fails(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path / "cache", ("https://example.test/firmware",), firmware)

    def fail_request(url, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(driver_device_module.requests, "get", fail_request)
    device = AF9015Device(FakeTransport())

    with pytest.raises(IOError, match="could not obtain verified firmware"):
        device.resolve_firmware_path()
    assert not (tmp_path / "cache" / "test-firmware.fw").exists()


def test_download_firmware_caches_and_uses_remote_image(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    monkeypatch.setattr(
        driver_device_module.requests,
        "get",
        lambda url, timeout: FirmwareResponse(firmware),
    )
    transport = FakeTransport()
    device = AF9015Device(transport)

    device.download_firmware()

    assert transport.writes[0][8:] == firmware
    assert (tmp_path / "test-firmware.fw").read_bytes() == firmware